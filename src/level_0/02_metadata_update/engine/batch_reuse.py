"""Copy verified stages 01-03 into a new batch with explicit reuse provenance.

Only the pinned v1 -> v1.1 compatibility case is supported. The computation
code for these three stages is unchanged. No raw payload is scanned here.
"""
import copy
import json
import os
from pathlib import Path
import shutil


def reuse_prefix(wf, previous_config, previous_package, new_config):
    old_c, old_cp = wf.config(previous_config)
    source = Path(old_c['batch_dir'])
    previous_package = previous_package.resolve(strict=True)
    old_identity_path = source/'state/batch.json'
    old_identity = wf.strict_load(old_identity_path)
    compatibility = wf.strict_load(wf.PACKAGE/'configs/reuse_v1_compatibility.json')
    expected_code = compatibility['source_code_sha256']
    wf.require(old_identity['code_sha256'] == expected_code, 'Unsupported source code for report reuse')
    wf.require(wf.sha(old_cp) == old_identity['configuration_sha256'], 'Source configuration changed')
    old_code = {name:wf.sha(previous_package/name) for name in expected_code}
    wf.require(old_code == expected_code, 'Source package differs from its frozen batch')
    current_code = wf.code_hashes()
    wf.require(set(current_code)-set(old_code) == {'engine/batch_reuse.py'}, 'Unreviewed new computation code')
    wf.require(set(old_code)-set(current_code) == set(), 'Source computation code removed')
    changed = {name for name in old_code if current_code.get(name) != old_code[name]}
    wf.require(changed == {'engine/review_policy.py','engine/metadata_workbook.py'}, 'Code outside the reviewed Step 04/08 changes differs')
    wf.require(wf.preflight(old_c) == {k:v for k,v in old_identity.items()
               if k not in ('configuration_sha256','code_sha256')}, 'Source identity changed')
    c, cp = wf.config(new_config)
    target = Path(c['batch_dir'])
    wf.require(source != target and source not in target.parents and target not in source.parents,
               'Reuse requires separate, non-overlapping batch directories')
    # Rule contents may evolve at their owning stages. All acquisition settings,
    # selected stations, paths and limits must remain the same for reuse.
    ignored = {'batch_dir','ownership_rules','selection_rules','workers'}
    wf.require({k:v for k,v in old_c.items() if k not in ignored} ==
               {k:v for k,v in c.items() if k not in ignored}, 'Acquisition configuration changed')
    new_plan = wf.preflight(c)
    wf.require(new_plan == {k:v for k,v in old_identity.items()
               if k not in ('configuration_sha256','code_sha256')}, 'Raw/workbook/station identity changed')
    parents = {step:wf.validate_receipt(old_c,source,step) for step in (1,2,3)}
    original_hashes = {str(step):wf.sha(wf.receipt(source,step)) for step in parents}
    for step,r in parents.items():
        wf.require(r['stage']==step and set(r['parent_receipts'])=={str(p) for p in wf.PARENTS[step]},
                   'Unexpected source stage dependency chain')
        wf.require(not r['external_inputs'], 'Unexpected reviewed inputs in stages 01-03')
        wf.require(set(r['report_sha256'])==set(wf.REPORTS[step]), 'Unexpected report set')
        out=Path(r['output']).resolve(strict=True)
        wf.require(source.resolve() in out.parents, 'Source report lies outside its batch')
        archived=source/'state'/f'{step:02d}_{r["revision"]}.json'
        wf.require(wf.sha(archived)==original_hashes[str(step)], 'Source archived receipt differs')
    source_proof = {'source_batch':str(source),'source_configuration':str(old_cp),
                    'source_configuration_sha256':wf.sha(old_cp),
                    'source_batch_identity_sha256':wf.sha(old_identity_path),
                    'source_stage_receipts_sha256':original_hashes,
                    'source_code_sha256':old_code}
    marker=target/'state/reused_prefix.json'
    if target.exists():
        wf.require(marker.is_file(), 'Target batch already exists without completed report reuse; choose a fresh target')
        batch,_=wf.setup(c,cp)
        recorded=wf.strict_load(marker)
        wf.require(recorded['source']==source_proof, 'Reuse source changed since preparation')
        for step in parents:
            r=wf.validate_receipt(c,batch,step)
            wf.require(r.get('reused_from',{}).get('receipt_sha256')==original_hashes[str(step)], 'Reused receipt provenance differs')
        print('Previously prepared stages 01-03 revalidated.',flush=True)
        return c,cp
    # Do not replace existing results. Incomplete copies remain for diagnosis.
    target.mkdir(parents=True,exist_ok=False)
    batch,frozen=wf.setup(c,cp)
    proof_dir=batch/'state/reused_inputs'
    proof_dir.mkdir(parents=True,exist_ok=False)
    shutil.copyfile(old_identity_path,proof_dir/'source_batch.json')
    shutil.copyfile(old_cp,proof_dir/'source_configuration.json')
    for step,old_r in parents.items():
        report_dir=batch/'reports'/f'{step:02d}_{wf.STEPS[step]}_v1'
        report_dir.mkdir(parents=True,exist_ok=False)
        for name,digest in old_r['report_sha256'].items():
            dst=report_dir/name
            shutil.copyfile(Path(old_r['output'])/name,dst)
            wf.require(wf.sha(dst)==digest,'Copied report differs: '+name)
        saved=proof_dir/f'{step:02d}_source_receipt.json'
        shutil.copyfile(wf.receipt(source,step),saved)
        wf.require(wf.sha(saved)==original_hashes[str(step)], 'Source receipt changed during copy')
        r=copy.deepcopy(old_r)
        r.update(revision='v1',created_utc=wf.now(),output=str(report_dir),
                 parent_receipts={str(p):wf.sha(wf.receipt(batch,p)) for p in wf.PARENTS[step]},
                 external_inputs={str(saved):wf.sha(saved),
                     str(proof_dir/'source_configuration.json'):wf.sha(proof_dir/'source_configuration.json'),
                     str(proof_dir/'source_batch.json'):wf.sha(proof_dir/'source_batch.json')},
                 reused_from={'source_batch':str(source),'original_output':old_r['output'],
                              'original_created_utc':old_r['created_utc'],
                              'receipt_sha256':original_hashes[str(step)],
                              'execution':'Reports copied and rehashed; stage computation was not rerun.',
                              'computation_code':'Original source_batch.json records the code that generated these reports.'})
        wf.dump(batch/'state'/f'{step:02d}_v1.json',r)
        wf.dump(wf.receipt(batch,step),r)
    wf.require(all(wf.sha(wf.receipt(source,int(s)))==h for s,h in original_hashes.items()), 'Source receipts changed during reuse')
    wf.require(wf.sha(old_identity_path)==source_proof['source_batch_identity_sha256'] and
               wf.sha(old_cp)==source_proof['source_configuration_sha256'], 'Source identity changed during reuse')
    wf.require(wf.code_hashes()==frozen['code_sha256'] and wf.sha(cp)==frozen['configuration_sha256'], 'New code/configuration changed during reuse')
    for step in parents:wf.validate_receipt(c,batch,step)
    wf.dump(marker,{'schema':'mtl0.prefix_reuse/1','complete':True,'created_utc':wf.now(),
                    'stages':[1,2,3],'source':source_proof,
                    'new_batch':str(batch),'new_code_sha256':frozen['code_sha256'],
                    'compatibility':'Only review_policy.py and metadata_workbook.py changed; batch_reuse.py is the added explicit import utility. Stage 01-03 algorithms and acquisition settings are unchanged.',
                    'raw_payloads_read':False,'source_modified':False})
    print('Verified and copied stages 01-03 into '+str(batch),flush=True)
    return c,cp


def check_decisions(wf,c,expectations):
    from collections import Counter
    batch=Path(c['batch_dir']);r=wf.validate_receipt(c,batch,4)
    records=wf.records(Path(r['output'])/'decision_evidence.jsonl')
    rows=[a['decision'] for a in records if a['record_type']=='station_decision']
    expected=wf.strict_load(expectations)
    counts=dict(Counter(a['decision'] for a in rows))
    wf.require(counts==expected['decision_counts'],'Decision counts differ from the reviewed expectation; inspect Step 04 before QC')
    wf.require(sum(not a['assigned_station'] for a in rows if a['decision']=='exclude_reviewed')==expected['excluded_unknown_owner_groups'],
               'Unknown-owner exclusion count differs')
    retained={a['assigned_station'] for a in rows if a['decision']=='retain_for_qc'}
    wf.require(len(retained)==expected['stations_with_retained_groups'],'Retained station coverage differs')
    print('REVIEW CHECK PASSED: '+json.dumps(counts,sort_keys=True),flush=True)
    print('Next: review reports/04_decisions_v1, then submit 05_timing_qc.sh.',flush=True)
