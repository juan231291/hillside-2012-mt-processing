"""Populate standard station/run provenance from existing Level 1 records.

No waveform, response, acquisition comment or timing changes. This module is
also the post-write hook for future Level 1 writers: apply_file(path, creator)
must run before computing the final file checksum.
"""
from datetime import datetime, timezone
import hashlib
import json

import h5py
import numpy as np

REVISION = 'native_provenance_20260925_v3'
SOFTWARE = 'mth5'
# Author list adopted from the suggested citation in the MTH5 Credits page:
# https://mth5.readthedocs.io/en/latest/authors.html
SOFTWARE_AUTHORS = ('Peacock, J. R.; Kappler, K.; Ronan, T.; Heagy, L.; '
                    'Kelbert, A.; Frassetto, A.')
FIELDS = ('provenance.creation_time', 'provenance.creator.author',
          'provenance.software.name', 'provenance.software.version',
          'provenance.software.author', 'provenance.log')


def require(ok, message):
    if not ok:
        raise ValueError(message)


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def read_json(handle, name):
    ds = handle['MT_Level1_Provenance/' + name]
    raw = ds[()].tobytes()
    require(hashlib.sha256(raw).hexdigest() == ds.attrs['sha256'], 'Embedded JSON checksum differs: ' + name)
    return json.loads(raw)


def author_text(value):
    require(isinstance(value, str) and value.strip() and '\n' not in value, 'A creator name is required')
    return value.strip()



def plan_file(path, creator):
    creator = author_text(creator)
    with h5py.File(path, 'r') as handle:
        require(int(handle.attrs['data_level']) == 1, 'Expected Level 1')
        cfg = read_json(handle, 'settings_json')
        proc = read_json(handle, 'processing_json')
        src = read_json(handle, 'source_metadata_json')
        selection = read_json(handle, 'run_selection_json')
        require(cfg['release'] == 'v5_20260921_200mv', 'This repair targets the current v5 release only')
        require(cfg['output_sample_rate_hz'] == 1000 and cfg['magnetic_sensitivity_mv_per_nt'] == 200, 'Unexpected processing settings')
        created = proc['created_utc']
        parsed = datetime.fromisoformat(created.replace('Z', '+00:00'))
        require(parsed.tzinfo is not None and parsed.year >= 2026, 'A verified processing creation date is required')
        sid = src['station_id']
        surveys = handle['Experiment/Surveys']
        station_paths = [g['Stations'][sid].name for g in surveys.values() if 'Stations' in g and sid in g['Stations']]
        require(len(station_paths) == 1, 'Station identity is ambiguous')
        base = station_paths[0]
        expected_paths = {base + '/' + r['run_id'] for r in proc['runs']}
        actual_paths = {g.name for g in handle[base].values() if str(g.attrs.get('mth5_type', '')).lower() == 'run'}
        require(expected_paths == actual_paths, 'Native runs differ from processing record')
        require(proc['run_selection'] == selection, 'Selection records differ')
        mth5_version = proc['software'].get('mth5')
        require(isinstance(mth5_version, str) and bool(mth5_version.strip()),
                'The original MTH5 package version must be recorded in the file')
        common = {'provenance.creation_time': created,
                  'provenance.creator.author': creator,
                  'provenance.software.name': SOFTWARE,
                  'provenance.software.version': mth5_version.strip()}
        changes = {}
        for group_path in [base] + [base+'/'+r['run_id'] for r in proc['runs']]:
            attrs = handle[group_path].attrs
            values = dict(common)
            for key, expected in common.items():
                previous = str(attrs.get(key, '')).strip()
                placeholder = key == 'provenance.creation_time' and previous.startswith('1980-01-01')
                require(not previous or placeholder or previous == expected,
                        'Existing non-placeholder provenance differs: ' + group_path + '/' + key)
            existing_author = str(attrs.get('provenance.software.author', '')).strip()
            require(existing_author in ('', '-', SOFTWARE_AUTHORS),
                    'Existing software author differs from the adopted credit: ' + group_path)
            values['provenance.software.author'] = SOFTWARE_AUTHORS
            # The publication decision is to leave the native log empty.
            # Embedded original processing records remain intact.
            values['provenance.log'] = ''
            changes[group_path] = values
        old = {p: {k: {'present': k in handle[p].attrs, 'value': str(handle[p].attrs[k]) if k in handle[p].attrs else None}
                   for k in FIELDS} for p in changes}
        return {'station': sid, 'revision': REVISION, 'creator': creator,
                'changes': changes, 'previous': old,
                'sample_hashes': {base+'/'+r['run_id']+'/'+ch: item['sample_sha256'] for r in proc['runs'] for ch,item in r['channels'].items()}}


def apply_plan(path, plan):
    with h5py.File(path, 'r+') as handle:
        for group_path, fields in plan['changes'].items():
            for key, value in fields.items():
                handle[group_path].attrs[key] = value
        handle.flush()


def verify_native(path, plan, samples=True):
    with h5py.File(path, 'r') as handle:
        for group_path, fields in plan['changes'].items():
            for key,value in fields.items():
                require(str(handle[group_path].attrs[key]) == value, 'Native field readback differs: ' + key)
        if samples:
            for name, expected in plan['sample_hashes'].items():
                ds = handle[name]
                digest = hashlib.sha256()
                for offset in range(0, len(ds), 262144):
                    digest.update(ds[offset:offset+262144].astype('<f8', copy=False).tobytes())
                require(digest.hexdigest() == expected, 'Waveform digest differs: ' + name)
    # Read the fields through MTH5's ordinary metadata API, not the custom JSON.
    from mth5.mth5 import MTH5
    with MTH5() as m:
        m.open_mth5(path, mode='r')
        for group_path, fields in plan['changes'].items():
            parts = group_path.strip('/').split('/')
            station = m.get_station(parts[4], survey=parts[2])
            metadata = station.metadata if len(parts) == 5 else station.get_run(parts[5]).metadata
            native = metadata.provenance
            require(native.creator.author == fields['provenance.creator.author'], 'Creator missing from standard MTH5 API')
            require(native.software.name == fields['provenance.software.name'], 'Software missing from standard MTH5 API')
            require(native.software.version == fields['provenance.software.version'], 'Software version missing from standard MTH5 API')
            require(native.software.author == fields['provenance.software.author'], 'Software authors missing from standard MTH5 API')
            require(native.log in ('', None), 'Native provenance log must remain empty')
            require(datetime.fromisoformat(native.creation_time.isoformat()) == datetime.fromisoformat(fields['provenance.creation_time']), 'Creation date differs in standard MTH5 API')


def apply_file(path, creator):
    plan = plan_file(path, creator)
    apply_plan(path, plan)
    verify_native(path, plan, samples=False)
    return plan
