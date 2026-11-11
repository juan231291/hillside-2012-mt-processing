"""Compact human review workbook; complete per-file provenance stays in JSONL."""
from collections import Counter
from fractions import Fraction
import math


def duration(seconds):
    value = Fraction(seconds)
    if value.denominator != 1:
        return str(value) + ' seconds'
    hours, remainder = divmod(int(value), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d}'


def add_sheet(wb, name, title, note, headers, widths, rows):
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    ws = wb.create_sheet(name)
    last = get_column_letter(len(headers))
    ws.append([title]); ws.merge_cells(f'A1:{last}1')
    ws.append([note]); ws.merge_cells(f'A2:{last}2')
    ws.append([]); ws.append(headers)
    for row in rows:
        ws.append(row)
    ws.freeze_panes = 'C5'
    ws.sheet_view.showGridLines = False
    ws.auto_filter.ref = f'A4:{last}{max(4, ws.max_row)}'
    ws.row_dimensions[1].height = 30
    ws.row_dimensions[2].height = 45
    ws.row_dimensions[4].height = 36
    for i, width in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = width
    for row in ws:
        for cell in row:
            cell.font = Font(name='Arial', size=10, color='16324F')
            cell.alignment = Alignment(wrap_text=True, vertical='center')
            # Treat arbitrary station names/comments as text, not Excel formulas.
            if isinstance(cell.value, str):
                cell.data_type = 's'
    ws['A1'].font = Font(name='Arial', size=16, bold=True, color='16324F')
    for cell in ws[4]:
        cell.font = Font(name='Arial', size=10, bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='16324F')
    for index in range(5, ws.max_row + 1):
        lines = max(math.ceil(len(str(ws.cell(index, i).value or '')) / max(8, width * .85))
                    for i, width in enumerate(widths, 1))
        ws.row_dimensions[index].height = max(42, lines * 15 + 14)
        if index % 2 == 0:
            for cell in ws[index]:
                cell.fill = PatternFill('solid', fgColor='F1F5F7')
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.orientation = 'landscape'
    ws.page_setup.paperSize = ws.PAPERSIZE_A3
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.print_title_rows = '1:4'
    ws.print_options.horizontalCentered = True
    ws.print_area = f'A1:{last}{ws.max_row}'
    return ws


def write_workbook(path, manifest, selections, runs, prior):
    from openpyxl import Workbook, load_workbook
    wb = Workbook(); wb.remove(wb.active)
    rows = []
    for r in runs:
        rows.append([r['station'], r['run_id'], r['start_utc_assumed'], r['end_exclusive_utc_assumed'],
                     duration(r['duration_seconds']), ', '.join(c.upper() for c in r['channels']),
                     r['source_group_count'], r['rate_hz'],
                     'Inferred from samples / recorder duration' if 'sample_count_over_logged_duration_estimate' in r['rate_bases']
                     else 'Reviewed workbook + filename boundaries' if 'reviewed_workbook_and_filename_boundary_consistency' in r['rate_bases']
                     else 'MiniSEED header'])
    add_sheet(wb, 'Run plan', 'MT survey | Level 0 run selection',
              'Plan only. UTC is assumed. All short runs at the selected native rate are retained. No minimum duration, resampling, timestamp correction or concatenation is applied.',
              ['Station', 'Run', 'Start UTC assumed', 'End exclusive UTC assumed', 'Duration h:m:s',
               'Channels', 'Source groups', 'Native rate Hz', 'Rate evidence'],
              [12, 15, 25, 25, 17, 25, 13, 15, 34], rows)
    rows = []
    for s in sorted(selections.values(), key=lambda s: (s['station'], s['recording_group'])):
        if s['action'] != 'include':
            rows.append([s['station'], s['recording_group'], s['action'].replace('_', ' '),
                         ', '.join(s['qc_snapshot']['rates_hz']), s['reason']])
        elif s['omitted_channels']:
            rows.append([s['station'], s['recording_group'], 'include partial channels',
                         s['rate_hz'], s['reason']])
    add_sheet(wb, 'Selection notes', 'Exclusions, holds and partial-channel selections',
              'These decisions affect the planned output only. All original files remain available. Earlier cross-station copy decisions are preserved separately in the evidence.',
              ['Station', 'Recording group', 'Action', 'Observed rate Hz', 'Reason'],
              [13, 39, 25, 20, 85], rows)
    counts = Counter((r['decision']['source_folder'], r['decision']['assigned_station'],
                      r['decision']['decision']) for r in prior)
    rows = []
    for (source, owner, action), count in sorted(counts.items()):
        comment = f'Duplicate recordings assigned to {owner} were excluded from the {source} output.' if action == 'exclude_duplicate' else ''
        note = 'Reference only; no output run is created.' if owner not in manifest['policy']['stations'] and action == 'retain_for_qc' else comment
        rows.append([source, owner, action, count, note])
    add_sheet(wb, 'Earlier decisions', 'Step 04 ownership and duplicate decisions',
              'Unchanged source decisions. Retain for QC alone does not mean inclusion in a Level 0 run. Step 06 evaluates all retained groups assigned to the configured survey stations.',
              ['Source folder', 'Assigned station', 'Earlier decision', 'Groups', 'Note'],
              [18, 18, 25, 13, 92], rows)
    rules = [
        ['Native rate', 'Native rate(s) selected by the per-station policy. ASCII rates remain estimates from sample count and logged duration; neighbour agreement is corroboration only.'],
        ['Startup recordings', 'Only the explicitly listed low-rate startup groups in 06_selection_policy.json are excluded. Other unexpected rates are held for review.'],
        ['Short runs', 'No duration cutoff for Level 0. Valid six-second runs remain separate. Level 1 selection is deferred.'],
        ['Clock conflicts', 'Exclude only explicitly reviewed groups listed in the policy. Other clock conflicts remain on hold. Never shift timestamps automatically or discard good overlapping recordings because of an excluded snippet.'],
        ['Continuity', 'Join only exactly adjacent intervals with the same station, channel set and rate. Preserve gaps as run boundaries.'],
        ['Channels', 'Partial channel sets require explicit group rules and verified empty omitted channels. Never trim good channels or invent missing samples.'],
        ['Overlapping selected data', 'If otherwise included recordings overlap, hold both for a separate copy/trim decision. Equal samples alone do not select ownership.'],
        ['Source verification', 'No raw files are read in Step 06. The future exporter must recheck source hashes and counts against the stored QC evidence.'],
        ['Policy edits', 'To change a decision, copy and edit the policy JSON, then rerun to a new report directory. The workbook is a review output, not an execution input.'],
        ['Metadata', 'Metadata transformation comes after run selection and export. This step preserves earlier comments; it does not edit the metadata workbook.']]
    add_sheet(wb, 'Rules', 'How this plan was selected',
              'Machine-readable rules, every recording decision, ordered source groups, sample offsets and prior evidence are included in selection_evidence.jsonl.',
              ['Rule', 'Meaning'], [27, 135], rules)
    expected = {ws.title: [tuple(None if v == '' else v for v in row) for row in ws.values] for ws in wb}
    wb.save(path)
    check = load_workbook(path, read_only=True, data_only=True)
    try:
        if {ws.title: list(ws.values) for ws in check} != expected:
            raise ValueError('Workbook readback differs')
    finally:
        check.close()
