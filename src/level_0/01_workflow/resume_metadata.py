#!/usr/bin/env python3
"""Activate the reviewed metadata update and regenerate Step 08 only.

Completed stages 01-07 and all prior outputs remain in place. The old package,
configuration and batch identity are pinned and retained in the update audit.
"""
import argparse
import os
from pathlib import Path
import workflow as wf


def historical_receipt(batch, step, path=None, seen=None):
    """Verify old metadata against its archived parents, even if superseded."""
    path=path or wf.receipt(batch,step)
    seen=set() if seen is None else seen
    digest=wf.sha(path)
    if digest in seen:return wf.strict_load(path)
    seen.add(digest)
    r=wf.strict_load(path)
    wf.require(r['complete'] is True and r['stage']==step and
               set(r['parent_receipts'])=={str(p) for p in wf.PARENTS[step]},'Invalid historical receipt')
    for parent,expected in r['parent_receipts'].items():
        parent=int(parent)
        archived=[p for p in (batch/'state').glob(f'{parent:02d}_*.json') if wf.sha(p)==expected]
        wf.require(len(archived)==1,'Historical parent receipt missing or ambiguous')
        historical_receipt(batch,parent,archived[0],seen)
    for name,expected in r['external_inputs'].items():
        wf.require(wf.sha(Path(name))==expected,'Historical rules/proof changed: '+name)
    for name,expected in r['report_sha256'].items():
        wf.require(wf.sha(Path(r['output'])/name)==expected,'Historical report changed: '+name)
    return r


def activate(config_path, previous_package=None, previous_config=None):
    spec=wf.strict_load(wf.PACKAGE/'configs/metadata_code_update.json')
    wf.require(spec['schema']=='mtl0.metadata_code_update/1','Unknown metadata update')
    target=wf.code_hashes()
    wf.require(target==spec['target_code_sha256'],'Target package differs from the reviewed release')
    c,cp=wf.config(config_path)
    batch=Path(c['batch_dir']);state=batch/'state';identity=state/'batch.json'
    current=wf.strict_load(identity)
    plan=wf.preflight(c)
    after={**plan,'configuration_sha256':wf.sha(cp),'code_sha256':target}
    audit=state/'code_updates'/spec['update_id']
    audit=audit.with_suffix('.json')
    wf.require(not list(state.glob('*.lock')),'A stage lock exists; finish or investigate that job first')
    lock=state/'code_update.lock'
    wf.dump(lock,{'job_id':os.getenv('PBS_JOBID'),'pid':os.getpid(),'created_utc':wf.now()})
    try:
        # Successful activation may be retried after Step 08 has run. Validate
        # the preserved prefix, not stale pointers for the old metadata outputs.
        if current==after:
            saved=wf.strict_load(audit)
            wf.require(saved['active_batch_identity']==after and saved['update_id']==spec['update_id'],
                       'Active metadata update has no matching audit')
            for step,digest in saved['preserved_prefix_receipts_sha256'].items():
                wf.validate_receipt(c,batch,int(step))
                wf.require(wf.sha(wf.receipt(batch,int(step)))==digest,'Preserved stage changed')
            wf.setup(c,cp)
            print('Metadata update already active; stages 01-07 revalidated.',flush=True)
            return c,cp

        candidates=[s for s in spec['sources'] if s['code_sha256']==current['code_sha256']]
        wf.require(len(candidates)==1,'Unsupported frozen package; expected a pinned v1.2, v1.2.1 or v1.2.2 release')
        source=candidates[0]
        package=(previous_package or wf.PACKAGE.parent/source['package_directory']).resolve(strict=True)
        actual={p.relative_to(package).as_posix():wf.sha(p)
                for p in sorted([package/'workflow.py',*(package/'engine').glob('*.py')])}
        wf.require(actual==source['code_sha256'],'Previous package differs from the pinned release')
        old,old_cp=wf.config(previous_config or package/'configs/hillside.json')
        c,cp=wf.config(cp)
        ignored={'ownership_rules','selection_rules','metadata_rules'}
        wf.require({k:v for k,v in old.items() if k not in ignored}==
                   {k:v for k,v in c.items() if k not in ignored},'Acquisition/output configuration changed')
        if old.get('metadata_rules') is not None:
            wf.require('engine/metadata_review.py' in actual,'Earlier package cannot support metadata rules')
            previous_review=wf.strict_load(Path(old['metadata_rules']))
            wf.require(previous_review['schema']=='mtl0.metadata_rules/1' and
                       previous_review['original_workbook_sha256']==plan['metadata_sha256'],
                       'Earlier metadata review source differs')
        for key in ('ownership_rules','selection_rules'):
            wf.require(wf.sha(Path(old[key]))==wf.sha(Path(c[key])),'Reviewed rules differ: '+key)
        before={**plan,'configuration_sha256':wf.sha(old_cp),'code_sha256':source['code_sha256']}
        wf.require(current==before,'Frozen acquisition, source workbook or configuration differs')

        # The file lists pin the whole release. These additional checks prevent
        # an accidental new pin from silently changing the preserved prefix.
        permitted={'workflow.py','engine/metadata_workbook.py','engine/08_prepare_metadata.py',
                   'engine/json_metadata_inputs.py','engine/station_metadata_json.py','engine/qc_batch_reuse.py',
                   'engine/metadata_review.py'}
        changed={p for p in actual if target.get(p)!=actual[p]}
        added={'engine/metadata_review.py'}-set(actual)
        wf.require(changed<=permitted and set(target)-set(actual)==added and
                   not set(actual)-set(target),'Update changes a preserved computation module')
        from qc_batch_reuse import function_text
        for name in ('closure','validate_receipt','preflight','code_hashes','setup','release'):
            wf.require(function_text(package/'workflow.py',name)==function_text(wf.PACKAGE/'workflow.py',name),
                       'Preserved workflow function changed: '+name)

        receipts={s:wf.validate_receipt(c,batch,s) for s in range(1,8)}
        # Previous metadata, if completed, must also be internally consistent.
        # It is audited here and replaced only by later, separately named runs.
        for s in (8,9,10):
            if wf.receipt(batch,s).exists():receipts[s]=historical_receipt(batch,s)
        hashes={str(s):wf.sha(wf.receipt(batch,s)) for s in receipts}
        for s,r in receipts.items():
            wf.require(r['stage']==s and set(r['parent_receipts'])=={str(p) for p in wf.PARENTS[s]},
                       'Unexpected stage dependency chain')
            wf.require(wf.sha(state/f'{s:02d}_{r["revision"]}.json')==hashes[str(s)],'Archived receipt differs')
        review_hash=wf.sha(Path(c['metadata_rules'])) if c.get('metadata_rules') else None
        # Validate the metadata rules schema and source pin before activation.
        from metadata_review import attach
        attach({'input_sha256':{}},Path(c['metadata_workbook']))
        proof={'schema':'mtl0.metadata_update_audit/1','update_id':spec['update_id'],
               'reason':spec['reason'],'previous_package':str(package),'new_package':str(wf.PACKAGE),
               'source_configuration':str(old_cp),'configuration':str(cp),
               'previous_batch_identity':before,'active_batch_identity':after,
               'previous_stage_receipts_sha256':hashes,
               'preserved_prefix_receipts_sha256':{s:h for s,h in hashes.items() if int(s)<=7},
               'previous_outputs':{str(s):r['output'] for s,r in receipts.items()},
               'previous_metadata_parent_matches_current':{
                   str(s):all(wf.sha(wf.receipt(batch,int(parent)))==digest
                              for parent,digest in r['parent_receipts'].items())
                   for s,r in receipts.items() if s>=8},
               'metadata_rules_sha256_at_activation':review_hash,'changed_files':sorted(changed),
               'added_files':sorted(set(target)-set(actual)),
               'raw_samples_read':False,'raw_samples_modified':False,'previous_outputs_modified':False,
               'activation':'Active only when state/batch.json equals active_batch_identity.'}
        if audit.exists():wf.require(wf.strict_load(audit)==proof,'Existing update audit differs')
        wf.require(wf.strict_load(identity)==before and wf.code_hashes()==target,'Identity/code changed')
        wf.require(wf.preflight(c)==plan and wf.sha(cp)==after['configuration_sha256'] and
                   wf.sha(old_cp)==before['configuration_sha256'],'Configuration/source changed')
        for step,digest in hashes.items():
            wf.require(wf.sha(wf.receipt(batch,int(step)))==digest,'Stage receipt changed')
        if not audit.exists():wf.dump(audit,proof)
        tmp=state/'batch.metadata_update.next'
        if tmp.exists():wf.require(wf.strict_load(tmp)==after,'Unexpected interrupted update file')
        else:wf.dump(tmp,after)
        with tmp.open('r+b') as stream:os.fsync(stream.fileno())
        os.replace(tmp,identity)
        wf.setup(c,cp)
        print('ACTIVATED: reviewed metadata update; completed stages 01-07 preserved.',flush=True)
        print('AUDIT: '+str(audit),flush=True)
        return c,cp
    finally:
        lock.unlink(missing_ok=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=wf.PACKAGE/'configs/hillside.json')
    parser.add_argument('--previous-package',type=Path)
    parser.add_argument('--previous-config',type=Path)
    parser.add_argument('--revision',default='v3')
    parser.add_argument('--activate-only',action='store_true')
    args=parser.parse_args()
    c,cp=activate(args.config,args.previous_package,args.previous_config)
    if not args.activate_only:wf.run_stage(c,cp,8,args.revision,1)


if __name__=='__main__':main()
