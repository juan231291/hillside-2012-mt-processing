"""Shared recorder parser. Anchors use independent filename/metadata evidence.

Read-only parsing and context lookup. This module writes no reports or decisions.
No waveform reads, timestamp corrections, rate inference or metadata updates.
Python 3.10+; standard library only.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import stat
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

STAMP = re.compile(r'^(\d{2}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})\s+(.*)$')
WRITE = re.compile(r'New Data File /data/(\S+) (\d+) seconds\s*$')
SERIAL = re.compile(r'Serial no\s+(\d+)\s*$')
CONFIG = re.compile(r'(station_long_identifier|channel_[0-5]_(?:long_id|short_id|samplerate|format))\s*=\s*(.*?)\s*$')
MAX_SOURCE_BYTES = 32 * 1024**2


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def iso(t):
    return t.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z') if t else ''


def parse_utc(text):
    if not text:
        return None
    t = datetime.fromisoformat(text.replace('Z', '+00:00'))
    if t.utcoffset() is None:
        raise ValueError('Evidence timestamp lacks a timezone')
    return t.astimezone(timezone.utc)


def utc_offset(text):
    if not re.fullmatch(r'[+-]\d{2}:\d{2}', text):
        raise ValueError('Log UTC offset must look like +00:00')
    h, m = map(int, text[1:].split(':'))
    if h > 14 or m > 59 or (h == 14 and m):
        raise ValueError('Invalid log UTC offset')
    return timezone((1 if text[0] == '+' else -1) * timedelta(hours=h, minutes=m))


def jsonl(path):
    with path.open(encoding='utf-8-sig') as f:
        return [json.loads(line) for line in f if line.strip()]


def source_file(row, root):
    rel = PurePosixPath(row['relative_path'])
    if row['entry_type'] != 'regular_file' or rel.is_absolute() or '..' in rel.parts or '\\' in str(rel):
        raise ValueError('Unexpected inventory path or entry type')
    path = root.joinpath(*rel.parts)
    if any(p.is_symlink() for p in (path, *path.parents) if p != root and root in p.parents):
        raise ValueError('Nested source links require review')
    actual = path.resolve(strict=True)
    s = actual.stat()
    if root not in actual.parents or not stat.S_ISREG(s.st_mode):
        raise ValueError('Source leaves its root or is not a regular file')
    if s.st_size != int(row['bytes']):
        raise ValueError('Source size changed since Step 01')
    if s.st_size > MAX_SOURCE_BYTES:
        raise ValueError('Log/config exceeds the 32 MiB read limit')
    before = (s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_ino)
    data = actual.read_bytes()
    s = actual.stat()
    if before != (s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_ino) or len(data) != s.st_size:
        raise ValueError('Source changed while reading')
    return actual, data


def parse_log(text, zone):
    events, sessions, serials = [], {}, set()
    session = {'start_line': 1, 'serial_events': [], 'context_events': []}
    sessions[1] = session
    current_serial = None
    for n, line in enumerate(text.splitlines(), 1):
        match = STAMP.match(line)
        if not match:
            continue
        try:
            time = datetime.strptime(match[1], '%d/%m/%y %H:%M:%S').replace(tzinfo=zone)
        except ValueError as exc:
            raise ValueError(f'Invalid log timestamp at line {n}: {exc}') from exc
        message = match[2]
        point = {'line': n, 'text': line, 'time_utc_assumed': iso(time)}
        if message.startswith('Recorder acquisition version'):
            session = {'start_line': n, 'serial_events': [], 'context_events': []}
            sessions[n] = session
            current_serial = None  # Never borrow a serial from an earlier restart.
        serial = SERIAL.fullmatch(message)
        if serial:
            current_serial = {**point, 'serial': serial[1].lstrip('0') or '0'}
            serials.add(current_serial['serial'])
            session['serial_events'].append(current_serial)
        lower = message.lower()
        if any(s in lower for s in ('pc time set to gps', 'digitiser set to pc', 'gps discontin', 'inifile has been changed', 'process will restart')):
            session['context_events'].append(point)
        write = WRITE.fullmatch(message)
        if write:
            path = PurePosixPath(write[1])
            if path.is_absolute() or '..' in path.parts:
                raise ValueError(f'Unexpected recorded file path at line {n}')
            events.append({**point, 'recorder_key': str(path), 'logged_seconds': int(write[2]),
                           'session_start_line': session['start_line'],
                           'serial': current_serial['serial'] if current_serial else None,
                           'serial_line': current_serial['line'] if current_serial else None})
    return events, sessions, sorted(serials)


def recorder_key(group):
    parts = PurePosixPath(group).parts
    return '/'.join(parts[-2:])  # Logs use day/stem, including when the copy is nested.


def logger_matches(meta, serial):
    return [w for w in meta.get('windows', []) if str(w.get('logger_id', '')).lstrip('0') == serial]


def inspect_log(source_id, parsed, rows, metadata):
    events, sessions, _ = parsed
    known = defaultdict(list)
    for row in rows:
        if row.get('anchor_station'):
            known[recorder_key(row['recording_group'])].append(row)
    anchors, lines = [], defaultdict(list)
    for event in events:
        matches = []
        for row in known.get(event['recorder_key'], []):
            meta = metadata.get(row['anchor_station'].casefold(), {})
            start = parse_utc(row['header_start_utc'] or row['filename_start_utc_assumed'])
            windows = logger_matches(meta, event['serial']) if event['serial'] else []
            if start and any(w['valid'] and parse_utc(w['start_utc']) <= start < parse_utc(w['end_utc']) for w in windows):
                matches.append(row)
        if len({r['anchor_station'].casefold() for r in matches}) != 1:
            continue
        owner = matches[0]['anchor_station']
        start = parse_utc(matches[0]['header_start_utc'] or matches[0]['filename_start_utc_assumed'])
        anchor = {**event, 'station': owner, 'supporting_group_ids': sorted(r['group_id'] for r in matches),
                  'recording_start_utc': iso(start), 'source_file_id': source_id}
        anchors.append(anchor)
        lines[(event['serial'], start.date().isoformat())].append(anchor)
    indices = {key: [a['line'] for a in aa] for key, aa in lines.items()}
    direct = {a['line']: [a] for a in anchors}
    result = defaultdict(list)
    for event in events:
        day = parse_utc(event['time_utc_assumed']).date().isoformat()
        key = (event['serial'], day)
        aa, nn = lines.get(key, []), indices.get(key, [])
        nearest = []
        before, after = bisect_left(nn, event['line']) - 1, bisect_right(nn, event['line'])
        if before >= 0:
            nearest.append({'side': 'preceding', **aa[before]})
        if after < len(aa):
            nearest.append({'side': 'following', **aa[after]})
        context = sessions[event['session_start_line']]
        result[event['recorder_key']].append({**event, 'source_file_id': source_id,
                                              'nearest_known_recordings': nearest,
                                              'matching_filename_recordings': direct.get(event['line'], []),
                                              'session': context})
    return result
