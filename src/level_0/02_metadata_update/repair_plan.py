"""Validate the known Step 08 audit-ID failure before an isolated restart."""
import copy
import json
from pathlib import Path
import workflow as wf

ORIGINAL_REVISION = 'v4_20260918'


def inspect_repair(config_path,revision,compare_rows):
    c,cp = wf.config(config_path)
    batch = Path(c['batch_dir'])
    wf.require(c['survey_id'] == 'Hillside' and c['expected_station_count'] == 73,
               'Repair is restricted to the reviewed Hillside batch')
    wf.require(not list((batch/'state').glob('*.lock')), 'A workflow stage has a lock; inspect its job first')
    original_plan_path = batch/'reports/hillside_metadata_update_20260918/plan.json'
    original_audit_path = batch/'state/code_updates/hillside_metadata_20260918.json'
    original_plan = wf.strict_load(original_plan_path)
    original_audit = wf.strict_load(original_audit_path)
    wf.require(original_plan['schema'] == 'hillside.metadata_correction_plan/1' and
               original_plan['revision'] == ORIGINAL_REVISION, 'Unexpected original correction plan')
    wf.require(original_audit['schema'] == 'mtl0.metadata_update_audit/1' and
               original_audit['update_id'] == 'hillside_metadata_20260918', 'Unexpected original activation audit')
    current = wf.strict_load(batch/'state/batch.json')
    wf.require(current == original_audit['active_batch_identity'],
               'Batch does not match the activated failed attempt')
    spec = wf.strict_load(wf.PACKAGE/'configs/metadata_code_update.json')
    wf.require(len(spec['sources']) == 1 and current['code_sha256'] == spec['sources'][0]['code_sha256'],
               'Failed source code differs from the pinned correction package')
    wf.require(wf.code_hashes() == spec['target_code_sha256'], 'Repair package code changed')
    package = Path(original_audit['new_package']).resolve(strict=True)
    old_cp = Path(original_audit['configuration']).resolve(strict=True)
    wf.require(old_cp == package/'configs/hillside.json', 'Unexpected failed configuration location')
    actual = {p.relative_to(package).as_posix():wf.sha(p)
              for p in sorted([package/'workflow.py',*(package/'engine').glob('*.py')])}
    wf.require(actual == current['code_sha256'], 'Do not edit the failed package in place')
    old,old_cp = wf.config(old_cp)
    c,cp = wf.config(config_path)
    ignored = {'metadata_rules','selection_rules','ownership_rules'}
    wf.require({k:v for k,v in old.items() if k not in ignored} ==
               {k:v for k,v in c.items() if k not in ignored}, 'Acquisition/output configuration changed')
    for key in ignored:
        wf.require(wf.sha(Path(old[key])) == wf.sha(Path(c[key])), 'Repair must not change scientific rules: '+key)
    frozen = {**wf.preflight(c),'configuration_sha256':wf.sha(old_cp),'code_sha256':actual}
    wf.require(current == frozen,'Frozen inputs/configuration differ from the failed attempt')
    wf.require(original_audit['previous_stage_receipts_sha256'] == original_plan['stage_receipts_sha256'],
               'The original review plan and activation audit disagree')
    receipts = {}
    for step in range(1,11):
        wf.require(wf.sha(wf.receipt(batch,step)) == original_plan['stage_receipts_sha256'][str(step)],
                   'A stage completed or changed since the reported failure; inspect before repair')
        receipts[step] = wf.validate_receipt(c,batch,step)
    for key in ('old_workbook','old_mth5_manifest'):
        wf.require(wf.sha(Path(original_plan[key])) == original_plan[key+'_sha256'], 'Previous release changed')
    for step in (8,9,10):
        parent = batch/('level0' if step == 10 else 'metadata')
        wf.require(not (parent/f'{step:02d}_{wf.STEPS[step]}_{revision}').exists(),
                   'A repair output already exists; earlier attempts must not be overwritten')
        wf.require(not (batch/'state'/f'{step:02d}_{revision}.json').exists(),'A repair receipt already exists')
    # The failed workbook/audit remain available. Verify this is the reported
    # duplicate-ID failure, and pin the files so a retry cannot alter them.
    failed = batch/'metadata'/f'08_excel_metadata_{ORIGINAL_REVISION}'
    failed_audit = failed/'metadata_changes.jsonl'
    records = wf.records(failed_audit)
    seen,duplicates = set(),[]
    for record in records:
        if record['id'] in seen:
            duplicates.append(record)
        seen.add(record['id'])
    wf.require(duplicates and all(r['record_type'] == 'metadata_cell_change' and
               r['field'] == 'station.comments' for r in duplicates),
               'Failed output is not the reported repeated station-comment audit-ID case')
    from metadata_inputs import validate_inputs
    from metadata_review import attach
    from metadata_workbook import build
    from correction import table
    dirs = {s:Path(r['output']) for s,r in receipts.items()}
    original = Path(c['metadata_workbook'])
    context = validate_inputs(original,dirs[7],dirs[6],dirs[5],dirs[4],
        Path(receipts[6]['policy_path']),wf.ENGINE/'06_select_runs.py',wf.ENGINE/'07_concatenate.py')
    attach(context,original)
    built = build(original,context)
    try:
        changed = compare_rows(*table(Path(original_plan['old_workbook'])),built['headers'],
            [r['values'] for r in built['rows']],original_plan['allowed_changes'])
        wf.require(changed == original_plan['changes'], 'The proposed metadata differs from the approved 498-cell update')
        wf.require(table(failed/'Level0_metadata.xlsx') ==
            (built['headers'],[r['values'] for r in built['rows']]), 'Repair would change the failed workbook values')
        identifiers = [r['id'] for group in ('station_records','run_records','changes') for r in built[group]]
        wf.require(len(identifiers) == len(set(identifiers)), 'Audit repair still has duplicate IDs')
    finally:
        built['source_workbook'].close()
    result = copy.deepcopy(original_plan)
    result.update(revision=revision,previous_package=str(package),previous_config=str(old_cp),
        previous_batch_identity=current,repair_of=ORIGINAL_REVISION,
        repair_reason='Coalesce repeated cell updates into one original-to-final audit entry; no metadata values changed.',
        preserved_repair_inputs={str(p):wf.sha(p) for p in
            (original_plan_path,original_audit_path,failed_audit,failed/'Level0_metadata.xlsx')})
    print(json.dumps({k:result[k] for k in ('revision','repair_of','repair_reason','station_count',
        'run_count','changed_cells','changes_by_field')},indent=2))
    print('REPAIR CHECK PASSED: same reviewed metadata, unique audit IDs, failed attempt retained.')
    return result
