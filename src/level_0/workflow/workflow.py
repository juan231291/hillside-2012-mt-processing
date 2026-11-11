#!/usr/bin/env python3
"""Reproducible Level 0 workflow. Each stage has immutable, versioned outputs."""
import argparse
from collections import Counter
from datetime import datetime,timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import sys

PACKAGE=Path(__file__).resolve().parent
ENGINE=PACKAGE/'engine'
sys.path.insert(0,str(ENGINE))
from station_metadata_json import strict_load,sha,require

STEPS={1:'inventory',2:'duplicates',3:'recorder_logs',4:'decisions',5:'timing_qc',6:'run_selection',
       7:'ascii',8:'excel_metadata',9:'json_metadata',10:'mth5'}
MODULES={1:'01_inventory',2:'02_compare_duplicates',3:'03_inspect_recorder_logs',4:'04_review_decisions',
         5:'05_timing_qc',6:'06_select_runs',7:'07_concatenate',8:'08_prepare_metadata',9:'09_excel_to_json',10:'10_create_mth5'}
PARENTS={1:[],2:[1],3:[2],4:[2,3],5:[1,3,4],6:[4,5],7:[4,5,6],8:[4,5,6,7],9:[4,5,6,7,8],10:[4,6,7,8,9]}
REPORTS={1:['inventory_review.xlsx','inventory_evidence.jsonl'],2:['duplicate_review.xlsx','duplicate_evidence.jsonl'],
         3:['log_review.csv','log_evidence.jsonl'],4:['decision.csv','decision_evidence.jsonl'],
         5:['qc_review.xlsx','qc_evidence.jsonl'],6:['selection_review.xlsx','selection_evidence.jsonl'],
         7:['export_review.xlsx','export_evidence.jsonl'],8:['Level0_metadata.xlsx','metadata_changes.jsonl'],
         9:['manifest.json'],10:['manifest.json']}


def dump(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x',encoding='utf-8',newline='\n') as f:json.dump(data,f,indent=2,allow_nan=False,ensure_ascii=False);f.write('\n')


def now():return datetime.now(timezone.utc).isoformat()


def code_hashes():
    return {p.relative_to(PACKAGE).as_posix():sha(p) for p in sorted([PACKAGE/'workflow.py',*ENGINE.glob('*.py')])}


def config(path):
    path=path.resolve(); c=strict_load(path)
    require(c['schema']=='mtl0.survey_configuration/1','Unknown configuration schema')
    allowed={'schema','survey_id','input_profile','raw_root','metadata_workbook','batch_dir','selection_rules','ownership_rules',
             'workers','stations','station_aliases','filename_utc_offset','log_utc_offset','limits',
             'expected_metadata_sha256','expected_station_count','metadata_rules'}
    require(not set(c)-allowed,'Unknown configuration keys: '+str(sorted(set(c)-allowed)))
    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*',c['survey_id']) is not None,'Unsafe survey ID')
    for k in ('raw_root','metadata_workbook','batch_dir','selection_rules','ownership_rules'):
        p=Path(c[k]);c[k]=str((p if p.is_absolute() else path.parent/p).resolve())
    if c.get('metadata_rules') is not None:
        p=Path(c['metadata_rules']);c['metadata_rules']=str((p if p.is_absolute() else path.parent/p).resolve())
    require(c.get('input_profile')=='earthdata_channels_v1','Only the documented channel/log profile is supported')
    require(type(c['workers']) is int and c['workers']>0,'workers must be a positive integer')
    selected=c.get('stations','ALL')
    require(selected=='ALL' or isinstance(selected,list) and selected and
            all(isinstance(s,str) and s for s in selected),'stations must be ALL or a nonempty list of station names')
    if isinstance(selected,list):
        require(len({s.casefold() for s in selected})==len(selected),'Repeated requested stations')
    limits=c.get('limits',{})
    require(isinstance(limits,dict) and not set(limits)-{'preview_bytes','miniseed_bytes','miniseed_samples','overlap_samples'},
            'Unknown resource limit key')
    require(all(type(v) is int and v>0 for v in limits.values()),'Resource limits must be positive integers')
    count=c.get('expected_station_count')
    require(count is None or type(count) is int and count>0,'expected_station_count must be positive or null')
    from workflow_common import offset
    offset(c['filename_utc_offset']);offset(c['log_utc_offset'])
    aliases=c.get('station_aliases',{})
    require(len({k.casefold() for k in aliases})==len(aliases),'Repeated case-insensitive aliases')
    require(all(re.fullmatch('[A-Za-z0-9_-]+',v) for v in aliases.values()),'Unsafe alias target')
    # A -> B -> C would make the result depend on how often normalization ran.
    require(not any(v.casefold() in {k.casefold() for k in aliases} and
                    {k.casefold():x for k,x in aliases.items()}[v.casefold()]!=v for v in aliases.values()),'Alias chains are unsupported')
    os.environ['MTL0_CONFIG']=str(path)
    return c,path


def preflight(c):
    from workflow_common import load_metadata
    from survey_settings import canonical_station
    raw=Path(c['raw_root']); original=Path(c['metadata_workbook']);batch=Path(c['batch_dir'])
    require(raw.is_dir() and original.is_file(),'Raw directory or original workbook is missing')
    require(batch!=raw and raw not in batch.parents and batch!=original.parent,'Batch outputs must be outside the input trees')
    metadata=load_metadata(original)
    expected=c.get('expected_metadata_sha256')
    require(not expected or sha(original)==expected,'Original metadata differs from configured checksum')
    folders={}; physical=[]
    for p in sorted(raw.iterdir()):
        if not p.is_dir():continue
        label=canonical_station(p.name); key=label.casefold()
        require(key not in folders,'Source folder collision after normalization: '+label)
        resolved=p.resolve(strict=True)
        require(not any(resolved==q or resolved in q.parents or q in resolved.parents for q in physical),'Overlapping raw sources')
        require(batch!=resolved and resolved not in batch.parents,'Output falls inside a source link target')
        physical.append(resolved);folders[key]={'label':label,'path':str(p),'resolved':str(resolved)}
    selected=c.get('stations','ALL')
    keys=set(metadata) if selected=='ALL' else {canonical_station(s).casefold() for s in selected}
    require(keys<=set(metadata),'Requested station absent from metadata')
    require(all(len(metadata[k]['windows'])==1 for k in keys),'This input profile needs one source metadata row per canonical station/deployment')
    require(keys<=set(folders),'Raw folders missing for: '+', '.join(sorted(keys-set(folders))))
    require(set(folders)<=set(metadata),'Unidentified source folders need explicit aliases/metadata: '+', '.join(sorted(set(folders)-set(metadata))))
    sources=[folders[k] for k in sorted(keys)]
    if c.get('expected_station_count') is not None:require(len(sources)==c['expected_station_count'],'Unexpected station count')
    # Labels may differ by case from the original workbook. Outputs follow the
    # explicit alias or raw-folder spelling, consistently across all stages.
    require(all(metadata[k]['station'].casefold()==folders[k]['label'].casefold() for k in keys),'Metadata/source naming differs')
    return {'survey_id':c['survey_id'],'metadata_sha256':sha(original),'sources':sources,
            'stations':[s['label'] for s in sources],'source_profile':c['input_profile'],
            'filename_utc_offset_assumed':c['filename_utc_offset'],'log_utc_offset_assumed':c['log_utc_offset']}


def setup(c,config_path):
    plan=preflight(c);batch=Path(c['batch_dir']);state=batch/'state'
    frozen={**plan,'configuration_sha256':sha(config_path),'code_sha256':code_hashes()}
    identity=state/'batch.json'
    if identity.exists():require(strict_load(identity)==frozen,'Inputs/configuration/code changed: use a new batch directory')
    else:dump(identity,frozen)
    return batch,frozen


def receipt(batch,step):return batch/'state'/f'{step:02d}.json'


def validate_receipt(c,batch,step,seen=None):
    seen=set() if seen is None else seen
    if step in seen:return strict_load(receipt(batch,step))
    seen.add(step)
    p=receipt(batch,step);require(p.is_file(),f'Step {step:02d} has not completed')
    r=strict_load(p);require(r['complete'] is True,'Incomplete step receipt')
    for parent,digest in r['parent_receipts'].items():
        validate_receipt(c,batch,int(parent),seen)
        require(sha(receipt(batch,int(parent)))==digest,f'Step {step:02d} is stale; rerun after changed inputs')
    for name,digest in r['external_inputs'].items():require(sha(Path(name))==digest,'Reviewed rules changed; rerun their owning stage')
    for name,digest in r['report_sha256'].items():require(sha(Path(r['output'])/name)==digest,'Report modified or missing: '+name)
    return r


def records(path):
    with path.open(encoding='utf-8') as f:return [json.loads(line) for line in f if line.strip()]


def closure(decisions,selection,stations,policy):
    dd=records(decisions/'decision_evidence.jsonl')
    pending=[r['group_id'] for r in dd if r['record_type']=='station_decision' and r['decision']['decision']=='unresolved']
    rr=records(selection/'selection_evidence.jsonl')
    held=[r['group_id'] for r in rr if r['record_type']=='recording_selection' and r['action']=='hold_for_review']
    actual={r['station'] for r in rr if r['record_type']=='run_plan'}
    expected=set(stations)-set(policy.get('excluded_stations',{}))
    require(not pending,f'Export blocked: {len(pending)} unresolved station/copy decisions. Review Step 04.')
    require(not held,f'Export blocked: {len(held)} held recordings. Review Step 06.')
    require(actual==expected,'Export station coverage differs from requested scope and explicit exclusions: '+str(sorted(expected-actual)))


def run_stage(c,config_path,step,revision,workers):
    require(re.fullmatch('[A-Za-z0-9_-]+',revision) is not None,'Unsafe revision')
    batch,frozen=setup(c,config_path)
    parent={s:validate_receipt(c,batch,s) for s in PARENTS[step]}
    inputs={str(s):sha(receipt(batch,s)) for s in parent}
    extra={}
    for k in (['ownership_rules'] if step==4 else ['selection_rules'] if step==6 else
              ['metadata_rules'] if step==8 and c.get('metadata_rules') is not None else []):
        extra[c[k]]=sha(Path(c[k]))
    root=batch/('level0' if step in (7,10) else 'metadata' if step in (8,9) else 'reports')
    out=root/f'{step:02d}_{STEPS[step]}_{revision}'
    require(not out.exists(),'Output revision exists. Choose a new --revision; earlier results are never overwritten')
    parents=lambda s:Path(parent[s]['output'])
    policy=Path(parent[6]['policy_path']) if 6 in parent else None
    if step in (7,8,9,10):closure(parents(4),parents(6),frozen['stations'],strict_load(policy))
    engine=importlib.import_module(MODULES[step])
    original=Path(c['metadata_workbook'])
    os.environ['MTL0_WORKERS']=str(workers)
    limits=c.get('limits',{})
    print(f"STEP {step:02d}: {STEPS[step]}, survey {c['survey_id']}, {len(frozen['stations'])} stations",flush=True)
    lock=batch/'state'/f'{step:02d}.lock'
    dump(lock,{'created_utc':now(),'pid':os.getpid(),'job_id':os.getenv('PBS_JOBID'),'output':str(out)})
    try:
        if step==1:
            result=engine.inventory([Path(s['path']) for s in frozen['sources']],[],out,
                    limits.get('preview_bytes',4096),limits.get('miniseed_bytes',64*1024**2))
            require(result['inventory_complete'],'Inventory incomplete; see its report')
        elif step==2:
            require(engine.run(parents(1),out,limits.get('miniseed_bytes',64*1024**2),limits.get('miniseed_samples',16000000))==0,'Duplicate comparison has file problems')
        elif step==3:
            require(engine.run(parents(2)/'duplicate_evidence.jsonl',original,out,log_offset=c['log_utc_offset'],filename_offset=c['filename_utc_offset'])==0,
                    'Recorder log inspection has read errors; see its report')
        elif step==4:
            engine.run(parents(2)/'duplicate_evidence.jsonl',original,parents(3),out,c['filename_utc_offset'])
            from review_policy import apply_ownership
            apply_ownership(out,strict_load(Path(c['ownership_rules'])),parents(3))
        elif step==5:
            engine.run(parents(1),parents(4),parents(3),out,workers=workers,max_overlap_samples=limits.get('overlap_samples',2000000))
        elif step==6:
            from review_policy import selection_policy
            rules=strict_load(Path(c['selection_rules']))
            policy=batch/'policies'/f'selection_{revision}.json'
            compiled=selection_policy(parents(5),parents(4),rules,frozen['stations'])
            dump(policy,compiled)
            engine.run(parents(5),parents(4),policy,out)
        elif step==7:
            engine.run(parents(6),parents(5),parents(4),policy,ENGINE/'06_select_runs.py',out,workers=workers)
        elif step==8:
            engine.run(original,parents(7),parents(6),parents(5),parents(4),policy,ENGINE/'06_select_runs.py',ENGINE/'07_concatenate.py',out)
        elif step==9:
            engine.run(parents(8),original,parents(7),parents(6),parents(5),parents(4),policy,
                       ENGINE/'06_select_runs.py',ENGINE/'07_concatenate.py',ENGINE/'08_prepare_metadata.py',out)
        elif step==10:
            engine.main(parents(9),parents(7),out,workers)
        require(sha(config_path)==frozen['configuration_sha256'] and code_hashes()==frozen['code_sha256'],'Configuration/code changed during execution')
        require(all(sha(Path(p))==h for p,h in extra.items()),'Rules changed during execution')
        require(all(sha(receipt(batch,int(s)))==h for s,h in inputs.items()),'Parent stage changed during execution')
        names=list(REPORTS[step])
        if step==9:names += [s['file'] for s in strict_load(out/'manifest.json')['stations']]
        r={'schema':'mtl0.stage_receipt/1','complete':True,'stage':step,'revision':revision,'created_utc':now(),
           'output':str(out),'parent_receipts':inputs,'external_inputs':extra,'workers':workers,
           'report_sha256':{name:sha(out/name) for name in names}}
        if step==6:r['policy_path']=str(policy);r['report_sha256']['../../policies/'+policy.name]=sha(policy)
        archived=batch/'state'/f'{step:02d}_{revision}.json';dump(archived,r)
        # Atomic pointer update; the immutable receipt above remains available.
        if step==10:release(batch,frozen,r,parent)
        tmp=batch/'state'/f'{step:02d}_{revision}.next';dump(tmp,r);os.replace(tmp,receipt(batch,step))
        print(f'COMPLETE step {step:02d}: {out}',flush=True)
    finally:lock.unlink(missing_ok=True)


def release(batch,frozen,last,parent):
    output=Path(last['output']); m=strict_load(output/'manifest.json')
    metadata=Path(parent[9]['output']); jm=strict_load(metadata/'manifest.json')
    ascii_dir=Path(parent[7]['output']); rr=records(ascii_dir/'export_evidence.jsonl')
    runs={(r['station'],r['run_id']):r for r in rr if r['record_type']=='verified_ascii_run'}
    require(m['complete'] and m['channel_storage_verified'],'MTH5 final verification incomplete')
    require(m['input_json_manifest_sha256']==sha(metadata/'manifest.json'),'MTH5 input manifest differs')
    require({s['station'] for s in m['stations']}=={s['station'] for s in jm['stations']},'MTH5/JSON station sets differ')
    count=0
    for station in m['stations']:
        require(station['json_sha256']==sha(metadata/(station['station']+'.json')),'Embedded JSON source differs')
        require(all(station[f] for f in ('all_samples_verified','native_metadata_verified','embedded_json_verified','channel_storage_verified')),'Station verification failed')
        require((output/station['file']).stat().st_size==station['bytes'],'MTH5 file size changed')
        for run in station['runs']:
            proof=runs[station['station'],run['run_id']]
            require(set(run['channels'])==set(proof['sample_sha256_by_channel']),'Channel set differs')
            for c,info in run['channels'].items():
                require(info['sample_sha256']==proof['sample_sha256_by_channel'][c] and info['storage_verified'],'Sample hash/compression differs')
                count+=1
    require(count==m['channel_instances'] and len(runs)==m['run_count'],'Release counts differ')
    manifest={'schema':'mtl0.release/1','complete':True,'created_utc':now(),'survey_id':frozen['survey_id'],
              'data_level':0,'station_count':m['station_count'],'run_count':m['run_count'],'channel_instances':count,
              'configuration_and_source_identity':frozen,'mth5_manifest_sha256':sha(output/'manifest.json'),
              'mth5_directory':str(output),'ascii_directory':str(ascii_dir),'json_directory':str(metadata),
              'excel_directory':parent[8]['output'],'stage_receipts':{**{str(s):sha(receipt(batch,s)) for s in range(1,10)},
                   '10':sha(batch/'state'/('10_'+last['revision']+'.json'))},
              'verification':'ASCII and MTH5 full readback was performed by their stages; all output channel hashes agree. Stored gzip/shuffle/Fletcher32 were checked.'}
    dump(output/'release_manifest.json',manifest)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['plan','run','status'])
    p.add_argument('--config',type=Path,default=PACKAGE/'configs/hillside.json')
    p.add_argument('--step',type=int,choices=range(1,11))
    p.add_argument('--revision',default='v1')
    p.add_argument('--workers',type=int)
    p.add_argument('--mpi',action='store_true')
    a=p.parse_args();c,cp=config(a.config)
    if a.action=='plan':
        print(json.dumps(preflight(c),indent=2));return
    if a.action=='status':
        batch,_=setup(c,cp)
        for step in STEPS:
            try:
                r=validate_receipt(c,batch,step)
                print(f'{step:02d} {STEPS[step]}: COMPLETE ({r["revision"]})')
            except (ValueError,OSError) as exc:print(f'{step:02d} {STEPS[step]}: {exc}')
        return
    require(a.step is not None,'run requires --step')
    workers=c['workers'] if a.workers is None else a.workers;require(workers>0,'Positive workers required')
    import parallel_jobs
    if a.mpi:
        rank,size=parallel_jobs.enable_mpi()
        # Import on all ranks so distributed functions have stable module names.
        importlib.import_module(MODULES[a.step])
        os.environ['MTL0_WORKERS']=str(min(workers,size))
        if rank:
            parallel_jobs.worker_loop();return
        workers=min(workers,size)
    try:run_stage(c,cp,a.step,a.revision,workers)
    finally:
        if a.mpi:parallel_jobs.stop_workers()


if __name__=='__main__':main()
