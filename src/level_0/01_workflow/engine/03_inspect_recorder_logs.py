"""Inspect recorder evidence BEFORE station/copy decisions.

Input: Step 02 evidence (including inventory) and original Excel metadata.
Output: log_review.csv (actionable rows) and log_evidence.jsonl (all groups).
No decision.csv input, sample reads, timestamp corrections or metadata changes.
"""
from pathlib import Path, PurePosixPath
from collections import Counter, defaultdict
from datetime import datetime, timezone
import argparse
import csv
import hashlib
import json

from workflow_common import load_evidence, load_metadata, filename_review, offset, sha
from recorder_evidence import (source_file, parse_log, inspect_log, recorder_key,
                               parse_utc, utc_offset, iso, CONFIG)

FIELDS = ['source_folder', 'source_role', 'recording_group', 'filename_station',
          'logged_serial_numbers', 'suggested_station', 'assessment', 'log_write_count',
          'logged_max_seconds', 'first_log_write_utc_assumed', 'last_log_write_utc_assumed',
          'filename_start_utc_assumed', 'timing_review_flags', 'assignment_review_flags',
          'reason', 'log_evidence_id', 'group_id']


def profiles(records, groups, comparisons, metadata, zone):
    rows, details = filename_review(records, groups, comparisons, metadata, zone)
    detail = {r['group_id']: r for r in details}
    blockers = {'filename_header_start_conflict', 'invalid_header_timing', 'header_read_problem',
                'channel_header_windows_differ', 'nonidentical_candidate_requires_review'}
    for row in rows:
        # These independent anchors are frozen before reading proposed assignments.
        # A filename, valid recording window and logged matching serial must agree.
        supported = row['filename_station'] and not blockers.intersection(row['review_flags'].split(';'))
        supported = supported and any(c['station'] == row['filename_station'] and c['relation'] == 'within'
                                      and not c.get('header_last_sample_outside')
                                      for c in detail[row['group_id']]['metadata_window_checks'])
        row['anchor_station'] = row['filename_station'] if supported else ''
    return rows


def exact_source_links(groups, comparisons):
    graph = defaultdict(set)
    for c in comparisons:
        if c['status'] == 'identical_files':
            a, b = c['group_ids']
            graph[a].add(b); graph[b].add(a)
    result = {}
    for gid in groups:
        todo, seen = [gid], set()
        while todo:
            item = todo.pop()
            if item not in seen:
                seen.add(item); todo.extend(graph[item] - seen)
        result[gid] = {(groups[g]['source_role'], groups[g]['source_label']) for g in seen}
    return result


def run(comparison, metadata_path, out, overrides=(), log_offset='+00:00', filename_offset='+00:00'):
    if out.exists():
        raise FileExistsError('Output already exists: ' + str(out))
    hashes = {str(p.resolve()): sha(p) for p in (comparison, metadata_path)}
    records, groups, comparisons = load_evidence(comparison)
    metadata = load_metadata(metadata_path)
    rows = profiles(records, groups, comparisons, metadata, offset(filename_offset))
    sources = [r['data'] for r in records if r['record_type'] == 'source']
    files = [(r['id'], r['data']) for r in records if r['record_type'] == 'file']
    roots = {(s['role'], s['label']): Path(s['resolved_root']).resolve() for s in sources}
    original = roots.copy()
    for arg in overrides:
        key, sep, value = arg.partition('=')
        role, slash, label = key.partition('/')
        if not sep or not slash or (role, label) not in roots:
            raise ValueError('Source override must match ROLE/LABEL=DIR in inventory')
        roots[role, label] = Path(value).resolve(strict=True)
    if any(out.resolve() == root or root in out.resolve().parents for root in roots.values()):
        raise ValueError('Output must be outside source roots')
    allowed = exact_source_links(groups, comparisons)
    zone = utc_offset(log_offset)
    all_events, log_sources, read_errors = defaultdict(list), [], []
    wanted = {recorder_key(r['recording_group']) for r in rows}
    for fid, row in files:
        key = row['source_role'], row['source_label']
        name = PurePosixPath(row['relative_path']).name.lower()
        if name not in ('recorder.msg', 'recorder.ini'):
            continue
        provenance = {'record_type': 'log_source', 'id': 'log_source:' + fid.removeprefix('file:'),
                      'inventory_file_id': fid, 'source_role': key[0], 'source_label': key[1],
                      'relative_path': row['relative_path'], 'original_root': str(original[key])}
        try:
            path, data = source_file(row, roots[key])
            text = data.decode('utf-8-sig', errors='strict')
            provenance.update(actual_path=str(path), bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
            if name == 'recorder.msg':
                parsed = parse_log(text, zone)
                anchor_rows = [r for r in rows if key in allowed[r['group_id']]]
                mapped = inspect_log(fid, parsed, anchor_rows, metadata)
                for name_key in wanted:
                    for event in mapped.get(name_key, []):
                        event['log_source_key'] = list(key)
                        all_events[name_key].append(event)
                provenance.update(kind='recorder_log', write_events=len(parsed[0]), historical_serials=parsed[2])
            else:
                settings = [{'line': n, 'key': m[1], 'value': m[2], 'text': line}
                            for n, line in enumerate(text.splitlines(), 1) if (m := CONFIG.fullmatch(line.strip()))]
                provenance.update(kind='config_snapshot', settings=settings,
                                  limitation='Undated snapshot; not applied retrospectively to recordings or sample rates.')
        except (ValueError, OSError, UnicodeError) as exc:
            provenance['error'] = str(exc); read_errors.append(fid)
        log_sources.append(provenance)
    available = {(s['source_role'], s['source_label']) for s in log_sources if s.get('kind') == 'recorder_log'}
    missing = sorted('/'.join(key) for key in roots if key not in available)
    output, evidence = [], []
    for row in rows:
        gid = row['group_id']
        found = sorted((e for e in all_events.get(recorder_key(row['recording_group']), [])
                        if tuple(e['log_source_key']) in allowed[gid]), key=lambda e: (e['source_file_id'], e['line']))
        serials = sorted({e['serial'] for e in found if e['serial']})
        timing_flags, assignment_flags, owners = set(), set(), set()
        start = parse_utc(row['filename_start_utc_assumed'])
        if not found:
            assignment_flags.add('no_matching_log_write')
        if found and any(e['serial'] is None for e in found):
            assignment_flags.add('serial_missing_in_recording_session')
        if found and len(serials) != 1:
            assignment_flags.add('no_unique_logged_serial')
        for event in found:
            if start and parse_utc(event['time_utc_assumed']) < start:
                timing_flags.add('log_write_precedes_filename_start')
            # Direct independent evidence takes precedence over proximity at a
            # deployment boundary. Unknown labels require surrounding anchors.
            supports = event['matching_filename_recordings'] or event['nearest_known_recordings']
            if not supports:
                assignment_flags.add('no_supported_recording_context')
            owners.update(a['station'] for a in supports)
        if len({s.casefold() for s in owners}) > 1:
            assignment_flags.add('known_recordings_support_different_stations')
        for sid in {e['source_file_id'] for e in found}:
            hits = [e for e in found if e['source_file_id'] == sid]
            lo, hi = min(e['line'] for e in hits), max(e['line'] for e in hits)
            if any(lo <= p['line'] <= hi and 'gps discontin' in p['text'].lower()
                   for e in hits for p in e['session']['context_events']):
                timing_flags.add('gps_discontinuity_between_append_writes')
        suggested = next(iter(owners)) if len(owners) == 1 and len(serials) == 1 and not assignment_flags else ''
        if suggested and row['filename_station'] and suggested.casefold() != row['filename_station'].casefold():
            assignment_flags.add('filename_label_differs_from_log_supported_station')
        times = [parse_utc(e['time_utc_assumed']) for e in found]
        eid = 'log_review:' + gid.removeprefix('group:')
        result = dict.fromkeys(FIELDS, '')
        result.update(source_folder=row['source_folder'], source_role=row['source_role'], recording_group=row['recording_group'],
                      filename_station=row['filename_station'], logged_serial_numbers=';'.join(serials), suggested_station=suggested,
                      assessment='station_supported_by_logger_and_context' if suggested else 'station_still_needs_review',
                      log_write_count=str(len(found)), logged_max_seconds=str(max(e['logged_seconds'] for e in found)) if found else '',
                      first_log_write_utc_assumed=iso(min(times)) if times else '', last_log_write_utc_assumed=iso(max(times)) if times else '',
                      filename_start_utc_assumed=row['filename_start_utc_assumed'], timing_review_flags=';'.join(sorted(timing_flags)),
                      assignment_review_flags=';'.join(sorted(assignment_flags)), group_id=gid, log_evidence_id=eid,
                      reason='Logged serial and independent filename/metadata recording context support this station; timing QC still follows.' if suggested else
                             'Recorder evidence does not establish a single station; consult filename/metadata evidence or review.')
        # Human report highlights decisions needing explanation and timing issues.
        human = not row['anchor_station'] or bool(timing_flags or assignment_flags)
        if human:
            output.append(result)
        evidence.append({'record_type': 'group_log_review', 'id': eid, 'review': result, 'included_in_human_review': human,
                         'filename_evidence': {k: row[k] for k in ('group_id', 'filename_station', 'anchor_station', 'review_flags')},
                         'write_events': found, 'allowed_log_source_keys': [list(k) for k in sorted(allowed[gid])],
                         'basis': 'Source-local logs, plus exact-copy-linked sources only. Frozen filename/metadata anchors require logged matching serial. Log line order, serial and UTC date select surrounding anchors; inferred owners never become anchors.',
                         'logged_seconds_note': 'Maximum cumulative logged seconds, not sample-verified duration. Copied logs are not independent measurements.'})
    for path, expected in hashes.items():
        if sha(Path(path)) != expected:
            raise ValueError('Input changed during recorder review')
    out.mkdir(parents=True, exist_ok=False)
    table = out / 'log_review.csv'
    with table.open('x', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS); writer.writeheader(); writer.writerows(output)
    with table.open(encoding='utf-8-sig', newline='') as f:
        if list(csv.DictReader(f)) != output:
            raise ValueError('CSV readback differs')
    manifest = {'record_type': 'log_review_manifest', 'id': 'manifest:recorder_log_review', 'schema': 'mtl0.recorder_log_review/2',
                'created_utc': iso(datetime.now(timezone.utc)), 'input_sha256': hashes,
                'comparison_sha256': sha(comparison), 'metadata_sha256': sha(metadata_path),
                'code_sha256': {p.name: sha(p) for p in (Path(__file__), Path(__file__).with_name('workflow_common.py'), Path(__file__).with_name('recorder_evidence.py'))},
                'log_review_csv_sha256': sha(table), 'log_utc_offset_assumed': log_offset, 'filename_utc_offset_assumed': filename_offset,
                'source_group_rows': len(evidence), 'human_review_rows': len(output),
                'assessment_counts': dict(Counter(r['review']['assessment'] for r in evidence)),
                'timing_flag_row_counts': dict(Counter(flag for r in evidence for flag in r['review']['timing_review_flags'].split(';') if flag)),
                'source_read_errors': read_errors, 'missing_log_sources': missing, 'complete': not read_errors,
                'decisions_applied': False, 'raw_waveforms_read': False, 'timestamps_corrected': False,
                'scope': 'All inventory groups checked against available inventoried logs. Missing logs are explicit evidence gaps, not reconstructed records.'}
    with (out / 'log_evidence.jsonl').open('x', encoding='utf-8', newline='\n') as f:
        for record in [manifest, *log_sources, *evidence]:
            f.write(json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n')
    print('Recorder evidence:', len(evidence), 'groups;', len(output), 'human review rows.', flush=True)
    print('Assessments:', manifest['assessment_counts'], '| Timing flags:', manifest['timing_flag_row_counts'], flush=True)
    print('SUCCESS' if manifest['complete'] else 'INCOMPLETE', out, flush=True)
    return 0 if manifest['complete'] else 2


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--comparison', type=Path, required=True)
    p.add_argument('--metadata', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--source-root', action='append', default=[])
    p.add_argument('--log-utc-offset', default='+00:00')
    p.add_argument('--filename-utc-offset', default='+00:00')
    a = p.parse_args()
    raise SystemExit(run(a.comparison, a.metadata, a.out, a.source_root, a.log_utc_offset, a.filename_utc_offset))
