"""Build a Level 0 run plan from completed QC; never open raw sample files.

Selection is whole-recording only. Source hashes and exact sample ranges are
carried forward for the future concatenator to verify before reading samples.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timedelta, timezone
from fractions import Fraction
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys

from selection_reports import write_workbook

CHANNELS = ('ex', 'ey', 'bx', 'by', 'bz')
CONTEXT_FLAGS = {'gap_between_reported_intervals', 'rate_differs_from_neighbouring_interval',
                 'overlapping_reported_intervals', 'ascii_rate_estimate_needs_review',
                 'channel_sample_counts_differ'} | {c + '_unusable_or_empty' for c in CHANNELS}
RATE_BASES = {'decoded_miniseed_header', 'sample_count_over_logged_duration_estimate',
              'reviewed_workbook_and_filename_boundary_consistency'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024**2), b''):
            h.update(block)
    return h.hexdigest()


def read_jsonl(path):
    with path.open(encoding='utf-8-sig') as f:
        rows = [json.loads(s) for s in f if s.strip()]
    ids = [r.get('id') for r in rows]
    require(None not in ids and len(ids) == len(set(ids)), 'Missing/repeated JSONL IDs')
    return rows


def iso(value):
    whole, fraction = divmod(round(Fraction(value)), 10**9)
    stamp = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=whole)
    return stamp.strftime('%Y-%m-%dT%H:%M:%S') + ('.' + f'{fraction:09d}'.rstrip('0') if fraction else '') + 'Z'


def portable_hashes(mapping):
    result = {}
    for path, value in mapping.items():
        name = PurePosixPath(path.replace('\\', '/')).name
        require(name not in result, 'Ambiguous input hash basename')
        result[name] = value
    return result


def load(qc, decisions, policy_path):
    paths = [qc/'qc_evidence.jsonl', qc/'qc_review.xlsx',
             decisions/'decision.csv', decisions/'decision_evidence.jsonl', policy_path]
    hashes = {str(p.resolve()): sha(p) for p in paths}
    records, previous = read_jsonl(paths[0]), read_jsonl(paths[3])
    qm = next(r for r in records if r['id'] == 'manifest:timing_qc')
    dm = next(r for r in previous if r['id'] == 'manifest:station_decisions')
    require(qm['schema'] == 'mtl0.timing_qc/1' and qm['review_complete'] is True,
            'Completed Step 05 QC required')
    require(dm['schema'] == 'mtl0.station_decisions/2' and dm['complete'] is True,
            'Completed Step 04 decisions required')
    require(sha(paths[1]) == qm['qc_review_xlsx_sha256'], 'QC workbook differs from its evidence')
    require(sha(paths[2]) == dm['decision_csv_sha256'], 'Decision CSV differs from its evidence')
    history = portable_hashes(qm['input_sha256'])
    for p in paths[2:4]:
        require(history.get(p.name) == sha(p), 'QC used a different decision file: ' + p.name)
    old_hashes = portable_hashes(dm['input_sha256'])
    for name in ('log_evidence.jsonl', 'log_review.csv'):
        require(history.get(name) == old_hashes.get(name) and name in history,
                'QC and decisions have different recorder evidence')
    for field in ('filename_utc_offset_assumed', 'log_utc_offset_assumed'):
        require(qm['context'][field] == dm[field], 'Timezone assumptions differ')
    with paths[2].open(encoding='utf-8-sig', newline='') as f:
        table = list(csv.DictReader(f))
    details = {r['group_id']: r for r in previous if r['record_type'] == 'station_decision'}
    require(len(table) == len(details) == dm['source_groups'], 'Decision coverage differs')
    require(len({r['group_id'] for r in table}) == len(table), 'Repeated decision group')
    require(dict(Counter(r['decision'] for r in table)) == dm['counts'], 'Decision counts differ')
    for row in table:
        require(details[row['group_id']]['decision'] == row, 'Decision CSV/JSON row differs')
    targets = set(qm['context']['target_stations'])
    require(targets == set(dm['survey_station_labels']), 'Trial station scope differs')
    expected = {r['group_id'] for r in table if r['decision'] == 'retain_for_qc'
                and r['assigned_station'] in targets}
    groups = {r['group_id']: r for r in records if r['record_type'] == 'recording_qc'}
    require(set(groups) == expected and len(groups) == qm['groups_scanned'], 'QC group coverage differs')
    for gid, g in groups.items():
        d = details[gid]['decision']
        require(g['station'] == d['assigned_station'] and g['recording_group'] == d['recording_group']
                and g['source_folder'] == d['source_folder'] and g['decision'] == 'retain_for_qc',
                'QC/decision identity differs: ' + gid)
        require(set(g['prior_review_flags']) == set(filter(None, d['review_flags'].split(';'))),
                'Prior QC flags differ: ' + gid)
        require(set(g['channels']) == set(CHANNELS), 'Unexpected QC channel keys')
    policy = json.loads(policy_path.read_text(encoding='utf-8-sig'))
    require(policy['schema'] == 'mtl0.run_selection_policy/1', 'Unknown policy schema')
    require(set(policy['stations']) == targets, 'Policy must cover exactly the survey stations')
    for pair in policy.get('confirmed_overlap_pairs', []):
        require(set(pair['group_ids']) <= set(groups) and len(set(pair['group_ids'])) == 2,
                'Overlap policy names unknown/repeated groups')
        matches = [r for r in records if r['record_type'] == 'overlap_comparison'
                   and {r['left_group_id'], r['right_group_id']} == set(pair['group_ids'])
                   and r['start_utc'] == pair['start_utc'] and r['end_exclusive_utc'] == pair['end_exclusive_utc']]
        require(len(matches) == 5 and {r['channel'] for r in matches} == set(CHANNELS)
                and all(r['status'] == 'identical_samples' for r in matches),
                'Policy duplicate statement is not supported by all five overlap comparisons')
    for station, settings in policy['stations'].items():
        require(settings['output_native_rate_hz']=='any' or Fraction(settings['output_native_rate_hz']) > 0, 'Invalid selected native rate')
        require(isinstance(settings['accept_logged_ascii_rate_estimates'], bool), 'Expected boolean rate policy')
        for gid, rule in settings['exclude_startup_groups'].items():
            require(gid in groups and groups[gid]['station'] == station, 'Startup rule points outside station')
            require(bool(rule['reason']) and Fraction(rule['expected_rate_hz']) > 0, 'Incomplete startup rule')
        for gid, rule in settings.get('exclude_timing_conflict_groups', {}).items():
            require(gid in groups and groups[gid]['station'] == station,
                    'Timing exclusion points outside station')
            require(bool(rule.get('reason')), 'Timing exclusion must explain the decision')
            require(groups[gid]['clock_uncertain'] or
                    'timing_uncertain_from_prior_evidence' in groups[gid]['flags'],
                    'Timing exclusion has no matching QC clock conflict: ' + gid)
            require(gid not in settings['exclude_startup_groups'],
                    'Recording must have one explicit exclusion category')
        for gid, channels in settings['allow_partial_groups'].items():
            require(gid in groups and groups[gid]['station'] == station, 'Partial-channel rule outside station')
            require(set(channels) < set(CHANNELS) and channels and len(channels) == len(set(channels)),
                    'Partial-channel rule must name a nonempty proper subset')
    if policy.get('reviewed_rate_sequences'):
        reference=policy['reviewed_rate_workbook']
        require(reference['filename_utc_offset']==qm['context']['filename_utc_offset_assumed'], 'Reviewed rate timezone differs from QC')
        hashes[str(Path(reference['path']).resolve())]=reference['sha256']
    return records, previous, groups, details, policy, hashes, qm


def layout(channel):
    """Validate a whole channel and return a continuous interval, or raise."""
    segments = channel['segments']
    require(channel['status'] == 'ok' and channel['sample_count'] > 0 and segments,
            'No usable channel payload')
    require(not channel.get('flags'), 'Channel QC flags require review')
    require(len(channel.get('file_sha256', '')) == 64, 'Missing source checksum')
    offset, end, first, rate = 0, None, None, None
    for segment in segments:
        start, step, stop, hz = map(Fraction, (segment['start_ns'], segment['step_ns'],
                                              segment['end_exclusive_ns'], segment['rate_hz']))
        count = segment['samples']
        require(isinstance(count, int) and count > 0 and segment['sample_offset'] == offset,
                'Segment sample offsets/counts are inconsistent')
        require(step > 0 and hz > 0 and step * hz == 10**9 and start + step * count == stop,
                'Segment timing/count/rate arithmetic differs')
        require(step.denominator==1 and start.denominator==1,'Sample times are not exactly representable in integer nanoseconds; a timing adapter is required')
        require(not segment.get('timing_uncertain', True), 'Channel timing is uncertain')
        require(segment['rate_basis'] in RATE_BASES, 'Unknown rate basis')
        if first is None:
            first, rate = start, hz
        else:
            require(start == end and hz == rate, 'Internal gap/overlap/rate change requires segment selection')
        offset += count
        end = stop
    require(offset == channel['sample_count'], 'Channel total differs from segment counts')
    return first, end, rate, offset


def classify(g, settings):
    result = {'record_type': 'recording_selection', 'id': 'selection:' + g['group_id'][6:],
              'group_id': g['group_id'], 'station': g['station'], 'recording_group': g['recording_group'],
              'qc_record_id': g['id'], 'action': 'hold_for_review', 'reason': '', 'run_id': None,
              'selected_channels': [], 'omitted_channels': list(CHANNELS), 'qc_snapshot': g}
    manual=settings.get('exclude_reviewed_groups',{}).get(g['group_id'])
    if manual:
        from survey_settings import checked_group_rule
        checked_group_rule(g,manual)
        result.update(action='exclude_reviewed',reason=manual['reason'])
        return result
    if g['clock_uncertain'] or 'timing_uncertain_from_prior_evidence' in g['flags']:
        rule = settings.get('exclude_timing_conflict_groups', {}).get(g['group_id'])
        if rule:
            result.update(action='exclude_timing_conflict', reason=rule['reason'])
        else:
            result['reason'] = 'Clock evidence conflicts with the reported start. No timestamp correction applied.'
        return result
    rule = settings['exclude_startup_groups'].get(g['group_id'])
    if rule:
        require({Fraction(r) for r in g['rates_hz']} == {Fraction(rule['expected_rate_hz'])},
                'Startup rule no longer matches the observed rate: ' + g['group_id'])
        require(settings['output_native_rate_hz']=='any' or Fraction(rule['expected_rate_hz']) != Fraction(settings['output_native_rate_hz']),
                'Startup rule cannot exclude the selected native rate')
        result.update(action='exclude_startup', reason=rule['reason'])
        return result
    unknown = set(g['flags']) - CONTEXT_FLAGS
    acknowledged=settings.get('acknowledge_group_flags',{}).get(g['group_id'])
    if acknowledged:
        from survey_settings import checked_group_rule
        checked_group_rule(g,acknowledged)
        flags=set(acknowledged.get('flags',[]))
        require(flags and flags <= {'repeated_five_channel_sample_sequence'} and flags <= set(g['flags']),
                'Only the repeated-sequence finding can be acknowledged explicitly')
        unknown-=flags
    if unknown:
        result['reason'] = 'QC findings require review: ' + '; '.join(sorted(unknown))
        return result
    available = [c for c in CHANNELS if g['channels'][c]['status'] == 'ok']
    if available != list(CHANNELS):
        allowed = settings['allow_partial_groups'].get(g['group_id'])
        if allowed is None or set(allowed) != set(available):
            result['reason'] = 'Incomplete channel set has no matching explicit selection rule.'
            return result
        # Do not treat read failures as proof that a channel contains no data.
        if any(g['channels'][c]['status'] != 'empty' for c in CHANNELS if c not in available):
            result['reason'] = 'An omitted channel has a read/missing-file problem rather than a verified empty payload.'
            return result
    try:
        layouts = [layout(g['channels'][c]) for c in available]
        require(layouts and len(set(layouts)) == 1, 'Usable channels do not have equal counts and aligned timing')
        start, end, rate, count = layouts[0]
        require(settings['output_native_rate_hz']=='any' or rate == Fraction(settings['output_native_rate_hz']), 'Rate is outside this selection policy')
        bases = sorted({s['rate_basis'] for c in available for s in g['channels'][c]['segments']})
        require(settings['accept_logged_ascii_rate_estimates'] or
                'sample_count_over_logged_duration_estimate' not in bases, 'ASCII rate estimate not accepted by policy')
    except ValueError as exc:
        result['reason'] = str(exc)
        return result
    result.update(action='include', reason='Usable aligned channels at the selected native rate; keep all samples.',
                  selected_channels=available, omitted_channels=[c for c in CHANNELS if c not in available],
                  start_ns=str(start), end_exclusive_ns=str(end), rate_hz=str(rate),
                  samples_per_channel=count, rate_bases=bases)
    if len(available) < 5:
        missing = ', '.join(c.upper() for c in CHANNELS if c not in available)
        result['reason'] = f'Retain {len(available)} readable channels separately; {missing} empty. No trimming or gap filling.'
    return result


def build_plan(groups, policy):
    from reviewed_rates import project
    views=project(groups,policy.get('reviewed_rate_sequences',[]),policy.get('reviewed_rate_workbook'))
    selected = {gid: classify(g, policy['stations'][g['station']]) for gid, g in views.items()}
    for gid,view in views.items():
        if view is not groups[gid]:
            selected[gid]['rate_review']=view['rate_review']
            selected[gid]['qc_snapshot']=groups[gid]
            if selected[gid]['action']=='include':
                selected[gid]['reason']='Include at the explicitly reviewed workbook/boundary rate. Original QC and its missing-rate flags remain recorded; sample values and filename-derived start are unchanged.'
    # Recompute overlaps among included intervals only. An excluded or held uncertain
    # snippet must not disqualify the good run that it appears to overlap.
    for station in policy['stations']:
        candidates = sorted((s for s in selected.values() if s['station'] == station and s['action'] == 'include'),
                            key=lambda s: (Fraction(s['start_ns']), s['group_id']))
        active = []
        conflicts = set()
        for current in candidates:
            active = [s for s in active if Fraction(s['end_exclusive_ns']) > Fraction(current['start_ns'])]
            for other in active:
                if set(current['selected_channels']) & set(other['selected_channels']):
                    conflicts.update((current['group_id'], other['group_id']))
            active.append(current)
        for gid in conflicts:
            selected[gid].update(action='hold_for_review', reason='Overlap between otherwise included recordings; copy/trim decision required.',
                                 selected_channels=[], omitted_channels=list(CHANNELS))
    runs = []
    for station in sorted(policy['stations']):
        candidates = sorted((s for s in selected.values() if s['station'] == station and s['action'] == 'include'),
                            key=lambda s: (Fraction(s['start_ns']), s['group_id']))
        station_runs = []
        for s in candidates:
            compatible = (station_runs and station_runs[-1]['end_exclusive_ns'] == s['start_ns']
                          and station_runs[-1]['channels'] == s['selected_channels']
                          and station_runs[-1]['rate_hz'] == s['rate_hz'])
            if not compatible:
                run_id = f'run_{len(station_runs)+1:04d}'
                station_runs.append({'record_type': 'run_plan', 'id': f'run:{station}/{run_id}',
                    'station': station, 'run_id': run_id, 'channels': s['selected_channels'],
                    'rate_hz': s['rate_hz'], 'start_ns': s['start_ns'], 'end_exclusive_ns': s['end_exclusive_ns'],
                    'samples_per_channel': 0, 'group_ids': [], 'source_records': [], 'rate_bases': []})
            r = station_runs[-1]
            s['run_id'] = r['run_id']
            r['group_ids'].append(s['group_id'])
            r['source_records'].append({'group_id': s['group_id'], 'selection_id': s['id'],
                                       'output_sample_offset': r['samples_per_channel'],
                                       'samples_per_channel': s['samples_per_channel']})
            r['samples_per_channel'] += s['samples_per_channel']
            r['end_exclusive_ns'] = s['end_exclusive_ns']
            r['rate_bases'] = sorted(set(r['rate_bases']) | set(s['rate_bases']))
        for r in station_runs:
            duration = (Fraction(r['end_exclusive_ns']) - Fraction(r['start_ns'])) / 10**9
            require(duration * Fraction(r['rate_hz']) == r['samples_per_channel'], 'Run contains a gap or count mismatch')
            r.update(start_utc_assumed=iso(r['start_ns']), end_exclusive_utc_assumed=iso(r['end_exclusive_ns']),
                     duration_seconds=str(duration), source_group_count=len(r['group_ids']))
        runs.extend(station_runs)
    return selected, runs


def run(qc, decisions, policy_path, out):
    require(not out.exists(), 'Output already exists; use a new report directory')
    records, previous, groups, details, policy, hashes, qm = load(qc, decisions, policy_path)
    # A report-only program still refuses destinations within known raw roots.
    output = out.resolve()
    for root in {g['original_root'] for g in groups.values()} | {g['source_root_used'] for g in groups.values()}:
        # Gadi paths can be inspected locally without opening those paths.
        if root.startswith('/') and sys.platform == 'win32':
            continue
        p = Path(root).resolve()
        require(output != p and p not in output.parents, 'Output must be outside raw source directories')
    selections, runs = build_plan(groups, policy)
    prior = [r for r in previous if r['record_type'] == 'station_decision']
    for gid, s in selections.items():
        s['station_decision_id'] = details[gid]['id']
        s['overlap_evidence_ids'] = groups[gid]['overlap_comparison_ids']
    for name, expected in hashes.items():
        require(sha(Path(name)) == expected, 'An input changed while preparing the plan')
    counts = dict(Counter(s['action'] for s in selections.values()))
    manifest = {'record_type': 'selection_manifest', 'id': 'manifest:run_selection',
        'schema': 'mtl0.run_selection/1', 'created_utc': datetime.now(timezone.utc).isoformat(),
        'input_sha256': hashes, 'qc_input_chain_sha256': qm['input_sha256'],
        'code_sha256': {p.name: sha(p) for p in (Path(__file__), Path(__file__).with_name('selection_reports.py'), Path(__file__).with_name('reviewed_rates.py'))},
        'policy': policy, 'counts': counts, 'recording_groups': len(selections), 'run_count': len(runs),
        'plan_complete': True, 'all_groups_included': counts.get('include', 0) == len(selections),
        'raw_files_opened': False, 'raw_data_modified': False, 'metadata_modified': False,
        'concatenated': False, 'level1_selection_applied': False, 'timestamps_corrected': False,
        'scope': 'Whole-recording Level 0 selection for survey targets only; native sample values and rates preserved.',
        'future_export_requirement': 'Before exporting, recheck source SHA256 and sample counts from qc_snapshot. Hold/excluded groups must not enter runs.',
        'assumptions': qm['context'], 'python': sys.version.split()[0]}
    out.mkdir(parents=True, exist_ok=False)
    write_workbook(out/'selection_review.xlsx', manifest, selections, runs, prior)
    manifest['selection_review_xlsx_sha256'] = sha(out/'selection_review.xlsx')
    evidence = [manifest, *runs, *sorted(selections.values(), key=lambda s: (s['station'], s['group_id'])),
                *[{'record_type': 'prior_station_decision', 'id': 'prior:' + r['id'], 'data': r} for r in prior],
                *[{'record_type': 'prior_qc_interval', 'id': 'prior:' + r['id'], 'data': r} for r in records
                  if r['record_type'] in ('overlap_comparison', 'reported_interval_gap', 'repeated_sample_sequence')]]
    with (out/'selection_evidence.jsonl').open('x', encoding='utf-8', newline='\n') as f:
        for r in evidence:
            f.write(json.dumps(r, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n')
    require(read_jsonl(out/'selection_evidence.jsonl') == evidence, 'Selection JSONL readback differs')
    print(f'SUCCESS: {len(runs)} planned Level 0 runs. Recording decisions: {counts}.')
    print('Reports:', out)
    print('Raw samples were not opened or modified. Concatenation has not started.')
    return manifest, selections, runs


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qc', type=Path, required=True)
    parser.add_argument('--decisions', type=Path, required=True)
    parser.add_argument('--policy', type=Path, default=Path(__file__).with_name('06_selection_policy.json'))
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    run(args.qc, args.decisions, args.policy, args.out)
