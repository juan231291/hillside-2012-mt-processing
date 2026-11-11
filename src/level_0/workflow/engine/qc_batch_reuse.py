"""Import verified v1.1 stages 01-05 into an independently frozen v1.2 batch."""
import ast
from copy import deepcopy
from pathlib import Path
import shutil


def function_text(path,name):
    text=path.read_text(encoding='utf-8')
    node=next(n for n in ast.parse(text).body if isinstance(n,ast.FunctionDef) and n.name==name)
    return ast.dump(node,include_attributes=False)


def reuse_qc(wf,previous_package,previous_config,new_config):
    old,old_cp=wf.config(previous_config)
    source=Path(old['batch_dir']).resolve()
    package=previous_package.resolve(strict=True)
    identity_path=source/'state/batch.json'
    identity=wf.strict_load(identity_path)
    compatible=wf.strict_load(wf.PACKAGE/'configs/reuse_v1_1_compatibility.json')
    expected=compatible['source_code_sha256']
    wf.require(identity['code_sha256']==expected,'Unsupported source code for QC reuse')
    wf.require(wf.sha(old_cp)==identity['configuration_sha256'],'Source configuration changed')
    wf.require({n:wf.sha(package/n) for n in expected}==expected,'Previous package differs from its frozen batch')
    current=wf.code_hashes()
    wf.require(current==compatible['reviewed_target_code_sha256'],'Unreviewed target code changes')
    changed={n for n in expected if expected[n]!=current.get(n)}
    wf.require(changed==set(compatible['changed_existing_files']),'Unexpected computation changes')
    wf.require(set(current)-set(expected)=={'engine/reviewed_rates.py','engine/qc_batch_reuse.py','engine/metadata_review.py'},'Unexpected added computation modules')
    wf.require(not set(expected)-set(current),'Computation modules removed')
    # The changed compiler is also imported by Step 04. Verify its actual
    # ownership algorithm, not just a claim that the change affects Step 06.
    for name in ('read_records','apply_ownership'):
        wf.require(function_text(package/'engine/review_policy.py',name)==
                   function_text(wf.ENGINE/'review_policy.py',name),'Ownership computation changed')
    plan={k:v for k,v in identity.items() if k not in ('code_sha256','configuration_sha256')}
    wf.require(wf.preflight(old)==plan,'Source acquisition identity changed')
    c,cp=wf.config(new_config);target=Path(c['batch_dir']).resolve()
    wf.require(source!=target and source not in target.parents and target not in source.parents,
               'Reuse needs separate non-overlapping batch directories')
    ignored={'batch_dir','ownership_rules','selection_rules','workers','metadata_rules'}
    wf.require({k:v for k,v in old.items() if k not in ignored}=={k:v for k,v in c.items() if k not in ignored},
               'Acquisition configuration changed')
    wf.require(wf.preflight(c)==plan,'Raw/workbook/station identity changed')
    wf.require(wf.sha(Path(old['ownership_rules']))==wf.sha(Path(c['ownership_rules'])),'Ownership rules changed; Step 04 and QC cannot be reused')
    wf.require(not list((source/'state').glob('*.lock')),'Source batch has a running stage lock')
    stages=range(1,6)
    receipts={s:wf.validate_receipt(old,source,s) for s in stages}
    hashes={str(s):wf.sha(wf.receipt(source,s)) for s in stages}
    external={}
    for step,r in receipts.items():
        wf.require(r['stage']==step and set(r['parent_receipts'])=={str(s) for s in wf.PARENTS[step]},'Unexpected source dependency chain')
        wf.require(set(r['report_sha256'])==set(wf.REPORTS[step]),'Unexpected source report set')
        output=Path(r['output']).resolve(strict=True)
        wf.require(source in output.parents,'Source reports are outside their batch')
        wf.require(wf.sha(source/'state'/f'{step:02d}_{r["revision"]}.json')==hashes[str(step)],'Source archived receipt differs')
        if step==4:
            wf.require(r['external_inputs'].get(old['ownership_rules'])==wf.sha(Path(c['ownership_rules'])),
                       'Completed Step 04 used different ownership rules')
        for path,digest in r['external_inputs'].items():
            wf.require(wf.sha(Path(path))==digest,'Source external proof changed')
            external[path]=digest
    proof={'source_batch':str(source),'source_configuration':str(old_cp),'source_configuration_sha256':wf.sha(old_cp),
           'source_identity_sha256':wf.sha(identity_path),'stage_receipts_sha256':hashes,
           'external_inputs_sha256':external,'source_code_sha256':expected}
    marker=target/'state/reused_qc.json'
    if target.exists():
        wf.require(marker.is_file(),'Target exists without completed QC reuse; choose a fresh target')
        batch,_=wf.setup(c,cp)
        wf.require(wf.strict_load(marker)['source']==proof,'Reuse source changed since preparation')
        for s in stages:
            r=wf.validate_receipt(c,batch,s)
            wf.require(r.get('reused_from',{}).get('receipt_sha256')==hashes[str(s)],'Reused receipt provenance differs')
        print('Previously prepared stages 01-05 revalidated.',flush=True)
        return c,cp
    target.mkdir(parents=True,exist_ok=False)
    batch,frozen=wf.setup(c,cp)
    saved=batch/'state/reused_inputs';saved.mkdir()
    for path,name in ((identity_path,'source_batch.json'),(old_cp,'source_configuration.json')):
        shutil.copyfile(path,saved/name)
    copied={}
    for i,(path,digest) in enumerate(sorted(external.items())):
        dst=saved/f'external_{i:03d}_{Path(path).name}'
        shutil.copyfile(path,dst);wf.require(wf.sha(dst)==digest,'Copied external proof differs')
        copied[path]=str(dst)
    for step,old_r in receipts.items():
        output=batch/'reports'/f'{step:02d}_{wf.STEPS[step]}_v1';output.mkdir(parents=True)
        for name,digest in old_r['report_sha256'].items():
            shutil.copyfile(Path(old_r['output'])/name,output/name)
            wf.require(wf.sha(output/name)==digest,'Copied report differs: '+name)
        original=saved/f'{step:02d}_source_receipt.json'
        shutil.copyfile(wf.receipt(source,step),original)
        wf.require(wf.sha(original)==hashes[str(step)],'Source receipt changed during copy')
        extra={copied[p]:h for p,h in old_r['external_inputs'].items()}
        extra.update({str(p):wf.sha(p) for p in (original,saved/'source_batch.json',saved/'source_configuration.json')})
        if step==4:extra[c['ownership_rules']]=wf.sha(Path(c['ownership_rules']))
        r=deepcopy(old_r)
        r.update(revision='v1',created_utc=wf.now(),output=str(output),external_inputs=extra,
                 parent_receipts={str(p):wf.sha(wf.receipt(batch,p)) for p in wf.PARENTS[step]},
                 reused_from={'source_batch':str(source),'original_output':old_r['output'],
                              'receipt_sha256':hashes[str(step)],'original_created_utc':old_r['created_utc'],
                              'external_proof_copies':{p:copied[p] for p in old_r['external_inputs']},
                              'execution':'Reports copied and rehashed; source computation was not rerun.'})
        wf.dump(batch/'state'/f'{step:02d}_v1.json',r);wf.dump(wf.receipt(batch,step),r)
    wf.require(wf.code_hashes()==frozen['code_sha256'] and wf.sha(cp)==frozen['configuration_sha256'],'Target code/configuration changed during reuse')
    wf.require(wf.sha(old_cp)==proof['source_configuration_sha256'] and wf.sha(identity_path)==proof['source_identity_sha256'],'Source identity changed during reuse')
    for s in stages:
        wf.require(wf.sha(wf.receipt(source,s))==hashes[str(s)],'Source receipt changed during reuse')
        wf.validate_receipt(old,source,s)
        wf.validate_receipt(c,batch,s)
    wf.dump(marker,{'schema':'mtl0.qc_prefix_reuse/1','complete':True,'created_utc':wf.now(),
                    'stages':list(stages),'source':proof,'new_batch':str(batch),'new_code_sha256':frozen['code_sha256'],
                    'raw_payloads_read':False,'source_modified':False,
                    'compatibility':'Pinned v1.1 to v1.2 transition. Stage 01-05 algorithms, acquisition settings and ownership rules unchanged.'})
    print('Verified and copied stages 01-05 into '+str(batch),flush=True)
    return c,cp


def check_selection(wf,c,expectations):
    from collections import Counter
    from reviewed_rates import canonical_sha
    batch=Path(c['batch_dir']);r=wf.validate_receipt(c,batch,6)
    records=wf.records(Path(r['output'])/'selection_evidence.jsonl')
    groups=[s for s in records if s['record_type']=='recording_selection']
    runs=[s for s in records if s['record_type']=='run_plan']
    expected=wf.strict_load(expectations)
    counts=dict(Counter(s['action'] for s in groups))
    wf.require(counts==expected['counts'] and len(groups)==expected['recording_groups'],'Selection counts differ from the accepted review')
    wf.require(len(runs)==expected['run_count'] and len({s['station'] for s in runs})==expected['station_count'],'Run/station coverage differs from the accepted review')
    wf.require(canonical_sha(runs)==expected['run_plans_sha256'],'Run membership, timing or counts differ from the reviewed plans')
    d=Path(wf.validate_receipt(c,batch,4)['output'])
    wf.closure(d,Path(r['output']),wf.strict_load(batch/'state/batch.json')['stations'],wf.strict_load(Path(r['policy_path'])))
    print('SELECTION CHECK PASSED: '+str(len(runs))+' runs, '+str(expected['station_count'])+' stations, zero holds.',flush=True)
