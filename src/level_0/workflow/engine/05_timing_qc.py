"""Level 0 timing/sample QC for retained survey recordings.

Read existing inventory, recorder evidence and decisions. Preserve sample values
and decisions. Inspect complete EM payloads, infer ASCII rate candidates from
logged duration, and compare overlapping samples exactly when grids permit it.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
import csv
from datetime import datetime, timedelta, timezone
from fractions import Fraction
import hashlib
import json
from pathlib import Path, PurePosixPath
import stat
import sys
import time

from qc_samples import CHANNELS, inspect, compare_slice
from qc_reports import write_reports

CLOCK_FLAGS = {'log_write_precedes_filename_start', 'gps_discontinuity_between_append_writes',
               'filename_header_start_conflict', 'invalid_header_timing', 'header_read_problem'}
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024**2), b''):
            h.update(block)
    return h.hexdigest()


def jsonl(path):
    with path.open(encoding='utf-8-sig') as f:
        records = [json.loads(s) for s in f if s.strip()]
    ids = [r.get('id') for r in records]
    if None in ids or len(ids) != len(set(ids)):
        raise ValueError('Missing or repeated evidence IDs: ' + str(path))
    return records


def read_csv(path):
    with path.open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def ns(value):
    if not value:
        return None
    t = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if t.utcoffset() is None:
        raise ValueError('Timestamp lacks explicit timezone')
    d = t.astimezone(timezone.utc) - EPOCH
    return Fraction((d.days * 86400 + d.seconds) * 10**9 + d.microseconds * 1000)


def iso_ns(value):
    if value is None:
        return ''
    value = round(Fraction(value))
    seconds, fraction = divmod(value, 10**9)
    return (EPOCH + timedelta(seconds=seconds)).strftime('%Y-%m-%dT%H:%M:%S') + ('.' + f'{fraction:09d}'.rstrip('0') if fraction else '') + 'Z'


def load_inputs(inventory, decisions, logs, overrides):
    if inventory.is_dir():
        inventory = inventory / 'inventory_evidence.jsonl'
    if logs.is_dir():
        logs = logs / 'log_evidence.jsonl'
    table, de = decisions/'decision.csv', decisions/'decision_evidence.jsonl'
    paths = (inventory, table, de, logs, logs.with_name('log_review.csv'))
    hashes = {str(p.resolve()): sha(p) for p in paths}
    inv, dec, log = jsonl(inventory), jsonl(de), jsonl(logs)
    im = next(r for r in inv if r['id']=='manifest:inventory')
    dm = next(r for r in dec if r['id']=='manifest:station_decisions')
    lm = next(r for r in log if r['id']=='manifest:recorder_log_review')
    if im.get('schema') != 'mtl0.inventory.evidence/1' or not im['original_summary']['inventory_complete']:
        raise ValueError('Complete updated Step 01 inventory required')
    if dm.get('schema') != 'mtl0.station_decisions/2' or dm.get('complete') is not True:
        raise ValueError('Completed Step 04 decisions required')
    if lm.get('schema') != 'mtl0.recorder_log_review/2' or lm.get('complete') is not True:
        raise ValueError('Completed Step 03 recorder evidence required')
    if dm['decision_csv_sha256'] != sha(table) or lm['log_review_csv_sha256'] != sha(logs.with_name('log_review.csv')):
        raise ValueError('Human review table differs from its evidence; reconcile edits first')
    recorded = {PurePosixPath(p.replace('\\','/')).name:h for p,h in dm['input_sha256'].items()}
    if recorded.get('log_evidence.jsonl') != sha(logs) or recorded.get('duplicate_evidence.jsonl') != lm['comparison_sha256']:
        raise ValueError('Decision and recorder input chains differ')
    for field in ('filename_utc_offset_assumed','log_utc_offset_assumed'):
        if dm[field] != lm[field]:
            raise ValueError('Input timezone assumptions disagree')
    rows = read_csv(table)
    details = {r['group_id']:r for r in dec if r['record_type']=='station_decision'}
    gg = {r['id']:r['data'] for r in inv if r['record_type']=='recording_group'}
    ll = {r['review']['group_id']:r for r in log if r['record_type']=='group_log_review'}
    if len(rows)!=len(details) or len(rows)!=dm['source_groups'] or set(details)!=set(gg) or set(ll)!=set(gg):
        raise ValueError('Inventory/decision/recorder group coverage differs')
    if len({r['group_id'] for r in rows}) != len(rows) or dict(Counter(r['decision'] for r in rows)) != dm['counts']:
        raise ValueError('Decision totals/IDs disagree')
    human_logs = [r['review'] for r in ll.values() if r['included_in_human_review']]
    if read_csv(logs.with_name('log_review.csv')) != human_logs:
        raise ValueError('Recorder CSV/JSON rows differ')
    files = defaultdict(lambda: defaultdict(list))
    for r in inv:
        if r['record_type']=='file' and r['data']['channel_from_extension'] in CHANNELS:
            f = r['data']
            gid='group:'+'/'.join((f['source_role'],f['source_label'],str(PurePosixPath(f['relative_path']).with_suffix(''))))
            if gid not in gg:
                raise ValueError('File refers to absent group')
            files[gid][f['channel_from_extension']].append(r)
    sources = [r['data'] for r in inv if r['record_type']=='source']
    roots = {(s['role'],s['label']):Path(s['resolved_root']).resolve() for s in sources}
    original_roots = {key:str(value) for key,value in roots.items()}
    for value in overrides:
        key,sep,path=value.partition('='); role,slash,label=key.partition('/')
        if not sep or not slash or (role,label) not in roots:
            raise ValueError('Source override must match ROLE/LABEL=DIR in inventory')
        roots[role,label]=Path(path).resolve(strict=True)
    tasks=defaultdict(list)
    trial={s['label'].casefold() for s in sources if s['role']=='survey'}
    if trial != {s.casefold() for s in dm['survey_station_labels']}:
        raise ValueError('Trial target stations differ from decisions')
    byid={r['group_id']:r for r in rows}
    for row in rows:
        gid=row['group_id']; group=gg[gid]; detail=details[gid]
        if detail['decision']!=row or detail['recorder_review']!=ll[gid]['review']:
            raise ValueError('Decision or recorder row differs from evidence')
        if any(row[a]!=group[b] for a,b in [('source_folder','source_label'),('source_role','source_role'),('recording_group','group_key')]):
            raise ValueError('Source group identity differs')
        if row['decision']=='exclude_duplicate':
            retained=byid.get(row['retained_group_id'])
            if not retained or retained['decision']!='retain_for_qc' or retained['assigned_station']!=row['assigned_station']:
                raise ValueError('Duplicate retained-copy link is inconsistent')
        if row['decision']!='retain_for_qc' or row['assigned_station'].casefold() not in trial:
            continue
        if row['retained_group_id']!=gid:
            raise ValueError('Retained group does not point to itself')
        key=row['source_role'],row['source_folder']
        tasks[row['assigned_station']].append({'decision':row, 'log':ll[gid],
                  'channels':dict(files[gid]), 'root':str(roots[key]), 'original_root':original_roots[key]})
    if not tasks:
        raise ValueError('No retained groups assigned to survey stations')
    return tasks, hashes, roots, {'decision_counts':dm['counts'], 'target_stations':sorted(dm['survey_station_labels']),
        'filename_utc_offset_assumed':dm['filename_utc_offset_assumed'], 'log_utc_offset_assumed':dm['log_utc_offset_assumed']}


def source_path(record, root):
    row=record['data']; rel=PurePosixPath(row['relative_path'])
    if row['entry_type']!='regular_file' or rel.is_absolute() or '..' in rel.parts or '\\' in row['relative_path']:
        raise ValueError('Unexpected source path/type')
    path=root.joinpath(*rel.parts)
    if any(p.is_symlink() for p in (path,*path.parents) if p!=root and root in p.parents):
        raise ValueError('Nested source link requires review')
    actual=path.resolve(strict=True)
    s=actual.stat()
    if root not in actual.parents or not stat.S_ISREG(s.st_mode) or s.st_size!=int(row['bytes']):
        raise ValueError('Source path/type/size differs from inventory')
    return actual


def group_qc(bundle):
    row=bundle['decision']; log=bundle['log']; gid=row['group_id']
    root=Path(bundle['root']).resolve(strict=True)
    flags=set()
    prior_flags=set(filter(None,row['review_flags'].split(';')))
    clock=bool(prior_flags & CLOCK_FLAGS)
    if clock:
        flags.add('timing_uncertain_from_prior_evidence')
    starts=ns(row['filename_start_utc_assumed'])
    bysource=defaultdict(list)
    for e in log['write_events']:
        bysource[e['source_file_id']].append(int(e['logged_seconds']))
    durations={max(values) for values in bysource.values()}
    duration=next(iter(durations)) if len(durations)==1 and next(iter(durations))>0 else None
    if duration is None:
        flags.add('logged_duration_missing_or_conflicting')
    channels={}
    for channel in CHANNELS:
        members=bundle['channels'].get(channel,[])
        if len(members)!=1:
            flags.add('missing_or_repeated_channel')
            channels[channel]={'status':'missing_or_repeated', 'file_ids':[r['id'] for r in members], 'sample_count':None, 'segments':[]}
            continue
        record=members[0]
        try:
            path=source_path(record,root)
            result=inspect(path,channel)
            result['_path']=path
        except (OSError,ValueError) as exc:
            result={'status':'error', 'error':str(exc), 'sample_count':None, 'segments':[], 'flags':['source_read_error']}
        result.update(file_id=record['id'], relative_path=record['data']['relative_path'])
        if result['status']!='ok':
            flags.add(channel+'_unusable_or_empty')
        else:
            if result['format']=='numeric_ASCII':
                if duration and starts is not None:
                    rate=Fraction(result['sample_count'],duration)
                    step=Fraction(10**9)/rate
                    result['segments']=[{'sample_offset':0,'samples':result['sample_count'],
                        'start_ns':str(starts),'step_ns':str(step),'end_exclusive_ns':str(starts+duration*10**9),
                        'rate_hz':str(rate),'rate_basis':'sample_count_over_logged_duration_estimate',
                        'time_basis':'filename_start_with_recorded_utc_offset_assumption'}]
                else:
                    flags.add('ascii_rate_not_estimated')
            for segment in result['segments']:
                segment['timing_uncertain']=clock
            flags.update(result['flags'])
        channels[channel]=result
    counts={c['sample_count'] for c in channels.values() if c.get('sample_count') is not None}
    if len(counts)>1:
        flags.add('channel_sample_counts_differ')
    good=[c for c in channels.values() if c['status']=='ok']
    # Compare complete coverage signatures, allowing adjacent traces with changed IDs.
    def coverage(c):
        merged=[]
        for s in c['segments']:
            a,step,b=Fraction(s['start_ns']),Fraction(s['step_ns']),Fraction(s['end_exclusive_ns'])
            if merged and merged[-1][1]==step and merged[-1][2]==a:
                merged[-1]=(merged[-1][0],step,b)
            else:
                merged.append((a,step,b))
        return tuple(merged)
    if len(good)==5 and len({coverage(c) for c in good})>1:
        flags.add('channel_time_or_rate_alignment_differs')
    rates=sorted({s['rate_hz'] for c in good for s in c['segments']},key=Fraction)
    inferred=any(c.get('format')=='numeric_ASCII' for c in good)
    if len(rates)>1:
        flags.add('multiple_channel_or_trace_rates')
    if any(c.get('format')=='MiniSEED2' for c in good) and duration:
        for c in good:
            if c.get('format')=='MiniSEED2':
                seconds=sum(Fraction(s['samples'],1)*Fraction(s['step_ns'])/10**9 for s in c['segments'])
                if seconds!=duration:
                    flags.add('header_duration_differs_from_logged_seconds')
    return {'record_type':'recording_qc','id':'qc:'+gid.removeprefix('group:'),'group_id':gid,
        'station':row['assigned_station'],'source_folder':row['source_folder'],'recording_group':row['recording_group'],
        'original_root':bundle['original_root'],'source_root_used':bundle['root'],
        'decision':row['decision'],'prior_review_flags':sorted(prior_flags),'flags':sorted(flags),
        'clock_uncertain':clock,'logged_max_seconds_consensus':duration,'channels':channels,
        'rates_hz':rates,'has_inferred_ascii_rate':inferred,'ascii_rate_corroborated_by_neighbour':False}


def timelines(groups,max_overlap_samples):
    overlaps, gaps, corroborated, links = [], [], set(), defaultdict(set)
    rate_changes=defaultdict(set)
    bychannel=defaultdict(list)
    for group in groups:
        for channel,c in group['channels'].items():
            if c['status']!='ok':
                continue
            for index,s in enumerate(c['segments']):
                bychannel[channel].append((Fraction(s['start_ns']),Fraction(s['end_exclusive_ns']),group,c,index,s))
    for channel,segments in bychannel.items():
        segments.sort(key=lambda x:(x[0],x[1],x[2]['group_id'],x[4]))
        active=[]
        trusted_end=None; previous=None
        for item in segments:
            start,end,group,c,index,s=item
            active=[a for a in active if a[1]>start]
            for left in active:
                a,b,lg,lc,li,ls=left
                begin,finish=max(a,start),min(b,end)
                relation={'record_type':'overlap_comparison','station':group['station'], 'channel':channel,
                    'left_group_id':lg['group_id'],'right_group_id':group['group_id'],
                    'left_file_id':lc['file_id'],'right_file_id':c['file_id'],
                    'left_segment_index':li,'right_segment_index':index,
                    'start_ns':str(begin),'end_exclusive_ns':str(finish),
                    'start_utc':iso_ns(begin),'end_exclusive_utc':iso_ns(finish),
                    'duration_seconds':str((finish-begin)/10**9)}
                lstep,rstep=Fraction(ls['step_ns']),Fraction(s['step_ns'])
                if lg['clock_uncertain'] or group['clock_uncertain']:
                    comparison={'status':'not_compared_uncertain_timing'}
                elif lstep!=rstep:
                    comparison={'status':'not_compared_different_rates'}
                else:
                    ix=(begin-a)/lstep; iy=(begin-start)/rstep; n=(finish-begin)/lstep
                    if any(v.denominator!=1 for v in (ix,iy,n)):
                        comparison={'status':'not_compared_unaligned_sample_grids'}
                    else:
                        comparison=compare_slice(lc,c,ls['sample_offset']+int(ix),s['sample_offset']+int(iy),int(n),max_overlap_samples)
                relation.update(comparison)
                relation['id']='overlap:'+str(len(overlaps)+1)+':'+group['station']
                overlaps.append(relation)
                links[lg['group_id']].add(relation['id']);links[group['group_id']].add(relation['id'])
            active.append(item)
            if group['clock_uncertain']:
                continue
            if trusted_end is not None:
                if previous[2]['group_id']!=group['group_id'] and previous[5]['rate_hz']!=s['rate_hz']:
                    rate_changes[group['group_id']].add(previous[2]['group_id'])
                    rate_changes[previous[2]['group_id']].add(group['group_id'])
                if start>trusted_end:
                    gaps.append({'record_type':'reported_interval_gap','id':f'gap:{group["station"]}:{len(gaps)+1}',
                        'station':group['station'],'channel':channel,'left_group_id':previous[2]['group_id'],
                        'right_group_id':group['group_id'],'start_utc':iso_ns(trusted_end),'end_exclusive_utc':iso_ns(start),
                        'duration_seconds':str((start-trusted_end)/10**9),
                        'meaning':'Gap between reported/candidate intervals; does not prove no other recoverable data exists.'})
                if start==trusted_end and previous[5]['rate_hz']==s['rate_hz'] and previous[2]['group_id']!=group['group_id']:
                    corroborated.add((previous[2]['group_id'],channel)); corroborated.add((group['group_id'],channel))
            if trusted_end is None or end>trusted_end:
                trusted_end=end; previous=item
    for g in groups:
        gid=g['group_id']; flags=set(g['flags'])
        g['overlap_comparison_ids']=sorted(links[gid])
        if links[gid]:
            flags.add('overlapping_reported_intervals')
        g['gap_before_ids']=[r['id'] for r in gaps if r['right_group_id']==gid]
        g['gap_after_ids']=[r['id'] for r in gaps if r['left_group_id']==gid]
        if g['gap_before_ids'] or g['gap_after_ids']:
            flags.add('gap_between_reported_intervals')
        g['rate_change_neighbour_groups']=sorted(rate_changes[gid])
        if rate_changes[gid]:
            flags.add('rate_differs_from_neighbouring_interval')
        g['ascii_rate_corroborated_by_neighbour']=g['has_inferred_ascii_rate'] and all((gid,c) in corroborated for c in CHANNELS)
        if g['has_inferred_ascii_rate'] and not g['ascii_rate_corroborated_by_neighbour']:
            flags.add('ascii_rate_estimate_needs_review')
        g['flags']=sorted(flags)
    return overlaps,gaps


def station_qc(station,bundles,max_overlap_samples):
    start=time.monotonic(); groups=[]
    print(f'{station}: scanning {len(bundles)} retained recording groups',flush=True)
    for i,bundle in enumerate(bundles,1):
        groups.append(group_qc(bundle))
        if i%25==0 or i==len(bundles):
            print(f'{station}: scanned {i}/{len(bundles)} groups',flush=True)
    overlaps,gaps=timelines(groups,max_overlap_samples)
    # Full-sequence candidates supplement timing overlaps. Equality alone does
    # not establish a duplicate; constant/repeated signals can be legitimate.
    signatures=defaultdict(list)
    for g in groups:
        if all(g['channels'][c].get('sample_hash_encoding')=='signed_int64_little_endian_values_v1' for c in CHANNELS):
            sig=tuple((g['channels'][c]['sample_count'],g['channels'][c]['sample_sha256']) for c in CHANNELS)
            signatures[sig].append(g)
    repeats=[]
    for gg in signatures.values():
        if len(gg)>1:
            repeats.append({'record_type':'repeated_sample_sequence','id':f'repeated:{station}:{len(repeats)+1}',
                'station':station,'group_ids':[g['group_id'] for g in gg],
                'evidence':'All five channels have equal sample counts and matching int64 sample-sequence SHA256 values; review timing before selecting a copy.'})
            for g in gg:
                g['flags']=sorted(set(g['flags'])|{'repeated_five_channel_sample_sequence'})
    for g in groups:
        for c in g['channels'].values():
            c.pop('_path',None)
        g['review_required']=bool(g['flags'])
    files=[c for g in groups for c in g['channels'].values()]
    summary={'record_type':'station_qc_summary','id':'summary:'+station,'station':station,
        'groups_scanned':len(groups),'groups_requiring_review':sum(g['review_required'] for g in groups),
        'channels_ok':sum(c['status']=='ok' for c in files),'channels_empty':sum(c['status']=='empty' for c in files),
        'channels_with_errors_or_missing':sum(c['status'] in ('error','missing_or_repeated') for c in files),
        'overlap_channel_comparisons':dict(Counter(r['status'] for r in overlaps)),
        'gap_channel_intervals':len(gaps),'repeated_sequence_sets':len(repeats),
        'observed_rates_hz':sorted({r for g in groups for r in g['rates_hz']},key=Fraction),
        'groups_with_header_rates':sum(any(c.get('format')=='MiniSEED2' and c['status']=='ok' for c in g['channels'].values()) for g in groups),
        'groups_with_ascii_rate_estimates':sum(g['has_inferred_ascii_rate'] for g in groups),
        'elapsed_seconds':round(time.monotonic()-start,3)}
    print(station+': QC complete; review groups '+str(summary['groups_requiring_review']),flush=True)
    return summary,groups,overlaps,gaps,repeats


def run(inventory,decisions,logs,out,workers=2,overrides=(),max_overlap_samples=2_000_000):
    if out.exists():
        raise FileExistsError('Output already exists: '+str(out))
    if workers<1 or max_overlap_samples<1:
        raise ValueError('Workers and overlap sample limit must be positive')
    tasks,hashes,roots,context=load_inputs(inventory,decisions,logs,overrides)
    if any(out.resolve()==r or r in out.resolve().parents for r in roots.values()):
        raise ValueError('Reports must be outside source folders')
    workers=min(workers,len(tasks))
    from parallel_jobs import map_jobs
    results=map_jobs(station_qc,[(station,bundles,max_overlap_samples) for station,bundles in sorted(tasks.items())],workers)
    from survey_qc import add_global_sequence_review
    add_global_sequence_review(results)
    for name,expected in hashes.items():
        if sha(Path(name))!=expected:
            raise ValueError('An input report changed during QC')
    out.mkdir(parents=True,exist_ok=False)
    import numpy,obspy,openpyxl
    manifest={'record_type':'qc_manifest','id':'manifest:timing_qc','schema':'mtl0.timing_qc/1',
        'created_utc':datetime.now(timezone.utc).isoformat(),'input_sha256':hashes,
        'code_sha256':{p.name:sha(p) for p in (Path(__file__),Path(__file__).with_name('qc_samples.py'),Path(__file__).with_name('qc_reports.py'))},
        'context':context,'workers':workers,'max_overlap_samples_per_channel':max_overlap_samples,
        'station_count':len(results),'groups_scanned':sum(r[0]['groups_scanned'] for r in results),
        'review_complete':True,'all_recordings_approved':False,'decisions_modified':False,'raw_data_modified':False,
        'python':sys.version.split()[0],'numpy':numpy.__version__,'obspy':obspy.__version__,'openpyxl':openpyxl.__version__,
        'rate_policy':'MiniSEED rates come from decoded headers. ASCII candidates are sample count / consensus maximum logged seconds. Neighbour agreement is corroboration, not independent rate confirmation. No rate is inferred from spacing alone or inherited from an undated configuration snapshot.',
        'timing_policy':'Clock-conflicted intervals stay flagged and are excluded from gap/adjacency corroboration. They may appear as uncertain overlap candidates; no timestamps are corrected. Rational nanosecond intervals are retained; UTC display is rounded to nearest ns.',
        'scope':'Retained groups assigned to survey stations only. Reference/excluded groups are not scanned. Full five-channel sample hashes find int64 sequence candidates; timeline overlaps are compared exactly up to the stated limit. No calibration, rotation, resampling, concatenation or metadata transformation.'}
    write_reports(out,manifest,results)
    print('SUCCESS: QC reports written; findings require run-selection review. Output:',out,flush=True)
    return 0


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--inventory',type=Path,required=True)
    p.add_argument('--decisions',type=Path,required=True)
    p.add_argument('--logs',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--workers',type=int,default=2)
    p.add_argument('--source-root',action='append',default=[])
    p.add_argument('--max-overlap-samples',type=int,default=2_000_000)
    a=p.parse_args()
    raise SystemExit(run(a.inventory,a.decisions,a.logs,a.out,a.workers,a.source_root,a.max_overlap_samples))
