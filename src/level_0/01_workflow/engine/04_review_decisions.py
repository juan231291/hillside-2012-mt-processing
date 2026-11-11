"""Create station/copy decisions after recorder evidence is available.

Read Step 02, Step 03 and the original workbook. Write decision.csv and
decision_evidence.jsonl. Timing flags survive supported station assignments.
No raw data, timestamps, workbook values or previous reports are changed.
"""
from pathlib import Path
from collections import Counter, defaultdict
from datetime import datetime, timezone
import argparse
import csv
import json

from workflow_common import (load_evidence, load_metadata, filename_review, offset,
                             sha, iso, COLUMNS)


def load_logs(folder, comparison, metadata, groups, filename_offset):
    path = folder / 'log_evidence.jsonl' if folder.is_dir() else folder
    table = path.with_name('log_review.csv')
    with path.open(encoding='utf-8-sig') as f:
        records = [json.loads(s) for s in f if s.strip()]
    ids = [r.get('id') for r in records]
    if None in ids or len(ids) != len(set(ids)):
        raise ValueError('Duplicate or missing recorder evidence IDs')
    manifest = next((r for r in records if r['id'] == 'manifest:recorder_log_review'), {})
    if manifest.get('schema') != 'mtl0.recorder_log_review/2' or manifest.get('complete') is not True:
        raise ValueError('Completed new Step 03 recorder evidence is required (schema /2)')
    if manifest.get('comparison_sha256') != sha(comparison) or manifest.get('metadata_sha256') != sha(metadata):
        raise ValueError('Recorder evidence used a different comparison or metadata workbook')
    if manifest.get('filename_utc_offset_assumed') != filename_offset:
        raise ValueError('Filename timezone assumptions differ between steps')
    if manifest.get('decisions_applied') or manifest.get('timestamps_corrected'):
        raise ValueError('Expected evidence collected before decisions or timestamp corrections')
    if manifest.get('log_review_csv_sha256') != sha(table):
        raise ValueError('Human recorder review differs from its evidence; reconcile edits first')
    details = {r['review']['group_id']: r for r in records if r['record_type'] == 'group_log_review'}
    if len(details) != manifest.get('source_group_rows') or set(details) != set(groups):
        raise ValueError('Recorder evidence must cover every Step 02 group exactly once')
    if dict(Counter(d['review']['assessment'] for d in details.values())) != manifest['assessment_counts']:
        raise ValueError('Recorder assessment totals disagree')
    with table.open(encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    if len(rows) != manifest['human_review_rows'] or rows != [d['review'] for d in details.values() if d['included_in_human_review']]:
        raise ValueError('Recorder CSV/evidence rows disagree')
    for gid, d in details.items():
        row, group = d['review'], groups[gid]
        if any(row[a] != group[b] for a, b in [('source_folder', 'source_label'), ('source_role', 'source_role'), ('recording_group', 'group_key')]):
            raise ValueError('Recorder group identity mismatch')
        if row['log_evidence_id'] != d['id'] or int(row['log_write_count']) != len(d['write_events']):
            raise ValueError('Recorder evidence links/counts disagree')
        if row['suggested_station']:
            if row['assessment'] != 'station_supported_by_logger_and_context' or not d['write_events']:
                raise ValueError('Station proposal lacks recorder support')
            serials = {e['serial'] for e in d['write_events']}
            if None in serials or len(serials) != 1 or ';'.join(sorted(serials)) != row['logged_serial_numbers']:
                raise ValueError('Station proposal lacks unique logged serial')
            owners = set()
            for event in d['write_events']:
                support = event['matching_filename_recordings'] or event['nearest_known_recordings']
                if not support or any(a['serial'] != event['serial'] for a in support):
                    raise ValueError('Station proposal lacks matching recorder context')
                owners.update(a['station'].casefold() for a in support)
            if owners != {row['suggested_station'].casefold()}:
                raise ValueError('Station proposal conflicts with supporting context')
    return details, manifest, {str(p.resolve()): sha(p) for p in (path, table)}


def build(records, groups, comparisons, stations, zone, logs):
    rows, details = filename_review(records, groups, comparisons, stations, zone)
    output = {r['group_id']: r for r in rows}
    evidence = {r['group_id']: r for r in details}
    graph = defaultdict(set)
    nonidentical = set()
    for comparison in comparisons:
        a, b = comparison['group_ids']
        if comparison['status'] == 'identical_files':
            graph[a].add(b); graph[b].add(a)
        else:
            nonidentical.update((a, b))
    for gid, row in output.items():
        item = logs[gid]['review']
        original = row.copy()
        flags = set(filter(None, row['review_flags'].split(';')))
        flags.update(filter(None, item['timing_review_flags'].split(';')))
        flags.update(filter(None, item['assignment_review_flags'].split(';')))
        suggested = item['suggested_station']
        if suggested:
            if suggested.casefold() not in stations:
                raise ValueError('Recorder proposal station is absent from metadata')
            row.update(assigned_station=stations[suggested.casefold()]['station'], decision='retain_for_qc',
                       retained_group_id=gid, metadata_comment='',
                       reason='Logged serial and independent recording context support the station. Retain for timing and sample QC.')
            if item['timing_review_flags']:
                row['reason'] += ' Timing warnings remain unresolved.'
        elif int(item['log_write_count']) > 0:
            row.update(assigned_station='', decision='unresolved', retained_group_id='', metadata_comment='',
                       reason='Matching recorder events lack a unique supported station; review before selecting this recording.')
            flags.add('recorder_station_context_requires_review')
        # Numeric equality with differing files/headers does not justify deletion.
        if gid in nonidentical:
            row.update(decision='unresolved', retained_group_id='', metadata_comment='',
                       reason='Name-matched files are not verified exact copies; retain evidence for manual copy/timing review.')
        evidence[gid].update(filename_only_assessment=original, recorder_evidence_id=logs[gid]['id'],
                             recorder_review=item, _flags=flags)
    visited = set()
    for first in sorted(graph):
        if first in visited:
            continue
        todo, component = [first], set()
        while todo:
            g = todo.pop()
            if g not in component:
                component.add(g); todo.extend(graph[g] - component)
        visited.update(component)
        owners = {output[g]['assigned_station'].casefold() for g in component if output[g]['assigned_station']}
        supported = len(owners) == 1 and all(output[g]['assigned_station'] and output[g]['decision'] != 'unresolved' for g in component)
        owner = next(iter(owners)) if len(owners) == 1 else None
        preferred = [g for g in component if groups[g]['source_label'].casefold() == owner]
        if not supported or len(preferred) != 1:
            for g in component:
                output[g].update(decision='unresolved', retained_group_id='', metadata_comment='',
                                 reason='Exact copies exist but a unique supported station and retained folder copy are not established.')
                evidence[g]['_flags'].add('exact_copy_group_needs_station_assignment')
            continue
        retained = preferred[0]
        canonical = stations[owner]['station']
        for g in component:
            row = output[g]
            evidence[g]['_flags'].discard('exact_copy_group_needs_station_assignment')
            row.update(assigned_station=canonical, retained_group_id=retained)
            if g == retained:
                row.update(decision='retain_for_qc', metadata_comment='',
                           reason='Retain the exact copy in the supported station folder; preserve timing/QC flags.')
            else:
                row.update(decision='exclude_duplicate',
                           reason='Five-channel exact copy assigned to the supported station; select the linked copy in its station folder.',
                           metadata_comment=f'Duplicate recordings assigned to {canonical} were excluded from the {row["source_folder"]} output.')
    for gid, row in output.items():
        row['review_flags'] = ';'.join(sorted(evidence[gid].pop('_flags')))
        evidence[gid]['decision'] = row.copy()
    return rows, details


def run(comparison, metadata, logs, out, filename_offset='+00:00'):
    if out.exists():
        raise FileExistsError('Output already exists: ' + str(out))
    hashes = {str(p.resolve()): sha(p) for p in (comparison, metadata)}
    records, groups, comparisons = load_evidence(comparison)
    for r in records:
        if r['record_type'] == 'source':
            raw = Path(r['data']['resolved_root']).resolve()
            if out.resolve() == raw or raw in out.resolve().parents:
                raise ValueError('Output must be outside raw source folders')
    stations = load_metadata(metadata)
    log_details, log_manifest, log_hashes = load_logs(logs, comparison, metadata, groups, filename_offset)
    hashes.update(log_hashes)
    rows, details = build(records, groups, comparisons, stations, offset(filename_offset), log_details)
    for path, expected in hashes.items():
        if sha(Path(path)) != expected:
            raise ValueError('Input changed during decision review')
    out.mkdir(parents=True, exist_ok=False)
    table = out / 'decision.csv'
    with table.open('x', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS); writer.writeheader(); writer.writerows(rows)
    with table.open(encoding='utf-8-sig', newline='') as f:
        if list(csv.DictReader(f)) != rows:
            raise ValueError('Decision CSV readback differs')
    counts = dict(Counter(r['decision'] for r in rows))
    manifest = {'record_type': 'decision_manifest', 'id': 'manifest:station_decisions', 'schema': 'mtl0.station_decisions/2',
                'created_utc': iso(datetime.now(timezone.utc)), 'input_sha256': hashes,
                'code_sha256': {p.name: sha(p) for p in (Path(__file__), Path(__file__).with_name('workflow_common.py'))},
                'decision_csv_sha256': sha(table), 'filename_utc_offset_assumed': filename_offset,
                'log_utc_offset_assumed': log_manifest['log_utc_offset_assumed'], 'source_groups': len(rows), 'counts': counts,
                'survey_station_labels': [r['data']['label'] for r in records if r['record_type'] == 'source' and r['data']['role'] == 'survey'],
                'complete': True, 'all_station_assignments_resolved': not counts.get('unresolved', 0),
                'scope': 'Station assignment and exact-copy selection within the configured survey scope. References remain reference sources.',
                'retention_scope': 'retain_for_qc is not approval for concatenation/publication. Clock, rate, channel, gap and overlap checks still follow.',
                'metadata_modified': False, 'raw_data_modified': False, 'timestamps_corrected': False}
    with (out / 'decision_evidence.jsonl').open('x', encoding='utf-8', newline='\n') as f:
        refs = [{'record_type': 'metadata_reference', 'id': 'metadata:' + k, **v} for k, v in sorted(stations.items())]
        for record in [manifest, *refs, *details]:
            f.write(json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n')
    print('SUCCESS: station/copy decisions created. Counts:', counts, flush=True)
    print('Timing warnings remain for QC. Output:', out, flush=True)
    return 0


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--comparison', type=Path, required=True)
    p.add_argument('--metadata', type=Path, required=True)
    p.add_argument('--logs', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--filename-utc-offset', default='+00:00')
    a = p.parse_args()
    raise SystemExit(run(a.comparison, a.metadata, a.logs, a.out, a.filename_utc_offset))
