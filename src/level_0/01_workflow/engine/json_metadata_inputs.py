"""Validate the reviewed Step 08 workbook against its complete report chain."""
from pathlib import Path
from openpyxl import load_workbook
from metadata_inputs import validate_inputs
from metadata_workbook import build, serializable


def load_prepared(metadata, original, export, selection, qc, decisions, policy,
                  step06_code, step07_code, step08_code):
    context = validate_inputs(original, export, selection, qc, decisions, policy,
                              step06_code, step07_code)
    import metadata_review
    metadata_review.attach(context, original)
    from ascii_export import require, sha
    from metadata_inputs import load_module
    six = load_module(step06_code, 'step06_for_json')
    audit_path = metadata / 'metadata_changes.jsonl'
    records = six.read_jsonl(audit_path)
    m = records[0]
    require(m['id'] == 'manifest:level0_metadata_workbook' and
            m['schema'] == 'mtl0.level0_metadata_workbook/1' and m['complete'] is True,
            'Complete Step 08 metadata required')
    require(m['workbook_filename'] == 'Level0_metadata.xlsx', 'Unexpected workbook name')
    workbook = metadata / m['workbook_filename']
    require(sha(workbook) == m['workbook_sha256'],
            'Workbook differs from its Step 08 change record; prepare a new recorded metadata revision first')
    require(six.portable_hashes(m['input_sha256']) == six.portable_hashes(context['input_sha256']),
            'Step 08 input chain differs')
    code_files = {**context['code_files'], '08_prepare_metadata.py': step08_code,
                  'metadata_inputs.py': step08_code.with_name('metadata_inputs.py'),
                  'metadata_workbook.py': step08_code.with_name('metadata_workbook.py'),
                  'metadata_review.py': Path(metadata_review.__file__)}
    require({n: sha(p) for n, p in code_files.items()} == m['code_sha256'], 'Step 08 code differs')
    require(m['metadata_review'] == context['metadata_review'], 'Step 08 metadata review differs')
    require(m['original_workbook_sha256'] == context['original_sha256'] and
            m['export_evidence_sha256'] == context['export_evidence_sha256'], 'Metadata source differs')
    require(all(m[k] is False for k in ('original_workbook_modified', 'samples_modified',
            'calibration_applied', 'rotation_applied', 'structured_mth5_json_created', 'mth5_created')),
            'Unexpected transformation in the Step 08 input')
    built = build(original, context)
    try:
        require(records[1:] == [*built['station_records'], *built['run_records'], *built['changes']],
                'Step 08 audit differs from the independently rebuilt metadata')
        require(m['metadata_headers'] == built['headers'] and
                m['original_headers'] == built['source_headers'], 'Metadata columns differ')
        require(m['source_rows_used'] == len(built['station_records']) and
                m['metadata_row_count'] == len(built['run_records']) and
                m['changed_cells'] == len(built['changes']) and
                m['selection_counts'] == context['selection_manifest']['counts'], 'Metadata totals differ')
        wb = load_workbook(workbook, read_only=True, data_only=False)
        try:
            require(wb.sheetnames == m['sheet_names'], 'Metadata sheets differ')
            sheet = wb[m['metadata_sheet']]
            require(not any(c.data_type in ('f', 'e') for row in sheet for c in row),
                    'Metadata formulas or Excel errors need review')
            rows = [[serializable(c.value) for c in row] for row in sheet]
            require(rows == [built['headers']] + [r['values'] for r in built['run_records']],
                    'Metadata cells differ from the verified Step 08 output')
        finally:
            wb.close()
        hashes = {**context['input_sha256'], str(workbook.resolve()): sha(workbook),
                  str(audit_path.resolve()): sha(audit_path)}
        return {'headers': rows[0], 'rows': rows[1:], 'run_records': built['run_records'],
                'station_records': built['station_records'], 'context': context,
                'source': {'workbook': workbook.name, 'workbook_sha256': sha(workbook),
                           'sheet': m['metadata_sheet'], 'audit': audit_path.name,
                           'audit_sha256': sha(audit_path),
                           'original_workbook_sha256': context['original_sha256']},
                'input_sha256': hashes, 'code_files': code_files}
    finally:
        built['source_workbook'].close()
