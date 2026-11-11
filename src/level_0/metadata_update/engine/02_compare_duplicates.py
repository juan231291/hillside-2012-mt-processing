"""Compare name-matched recording groups from an existing MT survey inventory.

Compare complete bytes first, then decoded numeric samples when bytes differ.
Timing and identity evidence is separate from numeric equality. This does not
search different names, resolve ownership, or change any source file.
Python 3.10+; NumPy, ObsPy and openpyxl are required.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import stat
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from sample_comparison import (MAX_DECODED_SAMPLES, MAX_MSEED_BYTES,
                               compare_samples, file_stamp)

CHANNELS = ('ex', 'ey', 'bx', 'by', 'bz')


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def inventory(path):
    """Load the old four reports or the new detailed evidence file."""
    if path.is_dir() and (path / 'inventory_evidence.jsonl').is_file():
        path = path / 'inventory_evidence.jsonl'
    if path.is_file():
        with path.open(encoding='utf-8-sig') as f:
            records = [json.loads(line) for line in f if line.strip()]
        roots = [r['data'] for r in records if r.get('record_type') == 'source']
        files = [r['data'] for r in records if r.get('record_type') == 'file']
        groups = [r['data'] for r in records if r.get('record_type') == 'recording_group']
        manifest = next(r for r in records if r.get('id') == 'manifest:inventory')
        summary = manifest['original_summary']
        if manifest.get('schema') != 'mtl0.inventory.evidence/1':
            raise ValueError('Unsupported evidence schema')
        inputs = {str(path): digest(path)}
    else:
        names = ('files.csv', 'recording_groups.csv', 'miniseed_headers.json', 'summary.json')
        inputs = {str(path / name): digest(path / name) for name in names}
        def read_csv(name):
            with (path / name).open(encoding='utf-8-sig', newline='') as f:
                return list(csv.DictReader(f))
        files, groups = read_csv('files.csv'), read_csv('recording_groups.csv')
        summary = json.loads((path / 'summary.json').read_text(encoding='utf-8'))
        roots = summary['sources']
        headers = json.loads((path / 'miniseed_headers.json').read_text(encoding='utf-8'))
        records = [{'record_type': 'manifest', 'id': 'manifest:inventory',
                    'schema': 'mtl0.inventory.evidence/1', 'original_summary': summary}]
        records += [{'record_type': 'source', 'id': f"source:{r['role']}/{r['label']}", 'data': r} for r in roots]
        for kind, name, rows in (('file', 'files.csv', files), ('recording_group', 'recording_groups.csv', groups)):
            for i, row in enumerate(rows, 2):
                key = row['relative_path'] if kind == 'file' else row['group_key']
                prefix = 'file' if kind == 'file' else 'group'
                records.append({'record_type': kind, 'id': f"{prefix}:{row['source_role']}/{row['source_label']}/{key}",
                                'source_id': f"source:{row['source_role']}/{row['source_label']}",
                                'source_report': name, 'source_row': i, 'data': row})
        records += [{'record_type': 'miniseed_headers', 'id': 'headers:' + key,
                     'file_id': 'file:' + key, 'data': value} for key, value in headers.items()]
    if summary.get('schema') != 'mtl0.inventory/1' or not summary.get('inventory_complete'):
        raise ValueError('A complete, supported Step 01 inventory is required')
    if any(r.get('record_type') == 'name_match_comparison' for r in records):
        raise ValueError('Use the original inventory, not a previous comparison output')
    ids = [r.get('id') for r in records]
    if None in ids or len(ids) != len(set(ids)):
        raise ValueError('Missing or duplicate inventory evidence IDs')
    source_ids = {'source:' + r['role'] + '/' + r['label'] for r in roots}
    if len(source_ids) != len(roots):
        raise ValueError('Duplicate inventory sources')
    for rec in records:
        kind = rec.get('record_type')
        if kind in ('file', 'recording_group'):
            row = rec['data']
            sid = 'source:' + row['source_role'] + '/' + row['source_label']
            relative = row['relative_path'] if kind == 'file' else row['group_key']
            expected = ('file:' if kind == 'file' else 'group:') + sid.removeprefix('source:') + '/' + relative
            if rec['id'] != expected or sid not in source_ids:
                raise ValueError('Inventory identity/link mismatch')
    for source in roots:
        key = source['role'], source['label']
        ff = [r for r in files if (r['source_role'], r['source_label']) == key]
        gg = [r for r in groups if (r['source_role'], r['source_label']) == key]
        if len(ff) != source['entries'] or len(gg) != source['em_filename_groups']:
            raise ValueError('Inventory source counts disagree')
    return roots, files, groups, records, inputs


def source_key(row):
    return row['source_role'], row['source_label']


def group_key(row):
    return (*source_key(row), row['group_key'])


def source_path(row, roots):
    if row['entry_type'] != 'regular_file':
        raise ValueError('Inventory entry is not a regular file')
    rel = PurePosixPath(row['relative_path'])
    if rel.is_absolute() or '..' in rel.parts or '\\' in row['relative_path']:
        raise ValueError('Unexpected relative path in inventory')
    root = roots[source_key(row)]
    path = root.joinpath(*rel.parts)
    if any(p.is_symlink() for p in (path, *path.parents) if p != root and root in p.parents):
        raise ValueError('Nested source link requires review')
    resolved = path.resolve(strict=True)
    if root not in resolved.parents:
        raise ValueError('Path leaves the source directory')
    info = resolved.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('Source is not a regular file')
    if info.st_size != int(row['bytes']):
        raise ValueError('File size changed since the inventory; refresh the inventory')
    if info.st_size == 0:
        raise ValueError('Empty channel file; equality of two empty files is not a recording duplicate')
    return resolved


def compare_bytes(left, right):
    before = [p.stat() for p in (left, right)]
    hashes, counts, same = [hashlib.sha256(), hashlib.sha256()], [0, 0], True
    with left.open('rb') as a, right.open('rb') as b:
        while True:
            x, y = a.read(1024 * 1024), b.read(1024 * 1024)
            if not x and not y:
                break
            same = same and x == y
            for i, chunk in enumerate((x, y)):
                hashes[i].update(chunk)
                counts[i] += len(chunk)
    for p, old, count in zip((left, right), before, counts):
        now = p.stat()
        if (old.st_size, old.st_mtime_ns, old.st_ino) != (now.st_size, now.st_mtime_ns, now.st_ino) or count != now.st_size:
            raise ValueError('Source changed during comparison')
    return {'identical_bytes': same, 'bytes_read': counts, 'sha256': [h.hexdigest() for h in hashes]}


def excel_report(path, sources, results):
    from openpyxl import Workbook, load_workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = 'Duplicate review'
    ws.sheet_view.showGridLines = False
    ws.append(['Name-matched recording review'])
    ws.append(['Exact copies checked first; differing files compared by sample value. Ownership remains undecided.'])
    ws.append([])
    ws.append(['Folder', 'Role', 'Pairs checked', 'Exact copies', 'Same samples', 'Different samples',
               'Read problems', 'Timing / ID flags', 'Interpretation'])
    for s in sources:
        key = (s['role'], s['label'])
        rr = [r for r in results if key in (tuple(r['left'][:2]), tuple(r['right'][:2]))]
        counts = Counter(r['status'] for r in rr)
        flagged = sum(bool(r['context_flags']) for r in rr)
        note = 'Sample equality does not establish timing, units or ownership.' if rr else 'No name candidates. Other duplicates have not been assessed.'
        ws.append([s['label'], s['role'], len(rr), counts['identical_files'] if rr else None,
                   counts['same_samples'] if rr else None, counts['different_samples'] if rr else None,
                   counts['file_problem'] if rr else None, flagged if rr else None, note])
    end = ws.max_row
    ws.auto_filter.ref = f'A4:I{end}'
    ws.freeze_panes = 'C5'
    ws.append([])
    for note in (
        'Pairs appear under both folders. Do not sum the folder rows to count unique comparisons.',
        'Exact copies: all five EM files are nonempty and byte-identical. No extra decoding or waveform validation is needed to prove a file copy.',
        'Same samples: bytes differ, but all five channels have equal values and counts. Different samples: at least one channel differs.',
        'Read problems: missing/repeated channels, empty/unreadable files, unsupported decoding, decode limits or changed sources.',
        'Timing / ID flags count pairs with MiniSEED header conflicts, gaps or changing identities/rates; this column overlaps the outcome counts.',
        'ASCII values have no embedded timestamps: timing remains unverified. Equal values alone are not a station-ownership decision.',
        'Scope: only cross-folder name candidates in Step 01. Different names, same-folder copies and partial overlaps are not searched here.',
        'Details: duplicate_evidence.jsonl contains sample counts, checksums, first difference, MiniSEED headers, errors and original inventory evidence.',
    ):
        ws.append([note])
    for row in ws:
        for c in row:
            if isinstance(c.value, str):
                c.data_type = 's'  # Source labels are literal text, never Excel formulas.
            c.font = Font(name='Arial', size=10, color='243244')
            c.alignment = Alignment(vertical='center')
    ws['A1'].font = Font(name='Arial', size=15, bold=True, color='163B50')
    for c in ws[4]:
        c.fill = PatternFill('solid', fgColor='163B50')
        c.font = Font(name='Arial', size=10, bold=True, color='FFFFFF')
        c.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
    for row in range(5, end + 1):
        ws.row_dimensions[row].height = 38
        ws.cell(row, 9).alignment = Alignment(wrap_text=True, vertical='center')
        for col in range(3, 9):
            ws.cell(row, col).number_format = '#,##0"  "'
        for col in (6, 7, 8):
            c = ws.cell(row, col)
            if c.value:
                c.fill = PatternFill('solid', fgColor='FFF0C2')
    for col, width in zip('ABCDEFGHI', (13, 12, 13, 13, 14, 15, 14, 16, 51)):
        ws.column_dimensions[col].width = width
    ws.row_dimensions[4].height = 28
    wb.save(path)
    check = load_workbook(path, read_only=True, data_only=True)
    assert list(check.active.values)[3:end] == list(ws.values)[3:end]
    check.close()


def run(input_path, out, max_mseed_bytes=MAX_MSEED_BYTES, max_decoded_samples=MAX_DECODED_SAMPLES):
    if out.exists():
        raise FileExistsError('Output already exists: ' + str(out))
    # Fail before reading source data if a required dependency is unavailable.
    import openpyxl
    import numpy
    import obspy
    if max_mseed_bytes <= 0 or max_decoded_samples <= 0:
        raise ValueError('Decode limits must be positive')
    sources, files, groups, inherited, hashes = inventory(input_path)
    roots = {(r['role'], r['label']): Path(r['resolved_root']).resolve(strict=True) for r in sources}
    for root in roots.values():
        if not root.is_dir():
            raise ValueError('Missing source directory: ' + str(root))
    target = out.resolve()
    if any(target == root or root in target.parents for root in roots.values()):
        raise ValueError('Output must be outside the raw source folders')
    gg = {group_key(row): row for row in groups}
    if len(gg) != len(groups):
        raise ValueError('Repeated recording group IDs in inventory')
    grouped = defaultdict(lambda: defaultdict(list))
    for row in files:
        if row['channel_from_extension'] in CHANNELS:
            key = (*source_key(row), str(PurePosixPath(row['relative_path']).with_suffix('')))
            grouped[key][row['channel_from_extension']].append(row)
    pairs = set()
    for key, row in gg.items():
        for match in filter(None, row['same_stem_in_other_sources'].split(';')):
            other = tuple(match.split('/', 2))
            if other not in gg or other[:2] == key[:2]:
                raise ValueError('Invalid cross-folder candidate: ' + match)
            pairs.add(tuple(sorted((key, other))))
    out.mkdir(parents=True, exist_ok=False)
    results = []
    print(f'Comparing {len(pairs)} unique name-matched group pairs', flush=True)
    for number, (left, right) in enumerate(sorted(pairs), 1):
        channels = []
        for channel in CHANNELS:
            rec = {'channel': channel}
            try:
                members = [grouped[key][channel] for key in (left, right)]
                if any(len(rows) != 1 for rows in members):
                    raise ValueError('Missing or repeated channel name in a candidate group')
                rows = [m[0] for m in members]
                rec['file_ids'] = ['file:' + '/'.join((*source_key(row), row['relative_path'])) for row in rows]
                paths = [source_path(row, roots) for row in rows]
                before = [file_stamp(p) for p in paths]
                rec.update(compare_bytes(*paths))
                if rec['identical_bytes']:
                    rec['sample_comparison'] = {
                        'method': 'not_needed_for_exact_copy', 'equal_samples': None,
                        'timing_status': 'not_revalidated_for_exact_copy', 'context_flags': [],
                        'note': 'Full byte equality proves an exact file copy; no new decoding or waveform-validity claim.'}
                else:
                    rec['sample_comparison'] = compare_samples(
                        *paths, channel, max_mseed_bytes=max_mseed_bytes,
                        max_decoded_samples=max_decoded_samples)
                if before != [file_stamp(p) for p in paths]:
                    raise ValueError('Source changed across the byte/sample comparison passes')
            except (OSError, ValueError) as exc:
                rec['error'] = str(exc)
            channels.append(rec)
        if any('error' in c for c in channels):
            status = 'file_problem'
        elif all(c['identical_bytes'] for c in channels):
            status = 'identical_files'
        elif all(c['identical_bytes'] or c['sample_comparison']['equal_samples'] for c in channels):
            status = 'same_samples'
        else:
            status = 'different_samples'
        flags = sorted({flag for c in channels for flag in c.get('sample_comparison', {}).get('context_flags', [])})
        result = {'record_type': 'name_match_comparison', 'id': f'comparison:{number:06d}',
                  'left': list(left), 'right': list(right),
                  'group_ids': ['group:' + '/'.join(key) for key in (left, right)],
                  'status': status, 'context_flags': flags, 'channels': channels, 'ownership_decision': None}
        results.append(result)
        if number % 25 == 0 or number == len(pairs):
            print(f'Checked {number}/{len(pairs)} group pairs', flush=True)
    for name, expected in hashes.items():
        if digest(Path(name)) != expected:
            raise ValueError('Input report changed during the comparison: ' + name)
    counts = dict(Counter(r['status'] for r in results))
    manifest = {'record_type': 'comparison_manifest', 'id': 'manifest:name_match_comparison',
                'schema': 'mtl0.name_match_comparison/2', 'created_utc': datetime.now(timezone.utc).isoformat(),
                'input_sha256': hashes, 'script_sha256': digest(Path(__file__)), 'python': sys.version.split()[0],
                'sample_comparison_sha256': digest(Path(__file__).with_name('sample_comparison.py')),
                'openpyxl': openpyxl.__version__, 'numpy': numpy.__version__, 'obspy': obspy.__version__,
                'decode_limits': {'max_miniseed_bytes_per_file': max_mseed_bytes, 'max_decoded_samples_per_file': max_decoded_samples},
                'unique_pairs': len(pairs), 'counts': counts,
                'pairs_with_context_flags': sum(bool(r['context_flags']) for r in results),
                'scope': 'Cross-folder name candidates from Step 01 only. Compare full bytes, then exact decoded numeric sequences when bytes differ. Header evidence kept separately; no ownership assignment or source changes.',
                'numeric_comparison': 'Exact finite numeric values and counts; no tolerance, trimming, gap filling, resampling or calibration. ASCII decimal values and exact binary floating-point values are not rounded.',
                'complete': not counts.get('file_problem', 0), 'original_inventory_records': len(inherited)}
    evidence = out / 'duplicate_evidence.jsonl'
    with evidence.open('x', encoding='utf-8', newline='\n') as stream:
        for row in [manifest, *inherited, *results]:
            stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n')
    excel_report(out / 'duplicate_review.xlsx', sources, results)
    print(f'RESULTS: {counts}. Reports: {out}', flush=True)
    return 0 if manifest['complete'] else 2


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', type=Path, required=True, help='Step 01 report directory or inventory_evidence.jsonl')
    parser.add_argument('--out', type=Path, required=True, help='New output directory; never overwritten')
    parser.add_argument('--max-miniseed-mib', type=int, default=MAX_MSEED_BYTES // 1024**2,
                        help='Maximum MiniSEED input size per file for decoding (default: 64 MiB)')
    parser.add_argument('--max-decoded-samples', type=int, default=MAX_DECODED_SAMPLES,
                        help='Maximum MiniSEED decoded samples per file (default: 16000000)')
    args = parser.parse_args()
    raise SystemExit(run(args.inventory, args.out, args.max_miniseed_mib * 1024**2, args.max_decoded_samples))
