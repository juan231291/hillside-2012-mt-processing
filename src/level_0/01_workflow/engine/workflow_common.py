"""Shared report validation, metadata reading and filename evidence.

Read Step 02 evidence and the original Excel workbook. filename_review returns
preliminary filename/window assessments in memory; recorder inspection uses
only independently supported filename evidence as anchors. It does not write
decisions. No raw reads, edits or physical installation-time reconstruction.
Python 3.10+ and openpyxl are required.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

from survey_settings import canonical_station

CHANNELS = {'ex', 'ey', 'bx', 'by', 'bz'}
COLUMNS = [
    'source_folder', 'source_role', 'recording_group', 'filename_station',
    'filename_start_utc_assumed', 'header_start_utc', 'header_end_exclusive_utc',
    'candidate_stations', 'metadata_windows_containing_start',
    'assigned_station', 'decision', 'applies_to', 'retained_group_id',
    'reason', 'review_flags', 'comparison_ids', 'metadata_comment', 'group_id',
]


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def iso(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z') if value else ''


def offset(value):
    if not re.fullmatch(r'[+-]\d{2}:\d{2}', value):
        raise ValueError('Filename UTC offset must be explicit, for example +00:00')
    hours, minutes = map(int, value[1:].split(':'))
    if hours > 14 or minutes > 59 or (hours == 14 and minutes):
        raise ValueError('Filename UTC offset out of range')
    return timezone((1 if value[0] == '+' else -1) * timedelta(hours=hours, minutes=minutes))


def parse_time(value):
    """Metadata and header times require an explicit timezone; never invent one."""
    if value is None or str(value).strip() in ('', '-', 'None'):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        raise ValueError('Date-only value is not a recording timestamp')
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).strip().replace('Z', '+00:00'))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError('Timestamp has no explicit timezone')
    return stamp.astimezone(timezone.utc)


def load_metadata(path):
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=False)
    stations = {}
    try:
        for ws in wb:
            rows = ws.iter_rows()
            first = next(rows, ())
            indices = defaultdict(list)
            for i, cell in enumerate(first):
                if cell.value is not None:
                    indices[str(cell.value).strip()].append(i)
            if 'station.id' not in indices:
                continue
            for cells in rows:
                def get(key):
                    cc = [cells[i] for i in indices.get(key, [])]
                    if any(c.data_type == 'f' for c in cc):
                        raise ValueError(f'Formula in {ws.title} row {cells[0].row}, {key}; provide literal source metadata')
                    vals = [c.value for c in cc if c.value is not None and str(c.value).strip() != '']
                    if len({str(v).strip() for v in vals}) > 1:
                        raise ValueError(f'Conflicting duplicate columns: {key} in {ws.title} row {cells[0].row}')
                    return vals[0] if vals else None
                station = get('station.id')
                if station is None:
                    continue
                station = canonical_station(str(station).strip())
                if not re.fullmatch(r'[A-Za-z0-9_-]+', station):
                    raise ValueError('Unexpected station identifier: ' + station)
                key = station.casefold()
                owner = stations.setdefault(key, {'station': station, 'windows': []})
                family = 'station.time_period'
                start, end = get(family + '.start'), get(family + '.end')
                if start is None and end is None:
                    family = 'run.time_period'
                    start, end = get(family + '.start'), get(family + '.end')
                def refs(field):
                    return [ws.title + '!' + cells[i].coordinate for i in indices.get(field, [])]
                evidence = {
                    'sheet': ws.title, 'row': cells[0].row, 'window_fields': family,
                    'station_cells': refs('station.id'), 'start_cells': refs(family + '.start'),
                    'end_cells': refs(family + '.end'), 'start_original': str(start) if start is not None else None,
                    'end_original': str(end) if end is not None else None,
                    'logger_id': str(get('run.data_logger.id') or ''),
                    'meaning': 'existing metadata recording window; not confirmed installation/recovery times',
                }
                try:
                    a, b = parse_time(start), parse_time(end)
                    if a is None or b is None or b <= a:
                        raise ValueError('Missing or non-increasing recording window')
                    evidence.update(start_utc=iso(a), end_utc=iso(b), valid=True)
                except (ValueError, TypeError) as exc:
                    evidence.update(valid=False, error=str(exc), start_utc=None, end_utc=None)
                owner['windows'].append(evidence)
    finally:
        wb.close()
    if not stations:
        raise ValueError('No station.id rows found in the original workbook')
    return stations


def load_evidence(path):
    with path.open(encoding='utf-8-sig') as f:
        records = [json.loads(line) for line in f if line.strip()]
    ids = [r.get('id') for r in records]
    if None in ids or len(set(ids)) != len(ids):
        raise ValueError('Missing or repeated evidence IDs')
    by_id = {r['id']: r for r in records}
    manifest = by_id.get('manifest:name_match_comparison', {})
    if manifest.get('schema') != 'mtl0.name_match_comparison/2' or manifest.get('complete') is not True:
        raise ValueError('A completed Step 02 comparison report without read problems is required')
    original = by_id.get('manifest:inventory', {})
    if not original.get('original_summary', {}).get('inventory_complete'):
        raise ValueError('Original complete inventory is missing')
    groups = {r['id']: r['data'] for r in records if r.get('record_type') == 'recording_group'}
    for gid, row in groups.items():
        expected = 'group:' + '/'.join((row['source_role'], row['source_label'], row['group_key']))
        if gid != expected:
            raise ValueError('Recording group ID does not match its source fields')
    comparisons = [r for r in records if r.get('record_type') == 'name_match_comparison']
    if len(comparisons) != manifest.get('unique_pairs') or dict(Counter(r['status'] for r in comparisons)) != manifest.get('counts'):
        raise ValueError('Comparison totals do not match the Step 02 manifest')
    pairs = set()
    for r in comparisons:
        gg = r.get('group_ids', [])
        if len(gg) != 2 or len(set(gg)) != 2 or any(g not in groups for g in gg):
            raise ValueError('Invalid comparison group links')
        pair = tuple(sorted(gg))
        if pair in pairs:
            raise ValueError('Repeated comparison pair')
        pairs.add(pair)
        channels = r.get('channels', [])
        if len(channels) != 5 or {c.get('channel') for c in channels} != CHANNELS:
            raise ValueError('Comparison does not contain five distinct EM channels')
        for c in channels:
            file_ids = c.get('file_ids', [])
            if len(file_ids) != 2 or any(fid not in by_id for fid in file_ids):
                raise ValueError('Missing channel file evidence')
            for fid, gid in zip(file_ids, gg):
                row = by_id[fid]['data']
                expected_gid = 'group:' + '/'.join((row['source_role'], row['source_label'], str(PurePosixPath(row['relative_path']).with_suffix(''))))
                if gid != expected_gid or row['channel_from_extension'] != c['channel']:
                    raise ValueError('Channel comparison links to the wrong group or channel')
        if r['status'] == 'identical_files':
            for c in channels:
                counts, hashes = c.get('bytes_read', []), c.get('sha256', [])
                if ('error' in c or c.get('identical_bytes') is not True or len(counts) != 2
                        or counts[0] <= 0 or counts[0] != counts[1] or len(hashes) != 2
                        or hashes[0] != hashes[1] or not re.fullmatch('[a-f0-9]{64}', hashes[0])):
                    raise ValueError('Exact-copy status lacks matching complete byte evidence')
    return records, groups, comparisons


def recording_time(group, file_rows, headers, filename_zone):
    stem = PurePosixPath(group['group_key']).name
    match = re.fullmatch(r'(.*?)(\d{12})', stem)
    prefix, filename, flags = '', None, []
    if match:
        prefix = match[1].rstrip('_')
        try:
            filename = datetime.strptime(match[2], '%y%m%d%H%M%S').replace(tzinfo=filename_zone).astimezone(timezone.utc)
        except ValueError:
            flags.append('invalid_filename_timestamp')
    else:
        flags.append('filename_timestamp_not_recognized')
    traces, channel_spans = [], {}
    for fid, row in file_rows:
        if int(row['bytes']) == 0:
            flags.append('empty_' + row['channel_from_extension'] + '_requires_timing_qc')
        detail = headers.get(fid)
        if not detail:
            continue
        if detail.get('warnings') or detail.get('error'):
            flags.append('header_read_problem')
        span = []
        for tr in detail.get('traces', []):
            try:
                a, b = parse_time(tr['start_utc']), parse_time(tr['end_exclusive_utc'])
                last = parse_time(tr['last_sample_utc'])
                count, rate = int(tr['samples']), float(tr['sample_rate_hz'])
                if a is None or b is None or last is None or not (a <= last < b) or count <= 0 or not math.isfinite(rate) or rate <= 0:
                    raise ValueError('Invalid trace interval/count/rate')
                span.append((a, b, last))
                traces.append({'file_id': fid, **tr})
            except (ValueError, TypeError, KeyError) as exc:
                flags.append('invalid_header_timing')
                traces.append({'file_id': fid, 'error': str(exc), 'original': tr})
        if span:
            channel_spans[row['channel_from_extension']] = (min(t[0] for t in span), max(t[1] for t in span), max(t[2] for t in span))
    start = min((s[0] for s in channel_spans.values()), default=None)
    end = max((s[1] for s in channel_spans.values()), default=None)
    last = max((s[2] for s in channel_spans.values()), default=None)
    if start and filename and abs((start - filename).total_seconds()) > 0.001:
        flags.append('filename_header_start_conflict')
    if len({s[:2] for s in channel_spans.values()}) > 1:
        flags.append('channel_header_windows_differ')
    if channel_spans and set(channel_spans) != CHANNELS:
        flags.append('header_timing_not_available_for_all_channels')
    return {'prefix': prefix, 'filename_start_utc_assumed': iso(filename), 'header_start_utc': iso(start),
            'header_end_exclusive_utc': iso(end), 'header_last_sample_utc': iso(last),
            'comparison_start_utc': iso(start or filename), 'flags': sorted(set(flags)), 'traces': traces,
            'time_basis': 'header UTC start' if start else 'filename start with explicit UTC-offset assumption',
            'duration_note': 'ASCII end/duration is not inferred from the next filename or metadata sample rate'}


def window_checks(candidate_keys, stations, timing):
    start = parse_time(timing['comparison_start_utc'])
    last = parse_time(timing['header_last_sample_utc'])
    checks = []
    for key in sorted(candidate_keys):
        owner = stations.get(key)
        if not owner:
            checks.append({'station': key, 'relation': 'station_missing_from_metadata'})
            continue
        for window in owner['windows']:
            result = {'station': owner['station'], 'metadata_sheet': window['sheet'], 'metadata_row': window['row']}
            if not window['valid']:
                result['relation'] = 'invalid_metadata_window'
            elif start is None:
                result['relation'] = 'recording_start_unknown'
            else:
                a, b = parse_time(window['start_utc']), parse_time(window['end_utc'])
                relation = 'before' if start < a else 'at_end' if start == b else 'after' if start > b else 'within'
                result.update(relation=relation, metadata_start_utc=iso(a), metadata_end_utc=iso(b),
                              header_last_sample_outside=bool(last and last > b))
            checks.append(result)
    return checks


def filename_review(records, groups, comparisons, stations, filename_zone):
    files, headers = defaultdict(list), {}
    for rec in records:
        if rec['record_type'] == 'file':
            row = rec['data']
            if row['channel_from_extension'] in CHANNELS:
                gid = 'group:' + '/'.join((row['source_role'], row['source_label'], str(PurePosixPath(row['relative_path']).with_suffix(''))))
                files[gid].append((rec['id'], row))
        elif rec['record_type'] == 'miniseed_headers':
            headers[rec['file_id']] = rec['data']
    linked, graph = defaultdict(list), defaultdict(set)
    for comparison in comparisons:
        a, b = comparison['group_ids']
        linked[a].append(comparison)
        linked[b].append(comparison)
        if comparison['status'] == 'identical_files':
            graph[a].add(b)
            graph[b].add(a)
    output, details = {}, {}
    for gid, group in groups.items():
        timing = recording_time(group, files[gid], headers, filename_zone)
        prefix_key = canonical_station(timing['prefix']).casefold()
        filename_station = stations.get(prefix_key, {}).get('station', '')
        candidates = {group['source_label'].casefold()}
        if filename_station:
            candidates.add(prefix_key)
        for comp in linked[gid]:
            candidates.update(groups[g]['source_label'].casefold() for g in comp['group_ids'])
        checks = window_checks(candidates, stations, timing)
        inside = sorted({c['station'] for c in checks if c['relation'] == 'within'})
        flags = set(timing['flags'])
        if not filename_station:
            flags.add('filename_does_not_identify_a_metadata_station')
        if filename_station and filename_station.casefold() != group['source_label'].casefold():
            flags.add('filename_station_differs_from_folder')
        if any(c['status'] != 'identical_files' for c in linked[gid]):
            flags.add('nonidentical_candidate_requires_review')
        if group.get('missing_channel_names') or group.get('repeated_channel_names'):
            flags.add('channel_names_require_timing_qc')
        matching = [c for c in checks if c['station'].casefold() == prefix_key and c['relation'] == 'within'
                    and not c.get('header_last_sample_outside')]
        if filename_station and not matching:
            flags.add('filename_station_time_not_supported_by_metadata_window')
        timing_blockers = {'filename_header_start_conflict', 'invalid_header_timing', 'header_read_problem',
                           'channel_header_windows_differ', 'nonidentical_candidate_requires_review'}
        supported = bool(filename_station and matching and not flags.intersection(timing_blockers))
        normal = supported and filename_station.casefold() == group['source_label'].casefold() and not linked[gid]
        row = dict.fromkeys(COLUMNS, '')
        row.update(source_folder=group['source_label'], source_role=group['source_role'], recording_group=group['group_key'],
                   filename_station=filename_station, filename_start_utc_assumed=timing['filename_start_utc_assumed'],
                   header_start_utc=timing['header_start_utc'], header_end_exclusive_utc=timing['header_end_exclusive_utc'],
                   candidate_stations=';'.join(stations.get(c, {}).get('station', c) for c in sorted(candidates)),
                   metadata_windows_containing_start=';'.join(inside), applies_to='Level 0 station selection',
                   assigned_station=filename_station if normal else '', decision='retain_for_qc' if normal else 'unresolved',
                   retained_group_id=gid if normal else '',
                   reason='Filename matches folder and its start is within the metadata recording window; continue with timing/QC.' if normal else
                          'Station/copy assignment requires review; no exclusion decision applied.',
                   comparison_ids=';'.join(sorted(c['id'] for c in linked[gid])), group_id=gid)
        output[gid] = row
        details[gid] = {'record_type': 'station_decision', 'id': 'decision:' + gid.removeprefix('group:'),
                        'group_id': gid, 'timing': timing, 'metadata_window_checks': checks,
                        'flags': flags, '_supported': supported, '_prefix_key': prefix_key}
    visited = set()
    for first in sorted(graph):
        if first in visited:
            continue
        todo, component = [first], set()
        while todo:
            item = todo.pop()
            if item not in component:
                component.add(item)
                todo.extend(graph[item] - component)
        visited.update(component)
        owners = {details[g]['_prefix_key'] for g in component if output[g]['filename_station']}
        eligible = len(owners) == 1 and all(details[g]['_supported'] for g in component)
        owner = next(iter(owners)) if len(owners) == 1 else None
        preferred = [g for g in component if groups[g]['source_label'].casefold() == owner]
        if not eligible or len(preferred) != 1:
            for g in component:
                details[g]['flags'].add('exact_copy_group_needs_station_assignment')
            continue
        retained = preferred[0]
        canonical = stations[owner]['station']
        for g in component:
            row = output[g]
            row.update(assigned_station=canonical, retained_group_id=retained)
            if g == retained:
                row.update(decision='retain_for_qc', reason='Retain the verified exact copy in the folder matching its filename station; metadata start-window check agrees.')
            else:
                row.update(decision='exclude_duplicate', reason='Five-channel exact copy; filename station and metadata start-window check support the retained copy.',
                           metadata_comment=f'Duplicate recordings assigned to {canonical} were excluded from the {row["source_folder"]} output.')
                details[g]['flags'].discard('filename_station_differs_from_folder')
    rows, evidence = [], []
    for gid in sorted(groups, key=lambda g: (output[g]['source_folder'].casefold(), output[g]['recording_group'], g)):
        row, detail = output[gid], details[gid]
        row['review_flags'] = ';'.join(sorted(detail.pop('flags')))
        detail.pop('_supported')
        detail.pop('_prefix_key')
        detail['decision'] = row.copy()
        rows.append(row)
        evidence.append(detail)
    return rows, evidence
