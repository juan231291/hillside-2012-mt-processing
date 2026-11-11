#!/usr/bin/env python3
"""Split Hillside Level-0 ASCII while EXCLUDING HSSL03 from bulk processing.

Inputs: HS005_run_0001.txt with columns described in '# columns: ...'.
Outputs: HS005_run_0001.{EX,EY,BX,BY,BZ} for the *available* channels.

The original source TXT is removed only after all selected channel output files
are written, counted and independently verified from disk. Zero samples are
invented: e.g. a four-channel run missing EY exports four channels.

Run identifiers are checked against the tq84 backup per station, not against
an earlier hard-coded survey run total (212, later verified as 210).

This is a post-MTH5, publication-format correction. The --exclude-station
option is used to leave HSSL03 unchanged during the bulk conversion.
Running this against an original Step-07 ASCII workspace before Step 10
would break the historical MTH5 reader, which expects a combined TXT
and commented header.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import hashlib
import os
from pathlib import Path
import re
import sys
import time

DEFAULT_ROOT = Path(
    '/g/data/my80/admin/staging/AuScope_Broadband/States_and_Territories/'
    'South_Australia/Hillside_2012/Level_0/ASCII'
)
DEFAULT_BACKUP = Path(
    '/g/data/tq84/jh7824/Hillside_survey/Hillside_survey/Level_0/ASCII'
)
COMPONENTS = ('EX', 'EY', 'BX', 'BY', 'BZ')
NUM_STATIONS = 73
# Run totals are derived from the authoritative tq84 backup, not hard-coded.
BUFFER_ROWS = 20_000
READ_BUFFER_BYTES = 4 * 1024 * 1024
RUN_PATTERN = re.compile(r'^(.+)_run_\d{4}$')
INTEGER = re.compile(rb'[+-]?\d+\Z')


class ConversionError(RuntimeError):
    pass


def header_info(source: Path) -> tuple[dict[str, str], tuple[str, ...]]:
    """Inspect the comment header and first row without reading the full data."""
    header: dict[str, str] = {}
    first_row = None
    with source.open('rb', buffering=READ_BUFFER_BYTES) as stream:
        for _ in range(100):
            row = stream.readline()
            if not row:
                raise ConversionError(f'{source}: missing numeric rows')
            if row.startswith(b'#'):
                kv = row[1:].strip().split(b':', 1)
                if len(kv) == 2:
                    key, value = kv
                    header[key.decode('ascii').strip()] = value.decode('ascii').strip()
                continue
            if not row.strip():
                raise ConversionError(f'{source}: unexpected blank row')
            first_row = row.split()
            break
    if first_row is None:
        raise ConversionError(f'{source}: no first numerical row')
    if header.get('schema') != 'mtl0.ascii_run/1':
        raise ConversionError(f'{source}: unexpected schema')
    if header.get('sample_representation') != 'signed_int64':
        raise ConversionError(f'{source}: unexpected sample representation')
    columns = tuple(header.get('columns', '').split())
    if not 1 <= len(columns) <= 5 or len(set(columns)) != len(columns):
        raise ConversionError(f'{source}: invalid channel set: {columns}')
    if not set(columns).issubset(COMPONENTS):
        raise ConversionError(f'{source}: unknown channel in {columns}')
    if tuple(c for c in COMPONENTS if c in columns) != columns:
        raise ConversionError(f'{source}: unexpected channel order {columns}')
    if len(first_row) != len(columns) or any(not INTEGER.fullmatch(t) for t in first_row):
        raise ConversionError(f'{source}: invalid first numeric row')
    for token in first_row:
        if not -(2**63) <= int(token) <= 2**63 - 1:
            raise ConversionError(f'{source}: int64 overflow')
    try:
        count = int(header['samples_per_channel'])
    except (ValueError, KeyError) as exc:
        raise ConversionError(f'{source}: invalid sample count') from exc
    if count <= 0:
        raise ConversionError(f'{source}: sample count <= 0')
    if header.get('station') != source.parent.name:
        raise ConversionError(f'{source}: station header differs from folder')
    if header.get('run_id') != source.stem[len(source.parent.name)+1:]:
        raise ConversionError(f'{source}: run ID header differs from file name')
    return header, columns


def source_backup(source: Path, backup_root: Path) -> Path:
    backup = backup_root / source.parent.name / source.name
    if not backup.is_file():
        raise ConversionError(f'Backup source is unavailable: {backup}')
    if source.exists() and source.stat().st_size != backup.stat().st_size:
        raise ConversionError(f'Backup size differs: {source}')
    return backup


def station_runs(station_dir: Path):
    sources = []
    channels = {}
    for entry in station_dir.iterdir():
        if not entry.is_file():
            continue
        ext = entry.suffix[1:].upper()
        if ext not in ('TXT', *COMPONENTS):
            continue
        match = RUN_PATTERN.fullmatch(entry.stem)
        if not match or match.group(1) != station_dir.name:
            raise ConversionError(f'Unexpected run filename: {entry}')
        if ext == 'TXT':
            sources.append(entry)
        else:
            channels.setdefault(entry.stem, set()).add(ext)
    sources.sort()
    source_stems = {p.stem for p in sources}
    for stem in channels:
        if stem in source_stems:
            raise ConversionError(f'{station_dir}: mixed TXT and output channels for {stem}; inspect manually')
    return sources, channels


def backup_run_names(backup_root: Path, station_name: str) -> set[str]:
    """Get all backed-up per-run TXT identifiers for one station.

    Backup is only read. The staged run inventory must exactly match it,
    including after individual TXT files have become five channel files.
    """
    folder = backup_root / station_name
    if not folder.is_dir():
        raise ConversionError(f'Backup station folder is unavailable: {folder}')
    names = set()
    for entry in folder.iterdir():
        if entry.is_file() and entry.suffix.lower() == '.txt':
            match = RUN_PATTERN.fullmatch(entry.stem)
            if not match or match.group(1) != station_name:
                raise ConversionError(f'Unexpected backup TXT filename: {entry}')
            names.add(entry.stem)
    if not names:
        raise ConversionError(f'No backed-up runs for {station_name}: {folder}')
    return names


def inventory(root: Path, backup_root: Path, station: str | None,
              exclude_station: str | None = None):
    if not root.is_dir() or not backup_root.is_dir():
        raise ConversionError('Stage directory or backup directory is not accessible')
    if station and exclude_station:
        raise ConversionError('--station and --exclude-station cannot be combined')
    all_stations = sorted(p for p in root.iterdir() if p.is_dir())
    if station:
        stations = [p for p in all_stations if p.name == station]
        if not stations:
            raise ConversionError(f'Station {station} not found')
    else:
        if len(all_stations) != NUM_STATIONS:
            raise ConversionError(f'Expected {NUM_STATIONS} stations but found {len(all_stations)}')
        stations = all_stations
    excluded_runs = 0
    if exclude_station:
        excluded = [p for p in all_stations if p.name == exclude_station]
        if len(excluded) != 1:
            raise ConversionError(f'Excluded station {exclude_station} not found')
        excluded_sources, excluded_finished = station_runs(excluded[0])
        excluded_names = {p.stem for p in excluded_sources} | set(excluded_finished)
        backup_excluded_names = backup_run_names(backup_root, exclude_station)
        if excluded_names != backup_excluded_names:
            raise ConversionError(
                f'{exclude_station}: run names differ from backup; inspect separately'
            )
        excluded_runs = len(excluded_names)
        # Current historical Hillside inventory: HSSL03 contains three runs.
        # Count names only; never read, convert or delete its data in this job.
        if exclude_station == 'HSSL03' and excluded_runs != 3:
            raise ConversionError(f'HSSL03 has {excluded_runs} runs; expected 3. Inspect first.')
        stations = [p for p in stations if p.name != exclude_station]
        if not stations:
            raise ConversionError('No stations remain after exclusion')
        print(f'EXCLUDING {exclude_station}: {excluded_runs} runs untouched', flush=True)
    run_total = 0
    channel_total = 0
    pending_total = 0
    for stn in stations:
        sources, finished = station_runs(stn)
        staged_names = {p.stem for p in sources} | set(finished)
        backup_names = backup_run_names(backup_root, stn.name)
        if staged_names != backup_names:
            missing = sorted(backup_names - staged_names)
            extra = sorted(staged_names - backup_names)
            raise ConversionError(
                f'{stn.name}: run identifiers differ from tq84 backup; '
                f'missing in my80={missing}; extra in my80={extra}'
            )
        for source in sources:
            source_backup(source, backup_root)
            _, order = header_info(source)
            channel_total += len(order)
            pending_total += 1
        for stem, present in finished.items():
            original = stn / (stem + '.txt')
            backup = source_backup(original, backup_root)
            _, expected = header_info(backup)
            if present != set(expected):
                raise ConversionError(f'{stn}/{stem}: existing channel set {present} != expected {set(expected)}')
            channel_total += len(expected)
        run_total += len(sources) + len(finished)
    print(f'PREFLIGHT PASS: stations={len(stations)}, runs={run_total}, channels={channel_total}, '
          f'pending_runs={pending_total}; run names match tq84 backup', flush=True)
    return stations, run_total, channel_total, pending_total


def flush_buffers(buffers, outputs, digests):
    for buf, out, digest in zip(buffers, outputs, digests):
        if buf:
            out.write(buf)
            digest.update(buf)
            buf.clear()


def disk_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb', buffering=READ_BUFFER_BYTES) as stream:
        while part := stream.read(READ_BUFFER_BYTES):
            digest.update(part)
    return digest.hexdigest()


def convert_run(source: Path, backup_root: Path):
    start = time.monotonic()
    source_backup(source, backup_root)
    header, order = header_info(source)
    expected_count = int(header['samples_per_channel'])
    finals = [source.with_suffix('.' + name) for name in order]
    parts = [Path(str(final) + '.part') for final in finals]
    for final in finals:
        if final.exists():
            raise ConversionError(f'Output exists; will not overwrite: {final}')
    for part in parts:
        if part.exists():
            part.unlink()  # interrupted uncommitted output; source TXT is still present
    digests = [hashlib.sha256() for _ in order]
    buffers = [bytearray() for _ in order]
    streams = []
    count = 0
    try:
        streams = [part.open('xb', buffering=READ_BUFFER_BYTES) for part in parts]
        with source.open('rb', buffering=READ_BUFFER_BYTES) as reader:
            in_header = True
            for row in reader:
                if in_header and row.startswith(b'#'):
                    continue
                in_header = False
                tokens = row.split()
                if len(tokens) != len(order):
                    raise ConversionError(f'{source}: row {count+1} has {len(tokens)} columns, expected {len(order)}')
                for i, token in enumerate(tokens):
                    if not INTEGER.fullmatch(token) or not -(2**63) <= int(token) <= 2**63-1:
                        raise ConversionError(f'{source}: invalid signed int64 on row {count+1}')
                    buffers[i].extend(token)
                    buffers[i].append(10)
                count += 1
                if count % BUFFER_ROWS == 0:
                    flush_buffers(buffers, streams, digests)
            flush_buffers(buffers, streams, digests)
        if count != expected_count:
            raise ConversionError(f'{source}: {count} rows, expected {expected_count}')
        for stream in streams:
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
        streams = []
        for part, digest in zip(parts, digests):
            if disk_sha256(part) != digest.hexdigest():
                raise ConversionError(f'Channel readback SHA-256 mismatch: {part}')
        for part, final in zip(parts, finals):
            os.replace(part, final)
        source.unlink()
    except Exception:
        for stream in streams:
            try:
                stream.close()
            except Exception:
                pass
        for part in parts:
            part.unlink(missing_ok=True)
        raise
    return source.name, count, len(order), time.monotonic()-start


def convert_station(station_path: str, backup_path: str):
    station = Path(station_path)
    sources, complete = station_runs(station)
    new_runs = 0
    for source in sources:
        name, count, channels, seconds = convert_run(source, Path(backup_path))
        print(f'PASS {station.name}/{name}: samples/channel={count}, channels={channels}, seconds={seconds:.1f}',flush=True)
        new_runs += 1
    return station.name, new_runs, len(complete)


def audit(stations, expected_runs, expected_channels, backup_root):
    run_total = channel_total = 0
    for station in stations:
        sources, finished = station_runs(station)
        if sources:
            raise ConversionError(f'{station}: {len(sources)} unfinished TXT runs')
        leftovers = list(station.glob('*.part'))
        if leftovers:
            raise ConversionError(f'{station}: stale .part files')
        for stem, present in finished.items():
            backup = source_backup(station / (stem+'.txt'), backup_root)
            _, expected = header_info(backup)
            if present != set(expected):
                raise ConversionError(f'{station}/{stem}: incomplete channel outputs')
            channel_total += len(expected)
        run_total += len(finished)
    if (run_total, channel_total) != (expected_runs,expected_channels):
        raise ConversionError('Audit totals differ from preflight inventory')
    print(f'FINAL STRUCTURE PASS: {len(stations)} stations, {run_total} runs, '
          f'{channel_total} channel files; no original TXT remaining.',flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=DEFAULT_ROOT)
    p.add_argument('--backup-root',type=Path,default=DEFAULT_BACKUP)
    p.add_argument('--station',help='Pilot on one station, e.g. HS005')
    p.add_argument('--exclude-station',help='Leave this station untouched, e.g. HSSL03')
    p.add_argument('--workers',type=int,default=1)
    p.add_argument('--check',action='store_true')
    p.add_argument('--audit',action='store_true')
    a = p.parse_args()
    if not 1 <= a.workers <= 16:
        p.error('workers must be 1..16')
    stations,runs,channels,pending = inventory(
        a.root, a.backup_root, a.station, a.exclude_station)
    if a.audit:
        audit(stations,runs,channels,a.backup_root)
        return 0
    if a.check:
        return 0
    failures=[]
    if pending:
        with ProcessPoolExecutor(max_workers=a.workers) as pool:
            futures={pool.submit(convert_station,str(stn),str(a.backup_root)):stn for stn in stations}
            for future in as_completed(futures):
                try:
                    station, new, old = future.result()
                    print(f'STATION COMPLETE {station}: new={new} already={old}',flush=True)
                except Exception as exc:
                    msg=f'{futures[future].name}: {exc}'
                    failures.append(msg)
                    print('ERROR:',msg,file=sys.stderr,flush=True)
    if failures:
        raise ConversionError('Station failures: '+'; '.join(failures))
    audit(stations,runs,channels,a.backup_root)
    return 0


if __name__=='__main__':
    try:
        sys.exit(main())
    except (ConversionError,OSError) as exc:
        print('FAILED:',exc,file=sys.stderr,flush=True)
        sys.exit(1)
