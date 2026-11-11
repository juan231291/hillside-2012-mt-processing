#!/usr/bin/env python3
"""Guarded Hillside correction: inspect, regenerate metadata, verify release."""
import argparse
import copy
from collections import Counter
from fractions import Fraction
import json
from pathlib import Path
import workflow as wf
from resume_metadata import activate

REVISION = 'v4_20260918_fix1'
CONFIG = wf.PACKAGE / 'configs/hillside.json'


def old_package(current, scripts):
    spec = wf.strict_load(wf.PACKAGE/'configs/metadata_code_update.json')
    wf.require(any(current['code_sha256'] == s['code_sha256'] for s in spec['sources']),
               'Frozen code is not a pinned Hillside release. Stop and share state/batch.json; do not edit it.')
    for cp in sorted(scripts.glob('*/configs/hillside.json')):
        package = cp.parent.parent
        if package.resolve() == wf.PACKAGE:
            continue
        if wf.sha(cp) != current['configuration_sha256']:
            continue
        actual = {p.relative_to(package).as_posix():wf.sha(p)
                  for p in sorted([package/'workflow.py', *(package/'engine').glob('*.py')])}
        if actual != current['code_sha256']:
            continue
        old, _ = wf.config(cp)
        wf.config(CONFIG)
        return package, cp
    raise ValueError('Matching original package/configuration/review not found under '+str(scripts)+
                     '. Keep the original scripts installed and share this message; do not alter the frozen state.')


def allowed_changes(previous, revised, source_comments=None):
    old = {(e['station'],e['field']):e for e in (previous or {}).get('edits',[])}
    result = []
    for edit in revised['edits']:
        key = (edit['station'],edit['field'])
        before = old[key]['value'] if key in old else edit['expected']
        if before != edit['value']:
            result.append({'station':key[0],'field':key[1], 'before':before, 'after':edit['value']})
    # Older reviewed engines joined selection notes without sentence punctuation.
    # Permit only this boundary punctuation change; note content is checked below.
    present = {(e['station'],e['field']) for e in result}
    for sid, comment in (source_comments or {}).items():
        key = (sid,'station.comments')
        if key not in present:
            value = old[key]['value'] if key in old else comment
            result.append({'station':sid,'field':'station.comments','before':value,
                           'after':value,'optional':True})
    return result


def table(path):
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=False)
    try:
        candidates = [s for s in wb if 'station.id' in [c.value for c in s[1]]]
        wf.require(len(candidates) == 1, 'Expected one metadata table')
        values = list(candidates[0].values)
        return list(values[0]), [list(r) for r in values[1:]]
    finally:
        wb.close()


def compare_rows(old_headers, old_rows, headers, rows, allowed):
    """Reject every change except a listed station/field correction."""
    wf.require(old_headers == headers, 'Metadata schema changed')
    si, ri = headers.index('station.id'), headers.index('run.id')
    def keyed(values):
        result = {(r[si], r[ri]):r for r in values}
        wf.require(len(result) == len(values), 'Repeated station/run row')
        return result
    old, new = keyed(old_rows), keyed(rows)
    wf.require(set(old) == set(new), 'Run identities or coverage changed')
    delta = {(d['station'], d['field']):d for d in allowed}
    result = []
    for key, row in new.items():
        for index, (a, b) in enumerate(zip(old[key], row)):
            if a == b and type(a) == type(b):
                continue
            field = headers[index]
            change = delta.get((key[0], field))
            wf.require(change is not None, 'Unapproved metadata change: '+str(key)+'/'+field)
            if field == 'station.comments':
                # Source text changes, but generated selection/ownership notes must survive verbatim.
                from metadata_workbook import join_comments
                before, after = change['before'], change['after']
                before = '' if before is None else str(before).strip()
                wf.require(isinstance(a, str) and isinstance(b, str) and a.startswith(before),
                           'Source comments changed unexpectedly: '+str(key))
                suffix = a[len(before):]
                wf.require(not suffix or suffix.startswith(('. ',' ')), 'Unexpected note boundary')
                tail = suffix[2:] if suffix.startswith('. ') else suffix.lstrip(' ')
                wf.require(b == join_comments(after,[tail]), 'Selection comments changed unexpectedly: '+str(key))
            else:
                wf.require(a == change['before'] and b == change['after'],
                           'Reviewed old/new value differs: '+str(key)+'/'+field)
            result.append({'station':key[0], 'run':key[1], 'field':field,
                           'before':a, 'after':b})
    observed = {(r['station'],r['field']) for r in result}
    required = {k for k,v in delta.items() if not v.get('optional')}
    wf.require(required <= observed <= set(delta), 'Some expected station corrections were not applied')
    return result


def inspect_original(scripts=None):
    c, cp = wf.config(CONFIG)
    batch = Path(c['batch_dir'])
    wf.require(c['survey_id'] == 'Hillside' and c['expected_station_count'] == 73,
               'This package is restricted to the 73-station Hillside batch')
    wf.require(not list((batch/'state').glob('*.lock')), 'A workflow job is active or left a lock')
    current = wf.strict_load(batch/'state/batch.json')
    package, old_cp = old_package(current, scripts or batch.parent/'scripts')
    c, cp = wf.config(CONFIG)
    frozen = {**wf.preflight(c), 'configuration_sha256':wf.sha(old_cp),
              'code_sha256':current['code_sha256']}
    wf.require(current == frozen, 'Acquisition inputs/configuration differ from the completed batch')
    spec = wf.strict_load(wf.PACKAGE/'configs/metadata_code_update.json')
    wf.require(wf.code_hashes() == spec['target_code_sha256'], 'Correction code changed')
    old_c, _ = wf.config(old_cp)
    c, cp = wf.config(CONFIG)
    ignored = {'metadata_rules','selection_rules','ownership_rules'}
    wf.require({k:v for k,v in old_c.items() if k not in ignored} ==
               {k:v for k,v in c.items() if k not in ignored}, 'Acquisition/output configuration changed')
    for field in ('selection_rules','ownership_rules'):
        wf.require(wf.sha(Path(c[field])) == wf.sha(Path(old_c[field])), 'Selection/ownership rules changed')
    receipts = {s:wf.validate_receipt(c,batch,s) for s in range(1,11)}
    dirs = {s:Path(r['output']) for s,r in receipts.items()}
    from metadata_inputs import validate_inputs
    from metadata_review import attach
    from metadata_workbook import build
    original = Path(c['metadata_workbook'])
    context = validate_inputs(original,dirs[7],dirs[6],dirs[5],dirs[4],
        Path(receipts[6]['policy_path']),wf.ENGINE/'06_select_runs.py',wf.ENGINE/'07_concatenate.py')
    hssl03 = sorted((r for r in context['runs'] if r['station'] == 'HSSL03'),
                   key=lambda r: Fraction(r['plan']['start_ns']))
    wf.require(hssl03 and 'ey' not in hssl03[-1]['plan']['channels'],
               'HSSL03 coverage differs from the reviewed comment; inspect before updating')
    # Reconstruct the actual previous review, which may predate author/azimuth edits.
    previous_manifest = wf.records(dirs[8]/'metadata_changes.jsonl')[0]
    previous_review = previous_manifest.get('metadata_review')
    if old_c.get('metadata_rules'):
        wf.require(previous_review == wf.strict_load(Path(old_c['metadata_rules'])),
                   'Current workbook review differs from the installed previous configuration')
    else:
        wf.require(previous_review is None, 'Unexpected previous metadata review')
    before_context = copy.deepcopy(context)
    before_context['metadata_review'] = previous_review
    before = build(original,before_context)
    old_workbook = dirs[8]/'Level0_metadata.xlsx'
    old_headers, old_rows = table(old_workbook)
    source_comments = {r['station']:r['original_values'][before['headers'].index('station.comments')]
                       for r in before['station_records']}
    try:
        # Only the documented sentence separator can differ across old engine versions.
        compare_rows(old_headers,old_rows,before['headers'],[r['values'] for r in before['rows']],
            allowed_changes(previous_review,{'edits':[]},source_comments))
    finally:
        before['source_workbook'].close()
    attach(context,original)
    built = build(original,context)
    allowed = allowed_changes(previous_review,context['metadata_review'],source_comments)
    try:
        delta = compare_rows(old_headers,old_rows,built['headers'],
            [r['values'] for r in built['rows']],allowed)
    finally:
        built['source_workbook'].close()
    manifest = wf.strict_load(dirs[10]/'manifest.json')
    wf.require(manifest['complete'] and manifest['station_count'] == 73,
               'Expected the completed 73-station MTH5 release')
    wf.require(manifest['run_count'] == len(context['runs']), 'Old MTH5 run count differs from retained exports')
    for s in (8,9,10):
        parent = batch/('level0' if s == 10 else 'metadata')
        wf.require(not (parent/f'{s:02d}_{wf.STEPS[s]}_{REVISION}').exists(),
                   'Correction output already exists; do not overwrite it')
    result = {'schema':'hillside.metadata_correction_plan/1', 'revision':REVISION,
        'previous_package':str(package), 'previous_config':str(old_cp),
        'previous_batch_identity':current,
        'stage_receipts_sha256':{str(s):wf.sha(wf.receipt(batch,s)) for s in receipts},
        'old_workbook':str(old_workbook), 'old_workbook_sha256':wf.sha(old_workbook),
        'old_mth5_manifest':str(dirs[10]/'manifest.json'),
        'old_mth5_manifest_sha256':wf.sha(dirs[10]/'manifest.json'),
        'station_count':manifest['station_count'], 'run_count':manifest['run_count'],
        'changed_cells':len(delta), 'changes_by_field':dict(Counter(d['field'] for d in delta)),
        'allowed_changes':allowed, 'changes':delta}
    print(json.dumps({k:v for k,v in result.items() if k not in
        ('changes','allowed_changes','previous_batch_identity','stage_receipts_sha256')},indent=2))
    print('CHECK PASSED: only the listed metadata fields change; all run fields and accepted azimuths are preserved.')
    return result


def inspect(scripts=None):
    from repair_plan import inspect_repair
    return inspect_repair(CONFIG,REVISION,compare_rows)


def plan_path(batch):
    return batch/'reports/hillside_metadata_update_20260918_fix1/plan.json'


def check_plan(batch, plan):
    wf.require(plan['revision'] == REVISION, 'Unexpected correction revision')
    for step, digest in plan['stage_receipts_sha256'].items():
        if int(step) <= 7:
            wf.require(wf.sha(wf.receipt(batch,int(step))) == digest, 'A retained processing stage changed')
    for key in ('old_workbook','old_mth5_manifest'):
        wf.require(wf.sha(Path(plan[key])) == plan[key+'_sha256'], 'A previous release file changed')
    for name,digest in plan.get('preserved_repair_inputs',{}).items():
        wf.require(wf.sha(Path(name)) == digest, 'An original correction record changed: '+name)


def apply(scripts=None):
    c, cp = wf.config(CONFIG)
    batch = Path(c['batch_dir'])
    path = plan_path(batch)
    if path.exists():
        plan = wf.strict_load(path)
        check_plan(batch,plan)
    else:
        plan = inspect(scripts)
        wf.dump(path,plan)
    c, cp = activate(CONFIG,Path(plan['previous_package']),Path(plan['previous_config']))
    for step in (8,9):
        current = wf.strict_load(wf.receipt(batch,step))
        if current['revision'] != REVISION:
            wf.run_stage(c,cp,step,REVISION,1)
        wf.validate_receipt(c,batch,step)
        if step == 8:
            workbook = Path(wf.strict_load(wf.receipt(batch,8))['output'])/'Level0_metadata.xlsx'
            changed = compare_rows(*table(Path(plan['old_workbook'])),*table(workbook),plan['allowed_changes'])
            wf.require(changed == plan['changes'], 'Written workbook differs from the checked preview')
    check_plan(batch,plan)
    print('METADATA COMPLETE: Excel and JSON verified. The dependent MTH5 job can now run.')


def verify():
    c, cp = wf.config(CONFIG)
    batch,_ = wf.setup(c,cp)
    plan = wf.strict_load(plan_path(batch)); check_plan(batch,plan)
    receipt = wf.validate_receipt(c,batch,10)
    wf.require(receipt['revision'] == REVISION, 'The corrected MTH5 revision has not completed')
    new_dir = Path(receipt['output'])
    old = wf.strict_load(Path(plan['old_mth5_manifest']))
    new = wf.strict_load(new_dir/'manifest.json')
    for field in ('station_count','run_count','channel_instances'):
        wf.require(old[field] == new[field], 'Coverage changed: '+field)
    old_stations = {s['station']:s for s in old['stations']}
    wf.require(set(old_stations) == {s['station'] for s in new['stations']}, 'Station set changed')
    for station in new['stations']:
        # Includes channel sets, counts, full sample hashes, rates and start/end times.
        wf.require(station['runs'] == old_stations[station['station']]['runs'],
                   'Recording content or timing changed: '+station['station'])
        wf.require(all(station[k] for k in ('all_samples_verified','native_metadata_verified',
            'embedded_json_verified','channel_storage_verified')), 'Incomplete native/provenance verification')
    release = wf.strict_load(new_dir/'release_manifest.json')
    wf.require(release['complete'] and release['mth5_manifest_sha256'] == wf.sha(new_dir/'manifest.json'),
               'Release manifest does not match')
    report = {'complete':True, 'revision':REVISION, 'station_count':new['station_count'],
        'run_count':new['run_count'], 'channel_instances':new['channel_instances'],
        'all_recorded_samples_rates_channels_and_times_unchanged':True,
        'previous_release_retained':True, 'release_manifest':str(new_dir/'release_manifest.json'),
        'excel_directory':release['excel_directory'], 'json_directory':release['json_directory'],
        'mth5_directory':str(new_dir)}
    result = plan_path(batch).with_name('verification.json')
    if result.exists():
        wf.require(wf.strict_load(result) == report, 'Existing verification report differs')
    else:
        wf.dump(result,report)
    print(json.dumps(report,indent=2))
    print('CORRECTION COMPLETE: new metadata verified, recorded samples unchanged, previous release retained.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('check','apply','verify'))
    parser.add_argument('--scripts',type=Path)
    args = parser.parse_args()
    if args.action == 'check': inspect(args.scripts)
    elif args.action == 'apply': apply(args.scripts)
    else: verify()
