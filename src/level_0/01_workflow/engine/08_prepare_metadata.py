"""Prepare a new Level 0 Excel metadata workbook from verified run exports."""
from datetime import datetime,timezone
import argparse
import json
from pathlib import Path
import sys

from metadata_inputs import validate_inputs
from metadata_workbook import build,write,serializable


def run(original,export,selection,qc,decisions,policy,step06_code,step07_code,out):
    if out.exists():raise FileExistsError('Output already exists; choose a new metadata directory: '+str(out))
    context=validate_inputs(original,export,selection,qc,decisions,policy,step06_code,step07_code)
    import metadata_review
    metadata_review.attach(context, original)
    from ascii_export import require,sha
    require(out.resolve()!=original.resolve().parent,'Use a derived-output directory separate from original metadata')
    for root in context['export_manifest']['source_roots_used'].values():
        if root.startswith('/') and sys.platform=='win32':continue
        p=Path(root).resolve()
        require(out.resolve()!=p and p not in out.resolve().parents,'Metadata output must be outside raw sources')
    built=build(original,context)
    try:
        for path,expected in context['input_sha256'].items():
            require(sha(Path(path))==expected,'An input changed during metadata preparation')
        out.mkdir(parents=True,exist_ok=False)
        workbook=out/'Level0_metadata.xlsx'
        info=write(workbook,built,original.name)
        for path,expected in context['input_sha256'].items():
            require(sha(Path(path))==expected,'An input changed while the workbook was written')
        import metadata_inputs,metadata_workbook,openpyxl
        code_files={**context['code_files'],
            '08_prepare_metadata.py':Path(__file__),
            'metadata_inputs.py':Path(metadata_inputs.__file__),
            'metadata_workbook.py':Path(metadata_workbook.__file__),
            'metadata_review.py':Path(metadata_review.__file__)}
        manifest={'record_type':'metadata_workbook_manifest','id':'manifest:level0_metadata_workbook',
            'schema':'mtl0.level0_metadata_workbook/1','created_utc':datetime.now(timezone.utc).isoformat(),
            'complete':True,'workbook_filename':workbook.name,'workbook_sha256':sha(workbook),
            'input_sha256':context['input_sha256'],'code_sha256':{name:sha(path) for name,path in code_files.items()},
            'original_workbook_filename':original.name,'original_workbook_sha256':context['original_sha256'],
            'original_headers':built['source_headers'],'metadata_headers':built['headers'],
            'source_rows_used':len(built['station_records']),'changed_cells':len(built['changes']),
            'selection_counts':context['selection_manifest']['counts'],
            'export_evidence_sha256':context['export_evidence_sha256'],**info,
            'original_workbook_modified':False,'raw_samples_opened':False,'ascii_samples_opened':False,
            'samples_modified':False,'calibration_applied':False,'rotation_applied':False,
            'structured_mth5_json_created':False,'mth5_created':False,
            'time_policy':'ISO 8601 UTC strings, with source UTC assumption preserved. End is the last sample. Station time spans all retained runs; each run/channel-family time spans its exported samples.',
            'source_policy':'Source metadata is preserved except explicit source-pinned reviewed edits, export-derived run fields and fields for absent channels. No missing sensor values are invented. Reviewed edits change metadata only; no calibration or rotation is applied.',
            'metadata_review':context['metadata_review'],
            'change_log_scope':'Before/after values compare every new run row with its original station row. Original source row numbers and values are retained. This audit JSONL is not the final structured MTH5 metadata JSON.',
            'python':sys.version.split()[0],'openpyxl':openpyxl.__version__}
        records=[manifest,*built['station_records'],*built['run_records'],*built['changes']]
        text=[json.dumps(r,ensure_ascii=False,allow_nan=False,default=serializable,separators=(',',':')) for r in records]
        path=out/'metadata_changes.jsonl'
        with path.open('x',encoding='utf-8',newline='\n') as f:
            f.write('\n'.join(text)+'\n')
        with path.open(encoding='utf-8') as f:
            require([json.loads(line) for line in f]==[json.loads(line) for line in text],'Metadata audit readback differs')
        require(len({r['id'] for r in records})==len(records),'Repeated metadata evidence IDs')
        print(f"SUCCESS: Level 0 metadata for {len(built['station_records'])} stations and {len(built['rows'])} runs.")
        print('Workbook:',workbook)
        print('Changes:',path)
        print('Original Excel and sample files were not modified. Structured JSON and MTH5 creation follow later.')
        return manifest,built['run_records'],built['changes']
    finally:
        built['source_workbook'].close()


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--original',type=Path,required=True)
    p.add_argument('--export',type=Path,required=True)
    p.add_argument('--selection',type=Path,required=True)
    p.add_argument('--qc',type=Path,required=True)
    p.add_argument('--decisions',type=Path,required=True)
    p.add_argument('--policy',type=Path,default=Path(__file__).with_name('06_selection_policy.json'))
    p.add_argument('--step06-code',type=Path,default=Path(__file__).with_name('06_select_runs.py'))
    p.add_argument('--step07-code',type=Path,default=Path(__file__).with_name('07_concatenate.py'))
    p.add_argument('--out',type=Path,required=True)
    a=p.parse_args()
    run(a.original,a.export,a.selection,a.qc,a.decisions,a.policy,a.step06_code,a.step07_code,a.out)
