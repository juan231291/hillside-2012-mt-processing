"""Export the reviewed Level 0 run plan as one verified ASCII file per run."""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys

from ascii_export import CHANNELS, ENCODING, export_station, require, sha, source_key, source_path


def load_step06(path):
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location('mtl0_step06_for_export', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_plan(selection, qc, decisions, policy_path, step06_code):
    six = load_step06(step06_code)
    path = selection/'selection_evidence.jsonl'
    selection_hash = sha(path)
    evidence = six.read_jsonl(path)
    m = next(r for r in evidence if r['id']=='manifest:run_selection')
    require(m['schema']=='mtl0.run_selection/1' and m['plan_complete'] is True,
            'Completed Step 06 selection is required')
    require(m['counts'].get('hold_for_review',0)==0,
            'Selection still has held recordings. Complete the reviewed Step 06 update before export.')
    require(m['concatenated'] is False and m['raw_data_modified'] is False and m['timestamps_corrected'] is False,
            'Unexpected selection provenance')
    require(sha(selection/'selection_review.xlsx')==m['selection_review_xlsx_sha256'],
            'Selection workbook differs from the evidence')
    require(set(m['code_sha256'])=={'06_select_runs.py','selection_reports.py','reviewed_rates.py'}, 'Selection code proof is incomplete')
    for name, expected in m['code_sha256'].items():
        require(name in ('06_select_runs.py','selection_reports.py','reviewed_rates.py') and sha(step06_code.with_name(name))==expected,
                'Installed Step 06 code differs from the code that made this plan: '+name)
    qc_records, previous, groups, details, policy, hashes, qm = six.load(qc,decisions,policy_path)
    expected_hashes = six.portable_hashes(m['input_sha256'])
    require(six.portable_hashes(hashes)==expected_hashes, 'QC/decision/policy inputs differ from the selection inputs')
    require(m['policy']==policy and m['qc_input_chain_sha256']==qm['input_sha256'], 'Selection provenance chain differs')
    selections, runs = six.build_plan(groups,policy)
    for gid,s in selections.items():
        s['station_decision_id'] = details[gid]['id']
        s['overlap_evidence_ids'] = groups[gid]['overlap_comparison_ids']
    stored = [r for r in evidence if r['record_type']=='recording_selection']
    require(len(stored)==len(selections)==m['recording_groups'] and
            {r['group_id']:r for r in stored}==selections, 'Recording selection differs from the reviewed policy/QC')
    require([r for r in evidence if r['record_type']=='run_plan']==runs and len(runs)==m['run_count'],
            'Run membership, order or timing differs from the reviewed plan')
    require(dict(Counter(r['action'] for r in stored))==m['counts'], 'Selection action counts differ')
    prior = [r for r in previous if r['record_type']=='station_decision']
    require([r['data'] for r in evidence if r['record_type']=='prior_station_decision']==prior,
            'Earlier ownership/copy decisions differ')
    require([r['data'] for r in evidence if r['record_type']=='prior_qc_interval']==
            [r for r in qc_records if r['record_type'] in ('overlap_comparison','reported_interval_gap','repeated_sample_sequence')],
            'Carried QC interval evidence differs')
    require(runs and all(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*',r['station']) and
                        re.fullmatch(r'run_[0-9]+',r['run_id']) for r in runs), 'Invalid/empty run identifiers')
    require(len({(r['station'],r['run_id']) for r in runs})==len(runs), 'Repeated station/run identity')
    require(not any(s['action']=='hold_for_review' for s in selections.values()), 'Unresolved selections remain')
    selected_sources = []
    for s in selections.values():
        if s['action']!='include':
            continue
        for c in s['selected_channels']:
            info=s['qc_snapshot']['channels'][c]
            require(info['format'] in ('numeric_ASCII','MiniSEED2') and info.get('sample_hash_encoding')==ENCODING,
                    'Selected samples lack exact int64 QC hashes; extend/review the representation before exporting')
            require(info['file_id'].startswith('file:') and re.fullmatch('[0-9a-f]{64}',info['file_sha256']) and
                    re.fullmatch('[0-9a-f]{64}',info['sample_sha256']), 'Invalid source evidence identifiers/hashes')
            selected_sources.append(info)
    hashes.update({str(path.resolve()):selection_hash,
                   str((selection/'selection_review.xlsx').resolve()):m['selection_review_xlsx_sha256']})
    summary={'stations':sorted({r['station'] for r in runs}), 'run_count':len(runs),
             'included_recording_groups':sum(s['action']=='include' for s in selections.values()),
             'source_channel_files':len(selected_sources),
             'selected_source_bytes':sum(c['bytes'] for c in selected_sources),
             'sample_values':sum(r['samples_per_channel']*len(r['channels']) for r in runs)}
    return selections,runs,m,hashes,selection_hash,summary


def write_reports(out, manifest, results, report_module):
    from openpyxl import Workbook,load_workbook
    from ascii_export import iso_ns
    from fractions import Fraction
    wb=Workbook();wb.remove(wb.active)
    rows=[]
    for r in results:
        p=r['plan']
        rows.append([r['station'],r['run_id'],p['samples_per_channel'],p['rate_hz'],
                     ', '.join(c.upper() for c in p['channels']),p['start_utc_assumed'],
                     iso_ns(Fraction(p['end_exclusive_ns'])-Fraction(10**9)/Fraction(p['rate_hz'])),
                     round(r['ascii_bytes']/1024**2,2),'Verified'])
    report_module.add_sheet(wb,'Exported runs','MT survey | verified Level 0 ASCII exports',
        'One file per run; tab-separated unscaled integer sample columns. UTC remains assumed. Sources and complete output sample sequences were checked. No metadata workbook or MTH5 file is created here.',
        ['Station','Run','Samples per channel','Rate Hz','Channel order','Start UTC assumed',
         'Last sample UTC assumed','File size MiB','Readback'],
        [13,15,22,14,25,26,29,18,18],rows)
    expected={ws.title:list(ws.values) for ws in wb}
    path=out/'export_review.xlsx';wb.save(path)
    check=load_workbook(path,read_only=True,data_only=True)
    try:
        require({ws.title:list(ws.values) for ws in check}==expected,'Export workbook readback differs')
    finally:
        check.close()
    manifest['export_review_xlsx_sha256']=sha(path)
    records=[manifest,*results]
    with (out/'export_evidence.jsonl').open('x',encoding='utf-8',newline='\n') as f:
        for r in records:
            f.write(json.dumps(r,ensure_ascii=False,allow_nan=False,separators=(',',':'))+'\n')
    with (out/'export_evidence.jsonl').open(encoding='utf-8') as f:
        require([json.loads(line) for line in f]==records,'Export evidence readback differs')


def run(selection,qc,decisions,policy,step06_code,out,workers=2,chunk_rows=16384,overrides=(),validate_plan_only=False):
    require(workers>=1 and 1<=chunk_rows<=1048576,'Invalid workers or chunk size')
    selections,runs,selection_manifest,hashes,selection_hash,summary=load_plan(selection,qc,decisions,policy,step06_code)
    print('Verified selection:',json.dumps(summary,sort_keys=True),flush=True)
    if validate_plan_only:
        print('PLAN VALIDATION ONLY: no raw samples opened and no output directory created.')
        return summary
    require(out is not None and not out.exists(),'Output already exists or is missing; choose a new output directory')
    roots={}
    for s in selections.values():
        if s['action']=='include':
            g=s['qc_snapshot'];key=source_key(g)
            require(key not in roots or roots[key]==g['source_root_used'],'Source root is inconsistent')
            roots[key]=g['source_root_used']
    for text in overrides:
        key,sep,value=text.partition('=')
        require(sep and key in roots and value,'Source override must match ROLE/LABEL=DIR in the selection')
        roots[key]=value
    for key,root in roots.items():
        p=Path(root).resolve(strict=True)
        require(p.is_dir() and out.resolve()!=p and p not in out.resolve().parents,'Output must be outside raw source roots')
        roots[key]=str(p)
    # Read only path/type/size metadata here. Full byte and sample hashes are
    # checked while exporting, avoiding a separate full preliminary scan.
    for s in selections.values():
        if s['action']=='include':
            for c in s['selected_channels']:
                source_path(s['qc_snapshot'],c,roots)
    tasks=defaultdict(list)
    for r in runs:
        tasks[r['station']].append(r)
    out.mkdir(parents=True,exist_ok=False)
    workers=min(workers,len(tasks))
    from parallel_jobs import map_jobs
    jobs=[(station,rr,{gid:s for gid,s in selections.items() if s['station']==station},roots,str(out),selection_hash,chunk_rows)
          for station,rr in sorted(tasks.items())]
    results=[r for station_results in map_jobs(export_station,jobs,workers) for r in station_results]
    for name,h in hashes.items():
        require(sha(Path(name))==h,'An input report/policy changed during export')
    require({(r['station'],r['run_id']) for r in results}=={(r['station'],r['run_id']) for r in runs},
            'Not every selected run was exported')
    import ascii_export,qc_samples,selection_reports,numpy,openpyxl
    code_paths=[Path(__file__),Path(ascii_export.__file__),Path(qc_samples.__file__),step06_code,Path(selection_reports.__file__)]
    manifest={'record_type':'ascii_export_manifest','id':'manifest:ascii_export','schema':'mtl0.ascii_export/1',
        'created_utc':datetime.now(timezone.utc).isoformat(),'complete':True,'all_output_values_verified':True,
        'selection_evidence_sha256':selection_hash,'input_sha256':hashes,
        'code_sha256':{p.name:sha(p) for p in code_paths},'source_roots_used':roots,
        'selection_counts':selection_manifest['counts'],'summary':summary,'workers':workers,'chunk_rows':chunk_rows,
        'format':'One tab-separated ASCII file per run; commented header; native unscaled int64 sample values.',
        'source_files_modified':False,'metadata_modified':False,'mth5_created':False,
        'timestamps_corrected':False,'resampling_applied':False,'calibration_applied':False,'rotation_applied':False,
        'verification':'Each source file byte SHA256 and int64 sample-sequence SHA256 matched Step 05. Complete ASCII readback matched per-channel counts and combined sample-sequence hashes. No tolerance or float conversion.',
        'python':sys.version.split()[0],'numpy':numpy.__version__,'openpyxl':openpyxl.__version__}
    write_reports(out,manifest,results,selection_reports)
    print(f'SUCCESS: {len(results)} Level 0 ASCII runs exported and completely readback verified. Output: {out}',flush=True)
    return manifest,results


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--selection',type=Path,required=True)
    p.add_argument('--qc',type=Path,required=True)
    p.add_argument('--decisions',type=Path,required=True)
    p.add_argument('--policy',type=Path,default=Path(__file__).with_name('06_selection_policy.json'))
    p.add_argument('--step06-code',type=Path,default=Path(__file__).with_name('06_select_runs.py'))
    p.add_argument('--out',type=Path)
    p.add_argument('--workers',type=int,default=2)
    p.add_argument('--chunk-rows',type=int,default=16384)
    p.add_argument('--source-root',action='append',default=[])
    p.add_argument('--validate-plan-only',action='store_true')
    a=p.parse_args()
    run(a.selection,a.qc,a.decisions,a.policy,a.step06_code,a.out,a.workers,a.chunk_rows,a.source_root,a.validate_plan_only)
