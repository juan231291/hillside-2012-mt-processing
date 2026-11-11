"""Portable Gadi report writer; two outputs, stable evidence IDs.

The XLSX is a generated human summary, not a processing input. All source
observations remain in JSONL. Existing inventories can be reformatted read-only.
"""
from pathlib import Path
from collections import Counter
import csv
import hashlib
import importlib.util
import json


def evidence_records(summary, files, groups, headers):
    yield {'record_type': 'manifest', 'id': 'manifest:inventory',
           'schema': 'mtl0.inventory.evidence/1', 'original_summary': summary,
           'report_writer_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    for source in summary['sources']:
        yield {'record_type': 'source', 'id': 'source:' + source['role'] + '/' + source['label'], 'data': source}
    # Preserve CSV-compatible strings for field-level compatibility with existing reports.
    for kind, rows, key, prefix in [('file', files, 'relative_path', 'file:'),
                                    ('recording_group', groups, 'group_key', 'group:')]:
        for original in rows:
            row = {k: str(v) if v is not None else '' for k, v in original.items()}
            sid = row['source_role'] + '/' + row['source_label']
            yield {'record_type': kind, 'id': prefix + sid + '/' + row[key],
                   'source_id': 'source:' + sid, 'data': row}
    for key, data in sorted(headers.items()):
        yield {'record_type': 'miniseed_headers', 'id': 'headers:' + key,
               'file_id': 'file:' + key, 'data': data}


def overview_rows(summary, files, groups):
    rows = []
    for source in summary['sources']:
        key = source['role'], source['label']
        ff = [r for r in files if (r['source_role'], r['source_label']) == key]
        gg = [r for r in groups if (r['source_role'], r['source_label']) == key]
        em = [r for r in ff if r['channel_from_extension']]
        mismatch = [r for r in gg if r['prefix_relation'] not in ('matches_folder_label', 'matches_ignoring_case')]
        matches = [r for r in gg if r['same_stem_in_other_sources']]
        missing = sum(bool(r['missing_channel_names'] or r['repeated_channel_names']) for r in gg)
        empty = sum(int(r['bytes'] or 0) == 0 for r in em)
        # Ordinary format names, multiple valid traces and harmless auxiliary binary
        # files are not inventory problems. Timing details remain in the evidence.
        read_problems = [r for r in ff if 'io_error:' in r['review_flags'] or
                         'source_changed_during_scan' in r['review_flags'] or
                         r['entry_type'] != 'regular_file']
        format_problems = [r for r in em if int(r['bytes'] or 0) > 0 and
                           (r['format'] not in ('integer_text_preview', 'numeric_text_preview', 'miniseed_headers_read')
                            or 'miniseed_parser_warning' in r['review_flags']
                            or 'header_channel_differs_from_extension' in r['review_flags'])]
        actions = []
        if mismatch:
            prefixes = Counter(r['filename_prefix'] or '(unrecognised)' for r in mismatch)
            shown = ', '.join(f'{k}: {v}' for k, v in sorted(prefixes.items())[:6])
            if len(prefixes) > 6:
                shown += f'; +{len(prefixes)-6} other prefixes'
            actions.append('Check station labels (' + shown + ').')
        if matches:
            actions.append('Compare matching names before deciding which copies to keep.')
        if missing or empty:
            actions.append('Check missing/repeated channels and empty EM files.')
        if read_problems or format_problems:
            actions.append('Inspect unreadable, unsupported or inconsistent entries.')
        rows.append([source['label'], source['role'], len(ff), len(gg), len(mismatch),
                     len(matches), missing, empty, len(read_problems), len(format_problems),
                     ' '.join(actions) or 'No inventory issues detected; timing and sample QC still follow.'])
    return rows


def write_overview(path, rows):
    # This dependency is available in the user's requested Gadi Python runtime.
    from openpyxl import Workbook, load_workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = 'Inventory review'
    ws.sheet_view.showGridLines = False
    ws.append(['MT survey | source inventory'])
    ws.merge_cells('A1:K1')
    ws['A1'].font = Font(name='Arial', size=18, bold=True, color='16324F')
    ws.row_dimensions[1].height = 30
    ws.append(['One row per source folder. Counts describe files and recording groups, not final concatenated runs.'])
    ws.merge_cells('A2:K2')
    ws.append([])
    ws.append(['Folder', 'Role', 'File entries', 'Recording groups', 'Label mismatches',
               'Groups with name matches', 'Missing/repeated channel groups', 'Empty EM files',
               'Read/path problems', 'EM format/header problems', 'Review action'])
    for row in rows:
        ws.append(row)
    last = ws.max_row
    ws.freeze_panes = 'C5'
    ws.auto_filter.ref = f'A4:K{last}'
    for cell in ws[4]:
        cell.font = Font(name='Arial', color='FFFFFF', bold=True, size=10)
        cell.fill = PatternFill('solid', fgColor='16324F')
        cell.alignment = Alignment(wrap_text=True, vertical='center')
    ws.row_dimensions[4].height = 48
    for n in range(5, last + 1):
        ws.row_dimensions[n].height = 78
        for col in range(1, 12):
            cell = ws.cell(n, col)
            cell.font = Font(name='Arial', size=10, color='16324F')
            cell.alignment = Alignment(wrap_text=True, vertical='center')
            if 3 <= col <= 10:
                cell.number_format = '#,##0'
            if 5 <= col <= 10 and cell.value:
                cell.fill = PatternFill('solid', fgColor='FFF0C2')
    for col, width in zip('ABCDEFGHIJK', (13, 12, 12, 15, 15, 18, 20, 12, 15, 18, 65)):
        ws.column_dimensions[col].width = width
    notes = [
        'Matching names are candidates only. Step 02 establishes byte/sample equality; counts appear under both folders.',
        'Different prefixes may be logger IDs or historical station names. They are evidence to investigate, not automatic exclusions.',
        'Readable ASCII and MiniSEED are not flagged just because their formats differ. Auxiliary binary files stay in the evidence.',
        'Text is previewed; MiniSEED headers are inspected. Full sample validity, timing, rate and overlap QC are later steps.',
        'Detailed source paths, group IDs, header records and flags: inventory_evidence.jsonl. No station decisions were applied.',
    ]
    ws.append([])
    for note in notes:
        ws.append([note])
        ws.merge_cells(start_row=ws.max_row, start_column=1, end_row=ws.max_row, end_column=11)
        ws.cell(ws.max_row, 1).alignment = Alignment(wrap_text=True, vertical='center')
        ws.row_dimensions[ws.max_row].height = 30
    wb.save(path)
    check = load_workbook(path, read_only=True, data_only=True)
    assert list(check.active.iter_rows(min_row=5, max_row=last, values_only=True)) == [tuple(r) for r in rows]
    check.close()


def write_reports(out, summary, files, groups, headers, inherited=None):
    records = list(inherited) if inherited is not None else list(evidence_records(summary, files, groups, headers))
    with (out / 'inventory_evidence.jsonl').open('x', encoding='utf-8', newline='\n') as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n')
    write_overview(out / 'inventory_review.xlsx', overview_rows(summary, files, groups))


def migrate_inventory(source, out):
    spec = importlib.util.spec_from_file_location('inventory_loader', Path(__file__).with_name('02_compare_duplicates.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    roots, files, groups, records, hashes = module.inventory(source)
    for root in roots:
        raw = Path(root['resolved_root']).resolve()
        if out.resolve() == raw or raw in out.resolve().parents:
            raise ValueError('Output must be outside all raw sources')
    summary = next(r['original_summary'] for r in records if r['id'] == 'manifest:inventory')
    headers = {r['file_id'].removeprefix('file:'): r['data'] for r in records if r['record_type'] == 'miniseed_headers'}
    manifest = next(r for r in records if r['id'] == 'manifest:inventory')
    manifest['report_reformat'] = {'input_sha256': hashes, 'raw_sources_rescanned': False,
                                 'writer_sha256': module.digest(Path(__file__))}
    for path, expected in hashes.items():
        if module.digest(Path(path)) != expected:
            raise ValueError('Original inventory changed during reformatting')
    out.mkdir(parents=True, exist_ok=False)
    write_reports(out, summary, files, groups, headers, records)
    print('SUCCESS: two inventory reports created from the existing evidence. Raw files were not rescanned.', flush=True)
    return summary
