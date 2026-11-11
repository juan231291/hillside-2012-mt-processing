"""Convert the verified Step 08 workbook into one structured JSON per station."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import sys

from json_metadata_inputs import load_prepared
from station_metadata_json import assemble, require, sha, strict_load, write_station


def run(metadata, original, export, selection, qc, decisions, policy,
        step06_code, step07_code, step08_code, out):
    require(not out.exists(), 'Output exists; choose a new JSON output directory: ' + str(out))
    prepared = load_prepared(metadata, original, export, selection, qc, decisions, policy,
                             step06_code, step07_code, step08_code)
    for raw_root in prepared['context']['export_manifest']['source_roots_used'].values():
        if sys.platform == 'win32' and raw_root.startswith('/'):
            continue
        root = Path(raw_root).resolve()
        require(root != out.resolve() and root not in out.resolve().parents, 'Output must be outside raw source trees')
    require(out.resolve() != metadata.resolve() and metadata.resolve() not in out.resolve().parents,
            'Keep derived JSON in a separate output directory from the Step 08 workbook')
    records = assemble(prepared)
    def unchanged():
        for name, expected in prepared['input_sha256'].items():
            require(sha(name) == expected, 'Input changed during conversion: ' + name)
    unchanged()
    out.mkdir(parents=True, exist_ok=False)
    files = []
    for sid, record in sorted(records.items()):
        path = out / (sid + '.json')
        write_station(path, record)
        files.append({'station': sid, 'file': path.name, 'sha256': sha(path),
                      'run_count': len(record['runs']),
                      'channel_instances': sum(len(r['channels']) for r in record['runs'])})
        print(f'VERIFIED JSON: {sid}, {len(record["runs"])} runs', flush=True)
    unchanged()
    code_files = {**prepared['code_files'], **{n: Path(__file__).with_name(n) for n in
        ('09_excel_to_json.py', 'json_metadata_inputs.py', 'station_metadata_json.py')}}
    import openpyxl
    manifest = {'schema': 'mtl0.level0.metadata_json_batch/2', 'complete': True,
        'created_utc': datetime.now(timezone.utc).isoformat(), 'data_level': 0,
        'stations': files, 'station_count': len(files), 'run_count': sum(f['run_count'] for f in files),
        'channel_instances': sum(f['channel_instances'] for f in files),
        'input_sha256': prepared['input_sha256'], 'code_sha256': {n: sha(p) for n, p in code_files.items()},
        'source': prepared['source'], 'all_json_readback_verified': True,
        'workbook_modified': False, 'samples_read': False, 'samples_modified': False, 'mth5_created': False,
        'format': 'MT survey interchange JSON for the next MTH5 writer; not native Experiment.to_json output',
        'normalization': ['Dot-separated Excel headings become nested objects.',
            'Identical duplicate station.id columns become one field; both original columns remain in source.headers/source.rows.',
            'Empty cells and lone dash placeholders become JSON null. Zero remains zero.',
            'Instrument/station/run IDs become strings; original cell types/values remain in source.rows.',
            'Channel and run-list strings become JSON arrays.',
            'Survey date fields become YYYY-MM-DD; run and station timestamps remain ISO 8601 UTC strings.',
            'Per-channel suffixes are removed inside each channel object; specific fields override shared fields.',
            'Absent channels are omitted. Unspecified tilt is null; no orientation is inferred.',
            'Source prose is preserved verbatim, including existing caveats and typographical errors.'],
        'mapping_notes': ['survey.acquired_by.author and station.acquired_by.name remain separate.',
            'The MTH5 writer must explicitly map station.acquired_by.name to the native author field.',
            'The writer must handle unknown numeric metadata explicitly rather than accept library zero defaults.',
            'The writer must preserve nonnative source fields and non-DOI citation URLs in provenance.',
            'ADC counts label is retained. No calibration response is added or applied.'],
        'versions': {'python': platform.python_version(), 'openpyxl': openpyxl.__version__}}
    path = out / 'manifest.json'
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2) + '\n')
    require(strict_load(path) == manifest, 'Manifest readback differs')
    print(f'SUCCESS: {len(files)} station JSON files, {manifest["run_count"]} runs. Output: {out}')
    return manifest


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('metadata', 'original', 'export', 'selection', 'qc', 'decisions', 'out'):
        p.add_argument('--' + name, type=Path, required=True)
    for name, filename in (('policy', '06_selection_policy.json'), ('step06-code', '06_select_runs.py'),
                           ('step07-code', '07_concatenate.py'), ('step08-code', '08_prepare_metadata.py')):
        p.add_argument('--' + name, type=Path, default=Path(__file__).with_name(filename))
    run(**vars(p.parse_args()))
