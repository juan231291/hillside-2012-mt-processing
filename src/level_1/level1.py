#!/usr/bin/env python3
"""Hillside L0 -> calibrated geomagnetic L1; bounded to one channel per worker."""
from __future__ import annotations
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from fractions import Fraction
import copy
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import shutil
import sys
import uuid

import h5py
import numpy as np
from scipy import fft

PACKAGE = Path(__file__).resolve().parent
CHUNK = 262144


def require(test, message):
    if not test:
        raise RuntimeError(message)


def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(8 * 1024**2), b''):
            h.update(block)
    return h.hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    tmp.write_bytes(encoded(value) + b'\n')
    os.replace(tmp, path)


def now():
    return datetime.now(timezone.utc).isoformat()


def end_time(start, count, rate):
    t = datetime.fromisoformat(start.replace('Z', '+00:00'))
    return (t + timedelta(seconds=(count - 1) / rate)).isoformat(timespec='microseconds')


def same_time(a, b):
    return datetime.fromisoformat(str(a).replace('Z', '+00:00')) == datetime.fromisoformat(str(b).replace('Z', '+00:00'))


def versions():
    v = {k: importlib.metadata.version(k) for k in ['mth5', 'mt_metadata', 'numpy', 'scipy', 'h5py']}
    v['python'] = platform.python_version()
    require(v['mth5'] == '0.6.4' and v['mt_metadata'] == '1.0.4',
            'Load NCI-geophys/26.03: this package requires mth5 0.6.4 and mt_metadata 1.0.4.')
    return v


def response_table(cfg):
    path = PACKAGE / cfg['response_file']
    require(sha(path) == cfg['response_sha256'], 'Response checksum differs')
    table = np.loadtxt(path, skiprows=2)
    require(table.shape == (57, 3) and np.isfinite(table).all(), 'Invalid response table')
    require(np.all(np.diff(table[:, 0]) > 0) and np.all(table[:, :2] > 0), 'Invalid frequencies/amplitudes')
    return table


def response(f, table):
    """Linear frequency/amplitude/phase interpolation, matching MTH5 FAP slinear."""
    require(np.all(f >= table[0, 0]) and np.all(f <= table[-1, 0]), 'Response extrapolation refused')
    amplitude = np.interp(f, table[:, 0], table[:, 1])
    phase = np.interp(f, table[:, 0], np.deg2rad(table[:, 2]))
    return amplitude * np.exp(1j * phase)


def magnetic_calibration(values, cfg, table):
    """Remove shared complex response, with explicit DC/band/edge treatment."""
    rate = cfg['output_sample_rate_hz']
    spec = cfg['magnetic_fft']
    require(spec['remove_mean'] is True, 'Magnetic DC cannot be recovered from an induction coil')
    values -= values.mean()
    pad = min(len(values) - 1, int(math.ceil(spec['reflection_seconds'] * rate)))
    padded = np.pad(values, (pad, pad), mode='reflect')
    # Taper only the synthetic reflection, leaving the recorded interior untapered.
    if pad:
        ramp = np.sin(np.linspace(0, np.pi / 2, pad, endpoint=False)) ** 2
        padded[:pad] *= ramp
        padded[-pad:] *= ramp[::-1]
    nfft = fft.next_fast_len(len(padded), real=True)
    spectra = fft.rfft(padded, n=nfft, workers=1, overwrite_x=True)
    del padded
    scale = cfg['microvolts_per_count'] / (cfg['magnetic_sensitivity_mv_per_nt'] * 1000)
    low, high = spec['low_taper_start_hz'], spec['low_taper_end_hz']
    require(table[0, 0] <= low < high < rate / 2 <= table[-1, 0], 'FFT band outside response support')
    for start in range(0, len(spectra), CHUNK):
        stop = min(start + CHUNK, len(spectra))
        f = np.arange(start, stop, dtype=np.float64) * rate / nfft
        good = (f >= low) & (f < rate / 2)
        multiplier = np.zeros(len(f), dtype=np.complex128)
        fg = f[good]
        weight = np.ones(len(fg))
        taper = fg < high
        weight[taper] = 0.5 * (1 - np.cos(np.pi * (fg[taper] - low) / (high - low)))
        multiplier[good] = scale * weight / response(fg, table)
        spectra[start:stop] *= multiplier
    restored = fft.irfft(spectra, n=nfft, workers=1, overwrite_x=True)
    result = restored[pad:pad + len(values)].copy()
    require(np.isfinite(result).all(), 'Non-finite magnetic output')
    return result, {'magnetic_sensitivity_mv_per_nt': cfg['magnetic_sensitivity_mv_per_nt'],
                    'magnetic_scale_nt_per_count_before_response': scale,
                    'mean_removed': True, 'reflection_samples_each_end': pad, 'fft_length': nfft,
                    'dc_and_nyquist_removed': True, 'no_response_extrapolation': True,
                    'low_taper_hz': [low, high], 'nominal_low_frequency_limit_hz': max(high, rate / len(values)),
                    'edge_caution_seconds': pad / rate,
                    'edge_caution': 'Frequency-dependent boundary uncertainty; short runs cannot resolve long periods. Reflection adds no observations.'}


def direction(station, name, azimuth, cfg):
    if station in cfg['uncorrected_stations']:
        return 1, azimuth, 'not_applied_unknown_azimuth'
    if name == 'bz':
        return 1, azimuth, 'vertical_unchanged'
    normal = 0 if name in ('ex', 'bx') else 90
    require(azimuth in (normal, normal + 180), f'Unexpected azimuth {station}/{name}: {azimuth}')
    if azimuth == normal:
        return 1, normal, 'not_required_already_aligned'
    return -1, normal, 'documented_direction_corrected'


def direction_comment(name, angle, status, cfg):
    if status == 'not_applied_unknown_azimuth':
        return cfg['uncorrected_comment']
    if status == 'documented_direction_corrected':
        return f'{name.capitalize()} polarity corrected.'
    return ''


def append_comments(original, *added):
    additions = ' '.join(part for part in added if part)
    if not additions:
        return original
    return (original.rstrip() + ' ' + additions).strip()


def acquisition_comment_for_output(value, record):
    # This source sentence described Level 0's last run, which can be excluded
    # from Level 1. Scope its replacement to the actual selected coverage.
    phrase = 'Retained-run metadata records the sampling rate, time coverage and available channels; Ey is absent from the final retained run.'
    if record['station_id'] != 'HSSL03' or phrase not in value:
        return value
    coverage = ('All retained Level 1 runs include Ey.' if all('ey' in r['channels'] for r in record['runs'])
                else 'Ey availability is recorded separately for each retained Level 1 run.')
    return value.replace(phrase, 'Level 1 run metadata records the sampling rate, time coverage and available channels. ' + coverage)


def fingerprint(cfg):
    return hashlib.sha256(encoded({'settings': cfg, 'code_sha256': sha(__file__)})).hexdigest()


def roots(cfg):
    batch = Path(cfg['batch'])
    suffix = f"{cfg['release']}_{cfg['output_sample_rate_hz']}hz"
    return (batch / 'level0' / ('10_mth5_' + cfg['source_revision']),
            batch / 'metadata' / ('09_json_metadata_' + cfg['source_revision']),
            batch / 'level1' / ('11_mth5_' + suffix),
            batch / 'reports' / ('hillside_level1_' + suffix))


def select_runs(record, cfg):
    """Apply the duration rule within one station, using exact source coverage."""
    policy = cfg['run_selection']
    require(policy['exclude_short_only_if_station_has_run_above_threshold'] is True,
            'Short runs may only be excluded if that station has a longer-than-threshold run')
    threshold = Fraction(str(policy['short_run_max_seconds']))
    require(threshold > 0, 'Run duration threshold must be positive')
    durations = {}
    for run in record['runs']:
        count = run['sample_coverage']['samples_per_channel']
        require(isinstance(count, int) and not isinstance(count, bool) and count > 0, 'Invalid source sample count')
        rate = Fraction(str(run['sample_rate']))
        require(rate > 0 and run['id'] not in durations, 'Invalid rate or duplicate run ID')
        durations[run['id']] = Fraction(count) / rate
    require(durations, 'Station has no source runs')
    has_long = any(duration > threshold for duration in durations.values())
    kept, excluded, decisions = [], [], []
    for run in record['runs']:
        duration = durations[run['id']]
        drop = has_long and duration <= threshold
        (excluded if drop else kept).append(run['id'])
        decisions.append({'run_id': run['id'], 'duration_seconds': float(duration),
                          'source_samples_per_channel': run['sample_coverage']['samples_per_channel'],
                          'source_sample_rate_hz': run['sample_rate'], 'channels': sorted(run['channels']),
                          'action': 'exclude' if drop else 'retain',
                          'reason': 'at_or_below_threshold_with_longer_run_at_station' if drop else
                                    'above_threshold' if duration > threshold else 'no_run_above_threshold_at_station'})
    return {'station': record['station_id'], 'threshold_seconds': float(threshold),
            'station_has_run_above_threshold': has_long, 'retained_run_ids': kept,
            'excluded_run_ids': excluded, 'decisions': decisions}


def selected_record(record, selection):
    result = copy.deepcopy(record)
    keep = set(selection['retained_run_ids'])
    result['runs'] = [r for r in result['runs'] if r['id'] in keep]
    require(result['runs'], 'Run selection must retain at least one run at each station')
    result['station']['run_list'] = [r['id'] for r in result['runs']]
    result['station']['channels_recorded'] = sorted({c for r in result['runs'] for c in r['channels']})
    result['station']['time_period'] = {
        'start': min(r['time_period']['start'] for r in result['runs']),
        'end': max(r['time_period']['end'] for r in result['runs'])}
    return result


def preflight(cfg):
    versions()
    require(cfg['output_sample_rate_hz'] == cfg['source_sample_rate_hz'] == 1000, 'This release preserves native 1000 Hz; downsampling is disabled')
    require(cfg['output_reference_frame'] == 'geomagnetic' and not cfg['apply_declination'], 'Geographic rotation forbidden')
    require(set(cfg['uncorrected_stations']) == {'HS021', 'HS039', 'HS040', 'HS041', 'HS055', 'HS055b'}, 'Exception policy changed')
    require(cfg['magnetic_sensitivity_mv_per_nt'] == 200 and cfg['microvolts_per_count'] == 1 and cfg['electric_interface_gain'] == 10, 'This release requires 200 mV/nT, 1 microvolt/count and electric interface gain 10')
    response_table(cfg)
    src, meta, out, reports = roots(cfg)
    require(src.resolve() != out.resolve() and src.resolve() not in out.resolve().parents, 'Output must be separate from L0')
    verification = load(Path(cfg['batch']) / 'reports' / 'hillside_metadata_update_20260918_fix1' / 'verification.json')
    require(verification['complete'] and verification['revision'] == cfg['source_revision'] and
            verification['all_recorded_samples_rates_channels_and_times_unchanged'], 'Completed corrected L0 verification missing')
    manifest = load(src / 'manifest.json')
    release = load(src / 'release_manifest.json')
    require(release['complete'] and release['mth5_manifest_sha256'] == sha(src / 'manifest.json'), 'L0 release manifest mismatch')
    for key, expect in [('station_count', cfg['expected_stations']), ('run_count', cfg['expected_source_runs']),
                        ('channel_instances', cfg['expected_source_channel_instances'])]:
        require(manifest[key] == expect, 'Unexpected L0 ' + key)
    require(manifest['complete'] and manifest['data_level'] == 0, 'Expected complete Level 0 inputs')
    jobs, runs, channels = [], 0, 0
    seen = set()
    for entry in manifest['stations']:
        sid = entry['station']
        require(sid not in seen and entry['file'] == sid + '_L0.mth5', 'Duplicate/unexpected station file')
        seen.add(sid)
        path = src / entry['file']
        require(path.is_file() and path.stat().st_size == entry['bytes'], 'L0 file missing/size differs: ' + sid)
        record = load(meta / (sid + '.json'))
        require(record['station_id'] == sid and record['data_level'] == 0, 'Wrong station metadata')
        require(record['station']['orientation']['reference_frame'] == 'geomagnetic', 'Wrong source reference frame')
        mruns = {r['run_id']: r for r in entry['runs']}
        require(len(mruns) == len(record['runs']), 'Duplicate/mismatched source runs')
        comment = record['station']['acquired_by']['comments']
        require('Logger gain: 1;' in comment and 'electric-channel interface gain: 10;' in comment, 'Unexpected gain metadata')
        for r in record['runs']:
            runs += 1
            required_unknown = (('bx', 'by') if sid in ('HS021', 'HS055') else
                                ('ex',) if sid in ('HS039', 'HS040', 'HS041', 'HS055b') else ())
            require(all(r['channels'][name].get('measurement_azimuth') is None for name in required_unknown),
                    'Accepted unknown azimuths differ: ' + sid)
            require(r['sample_rate'] == cfg['source_sample_rate_hz'], 'Unexpected source sampling rate')
            mrun = mruns[r['id']]
            require(set(r['channels']) == set(mrun['channels']), 'Source channel coverage differs')
            for name, cm in r['channels'].items():
                channels += 1
                require(name in ('ex', 'ey', 'bx', 'by', 'bz'), 'Unexpected channel')
                require(cm['units'] == 'ADC counts', 'Input already calibrated or units differ')
                require(r['sample_coverage']['samples_per_channel'] == mrun['channels'][name]['samples'], 'Sample count differs')
                require(r['ascii']['sample_sha256_by_channel'][name] == mrun['channels'][name]['sample_sha256'], 'Source sample digest differs')
                direction(sid, name, cm.get('measurement_azimuth'), cfg)
                if name.startswith('e'):
                    require(float(cm['dipole_length']) > 0, 'Missing electric dipole length')
                else:
                    require(cm['sensor']['model'] == 'LEMI-120', 'Unexpected magnetic sensor')
        selection = select_runs(record, cfg)
        jobs.append({'cfg': cfg, 'station': sid, 'entry': entry, 'source': str(path), 'selection': selection,
                     'metadata': str(meta / (sid + '.json')), 'out': str(out), 'reports': str(reports)})
    require(len(jobs) == cfg['expected_stations'] and runs == cfg['expected_source_runs'] and channels == cfg['expected_source_channel_instances'], 'Manifest totals do not reconcile')
    h = load(meta / 'HSSL03.json')
    require(set(next(r for r in h['runs'] if r['id'] == 'run_0003')['channels']) == {'ex', 'bx', 'by', 'bz'}, 'HSSL03 final-run coverage changed')
    # Conservative uncompressed float64 upper bound, not a promise of compression.
    output_bytes = sum(math.ceil(r['samples'] * cfg['output_sample_rate_hz'] / cfg['source_sample_rate_hz']) * 8
                       for j in jobs for run in j['entry']['runs']
                       if run['run_id'] in j['selection']['retained_run_ids'] for r in run['channels'].values())
    selections = [j['selection'] for j in jobs]
    retained_runs = sum(len(s['retained_run_ids']) for s in selections)
    retained_channels = sum(len(d['channels']) for s in selections for d in s['decisions'] if d['action'] == 'retain')
    existing = next(p for p in [out, out.parent, Path(cfg['batch'])] if p.exists())
    free = shutil.disk_usage(existing).free
    require(free > output_bytes * 1.1, 'Insufficient filesystem free space for conservative full output estimate')
    return jobs, {'created_utc': now(), 'station_count': len(jobs), 'source_run_count': runs,
                  'source_channel_instances': channels, 'run_count': retained_runs, 'channel_instances': retained_channels,
                  'excluded_run_count': runs - retained_runs, 'excluded_channel_instances': channels - retained_channels,
                  'run_selection': selections, 'uncompressed_output_bytes_estimate': output_bytes,
                  'filesystem_free_bytes': free, 'quota_note': 'Filesystem free space is not a project-quota check.',
                  'fingerprint': fingerprint(cfg), 'source_manifest_sha256': sha(src / 'manifest.json')}


def read_values(dataset, digest_expected):
    values = np.empty(len(dataset), dtype=np.float64)
    digest = hashlib.sha256()
    for start in range(0, len(dataset), CHUNK):
        block = dataset[start:start + CHUNK]
        require(block.dtype.kind == 'i', 'Input samples must remain integer L0 counts')
        digest.update(block.astype('<i8', copy=False).tobytes())
        values[start:start + len(block)] = block
    require(digest.hexdigest() == digest_expected, 'Input sample verification failed')
    return values


def sample_sha(dataset):
    digest = hashlib.sha256()
    for start in range(0, len(dataset), CHUNK):
        block = dataset[start:start + CHUNK]
        require(np.isfinite(block).all(), 'Non-finite output samples')
        digest.update(block.astype('<f8', copy=False).tobytes())
    return digest.hexdigest()


def comment_value(obj):
    return getattr(obj, 'value', None) or ''


def verify_output(path, record, receipts, cfg, signature):
    from mth5.mth5 import MTH5
    with MTH5() as m:
        m.open_mth5(path, mode='r')
        require(m.validate_file() and int(m.data_level) == 1, 'Output is not valid Level 1 MTH5')
        station = m.get_station(record['station_id'], survey=record['survey']['name'])
        filter_name = f"hillside_lemi120_l120new_{cfg['magnetic_sensitivity_mv_per_nt']:g}mv"
        fap = m.get_survey(record['survey']['name']).filters_group.to_filter_object(filter_name)
        table = response_table(cfg)
        require(np.array_equal(fap.frequencies, table[:, 0]) and
                np.array_equal(fap.amplitudes, table[:, 1] * cfg['magnetic_sensitivity_mv_per_nt'] * 1000 / cfg['microvolts_per_count']) and
                np.array_equal(fap.phases, np.deg2rad(table[:, 2])), 'Stored magnetic response differs from the adopted calibration')
        require(str(station.metadata.orientation.reference_frame).lower().split('.')[-1] == 'geomagnetic', 'Output frame differs')
        require(set(station.metadata.run_list) == {r['id'] for r in record['runs']}, 'Output run set differs')
        actual_runs = {name for name, obj in station.hdf5_group.items() if str(obj.attrs.get('mth5_type', '')).lower() == 'run'}
        require(actual_runs == {r['id'] for r in record['runs']}, 'Excluded/invented native run group present')
        require(set(station.metadata.channels_recorded) == set(record['station']['channels_recorded']), 'Station coverage differs')
        for rec in receipts:
            run = station.get_run(rec['run_id'])
            require(set(run.hdf5_group) == set(rec['channels']), 'Missing/invented output channel')
            require(run.metadata.sample_rate == cfg['output_sample_rate_hz'], 'Run rate differs')
            for name, expected in rec['channels'].items():
                c = run.get_channel(name)
                require(c.hdf5_dataset.shape == (expected['samples'],) and c.hdf5_dataset.dtype == np.dtype('<f8'), 'Output shape/dtype differs')
                require(c.metadata.sample_rate == cfg['output_sample_rate_hz'], 'Channel rate differs')
                require(same_time(c.metadata.time_period.start, expected['start']) and same_time(c.metadata.time_period.end, expected['end']), 'Output channel times differ')
                require(c.metadata.units == expected['native_units'], 'Output units differ')
                angle = c.metadata.measurement_azimuth
                require((expected['azimuth'] is None and np.isnan(angle)) or angle == expected['azimuth'], 'Unknown/known azimuth was changed')
                require(expected['metadata_comment'] == comment_value(c.metadata.comments), 'Output comment differs from approved metadata')
                require(c.metadata.filters and all(not f.applied for f in c.metadata.filters), 'Removed instrument response flags differ')
                if not name.startswith('e'):
                    require([f.name for f in c.metadata.filters] == [filter_name], 'Magnetic channel references the wrong sensitivity')
                require(sample_sha(c.hdf5_dataset) == expected['sample_sha256'], 'Full output sample readback failed')
        require(len(m.channel_summary.to_dataframe()) == sum(len(r['channels']) for r in receipts), 'Channel summary count differs')
    with h5py.File(path, 'r') as f:
        group = f['MT_Level1_Provenance']
        require(group.attrs['fingerprint'] == signature, 'Embedded settings fingerprint differs')
        require(json.loads(group['settings_json'][()].tobytes()) == cfg, 'Embedded calibration settings differ')
        for ds in group.values():
            require(hashlib.sha256(ds[()].tobytes()).hexdigest() == ds.attrs['sha256'], 'Embedded document checksum differs')


def process_station(job):
    from mth5.mth5 import MTH5
    from mt_metadata.common import Comment
    from mt_metadata.timeseries import Electric, Magnetic
    from mt_metadata.timeseries.filters import FrequencyResponseTableFilter, CoefficientFilter
    cfg, sid = job['cfg'], job['station']
    v = versions()
    signature = fingerprint(cfg)
    source, meta = Path(job['source']), Path(job['metadata'])
    out, reports = Path(job['out']), Path(job['reports'])
    out.mkdir(parents=True, exist_ok=True)
    reports.mkdir(parents=True, exist_ok=True)
    final, receipt_path = out / (sid + '_L1.mth5'), reports / (sid + '.json')
    if final.exists():
        require(receipt_path.exists(), 'Existing output lacks receipt: ' + sid)
        old = load(receipt_path)
        require(old['complete'] and old['fingerprint'] == signature and old['source_sha256'] == job['entry']['sha256'] == sha(source) and old['metadata_sha256'] == sha(meta)
                and old['output_sha256'] == sha(final), 'Existing output differs; choose a new release name')
        print('VERIFIED EXISTING ' + sid, flush=True)
        return old
    lock = out / (sid + '.lock')
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(fd, encoded({'pid': os.getpid(), 'pbs_job': os.environ.get('PBS_JOBID'), 'started': now()}))
    os.close(fd)
    partial = out / (sid + '_' + uuid.uuid4().hex[:10] + '.partial.mth5')
    try:
        require(sha(source) == job['entry']['sha256'], 'Source MTH5 file hash differs: ' + sid)
        stamp = (source.stat().st_size, source.stat().st_mtime_ns)
        meta_bytes = meta.read_bytes()
        source_record = json.loads(meta_bytes)
        selection = select_runs(source_record, cfg)
        require(selection == job['selection'], 'Run selection changed after preflight')
        record = selected_record(source_record, selection)
        table = response_table(cfg)
        receipts = []
        with h5py.File(source, 'r') as f:
            require(f['MT_Level0_Provenance']['metadata_json'][()].tobytes() == meta_bytes, 'Embedded L0 JSON differs from current station JSON')
        with MTH5() as src, MTH5(file_version='0.2.0', data_level=1, compression='gzip', compression_opts=4, shuffle=True) as dst:
            src.open_mth5(source, mode='r')
            require(int(src.data_level) == 0, 'Refusing to calibrate an already processed input')
            dst.open_mth5(partial, mode='x')
            survey_id = record['survey']['name']
            source_survey = src.get_survey(survey_id)
            survey = dst.add_survey(survey_id, survey_metadata=source_survey.metadata.copy())
            source_station = src.get_station(sid, survey=survey_id)
            sm = source_station.metadata.copy()
            # Clear both linked containers before assignment: assigning runs=[]
            # alone causes mt_metadata to restore the old run_list as placeholders.
            sm.runs.clear()
            sm.run_list.clear()
            sm.channels_recorded.clear()
            sm.run_list = record['station']['run_list']
            sm.channels_recorded = record['station']['channels_recorded']
            sm.acquired_by.comments = Comment(value=acquisition_comment_for_output(comment_value(sm.acquired_by.comments), record))
            if sid in cfg['uncorrected_stations']:
                note = cfg['uncorrected_comment']
            else:
                corrected = sorted({name for r in record['runs'] for name, cm in r['channels'].items()
                                    if direction(sid, name, cm.get('measurement_azimuth'), cfg)[0] == -1})
                note = ' '.join(f'{name.capitalize()} polarity corrected.' for name in corrected)
            selection_note = 'Runs of one hour or less excluded.' if selection['excluded_run_ids'] else ''
            sm.comments = Comment(value=append_comments(comment_value(sm.comments), note, selection_note))
            station = dst.add_station(sid, survey=survey_id, station_metadata=sm)
            fap = FrequencyResponseTableFilter(name=f"hillside_lemi120_l120new_{cfg['magnetic_sensitivity_mv_per_nt']:g}mv", frequencies=table[:, 0],
                amplitudes=table[:, 1] * cfg['magnetic_sensitivity_mv_per_nt'] * 1000 / cfg['microvolts_per_count'],
                phases=np.deg2rad(table[:, 2]), units_in='nT', units_out='count')
            fap.comments = Comment(value=f"Adopted shared l120new.rsp and {cfg['magnetic_sensitivity_mv_per_nt']:g} mV/nT; forward response removed once. Linear amplitude/phase interpolation in frequency.")
            survey.filters_group.add_filter(fap)
            for r in record['runs']:
                sr = source_station.get_run(r['id'])
                require(set(sr.hdf5_group) == set(r['channels']), 'Native source channel set differs')
                rm = sr.metadata.copy()
                rm.sample_rate = cfg['output_sample_rate_hz']
                target_run = station.add_run(r['id'], run_metadata=rm)
                receipt = {'run_id': r['id'], 'source_time_period': r['time_period'], 'channels': {}}
                for name, cm in r['channels'].items():
                    print(f'CALIBRATING {sid}/{r["id"]}/{name}', flush=True)
                    sc = sr.get_channel(name)
                    require(sc.metadata.filters == [] and sc.metadata.units == 'digital counts', 'Source response/units are not uncalibrated L0')
                    require(sc.hdf5_dataset.shape == (r['sample_coverage']['samples_per_channel'],), 'Native source count differs')
                    require(sc.metadata.sample_rate == cfg['source_sample_rate_hz'] and same_time(sc.metadata.time_period.start, cm['time_period']['start'])
                            and same_time(sc.metadata.time_period.end, cm['time_period']['end']), 'Native source timing differs')
                    old_angle = cm.get('measurement_azimuth')
                    require((old_angle is None and np.isnan(sc.metadata.measurement_azimuth)) or old_angle == sc.metadata.measurement_azimuth, 'Native source azimuth differs')
                    data = read_values(sc.hdf5_dataset, r['ascii']['sample_sha256_by_channel'][name])
                    extras = {}
                    if name.startswith('e'):
                        gain = cfg['electric_interface_gain'] * float(cm['dipole_length']) / cfg['microvolts_per_count']
                        data /= gain
                        filt = CoefficientFilter(name=f'{sid.lower()}_{r["id"]}_{name}_electric_scale',
                            gain=gain, units_in='mV/km', units_out='count')
                        survey.filters_group.add_filter(filt)
                        filter_name, units = filt.name, 'mV/km'
                        extras['electric_dipole_metres'] = float(cm['dipole_length'])
                    else:
                        data, extras = magnetic_calibration(data, cfg, table)
                        filter_name, units = fap.name, 'nT'
                    sign, angle, status = direction(sid, name, old_angle, cfg)
                    data *= sign
                    # MTH5 dynamically subclasses channel metadata; copying that
                    # subclass fails its add_channel type guard. Rebuild the plain
                    # public class, preserving values but not file references.
                    native = Electric() if name.startswith('e') else Magnetic()
                    native.from_dict({k: value for k, value in sc.metadata.to_dict(single=True, required=False).items()
                                      if k not in ('hdf5_reference', 'mth5_type')})
                    native.units = units
                    native.sample_rate = cfg['output_sample_rate_hz']
                    native.measurement_azimuth = float('nan') if angle is None else angle
                    native.time_period.end = end_time(cm['time_period']['start'], len(data), cfg['output_sample_rate_hz'])
                    native.filters = []
                    native.add_filter(name=filter_name, applied=False)
                    channel_note = direction_comment(name, angle, status, cfg)
                    native.comments = Comment(value=append_comments(comment_value(native.comments), channel_note))
                    channel = target_run.add_channel(name, 'electric' if name.startswith('e') else 'magnetic', data=None,
                        shape=(len(data),), max_shape=(len(data),), chunks=(min(65536, len(data)),), channel_dtype='<f8',
                        channel_metadata=native, **dst.dataset_options)
                    digest = hashlib.sha256()
                    for start in range(0, len(data), CHUNK):
                        block = data[start:start + CHUNK].astype('<f8', copy=False)
                        require(np.isfinite(block).all(), 'Non-finite calibrated output')
                        channel.hdf5_dataset[start:start + len(block)] = block
                        digest.update(block.tobytes())
                    receipt['channels'][name] = {'samples': len(data), 'source_samples': len(sc.hdf5_dataset),
                        'sample_sha256': digest.hexdigest(), 'source_sample_sha256': r['ascii']['sample_sha256_by_channel'][name],
                        'start': cm['time_period']['start'], 'end': native.time_period.end.isoformat(),
                        'native_units': native.units, 'azimuth': angle, 'source_azimuth': old_angle,
                        'sign_factor': sign, 'direction_status': status,
                        'direction_correction_applied': sign == -1, 'direction_comment': channel_note,
                        'metadata_comment': comment_value(native.comments), 'processing': extras}
                    channel.write_metadata()
                    del data
                target_run.update_metadata()
                receipts.append(receipt)
            station.update_metadata()
            survey.metadata.time_period.start_date = record['survey']['time_period']['start_date']
            survey.metadata.time_period.end_date = record['survey']['time_period']['end_date']
            survey.write_metadata()
            dst.channel_summary.summarize()
        # The native metadata library inserts defaults for some unprovided fields.
        # Preserve source omissions (unknown locations etc.) in the derived file.
        with h5py.File(source, 'r') as a, h5py.File(partial, 'a') as b:
            base = f'/Experiment/Surveys/{record["survey"]["name"]}/Stations/{sid}'
            for r in record['runs']:
                for name in r['channels']:
                    old, new = a[base + '/' + r['id'] + '/' + name], b[base + '/' + r['id'] + '/' + name]
                    for key in list(new.attrs):
                        if key.startswith(('location.', 'positive.', 'negative.')) and key not in old.attrs:
                            del new.attrs[key]
            group = b.create_group('MT_Level1_Provenance')
            group.attrs['fingerprint'] = signature
            payloads = {'source_metadata_json': meta_bytes, 'settings_json': encoded(cfg),
                        'processing_json': encoded({'created_utc': now(), 'software': v, 'source_file': str(source),
                            'source_file_sha256': job['entry']['sha256'], 'declination_applied': False,
                            'anti_alias_filter_applied': False, 'resampling_applied': False,
                            'logger_frequency_response': 'No separate PR6-24 frequency-response table supplied; nominal stored-format scale applied.',
                            'run_selection': selection, 'runs': receipts}),
                        'run_selection_json': encoded(selection), 'l120new_rsp': (PACKAGE / cfg['response_file']).read_bytes()}
            for name, payload in payloads.items():
                ds = group.create_dataset(name, data=np.frombuffer(payload, dtype=np.uint8), compression='gzip')
                ds.attrs['sha256'] = hashlib.sha256(payload).hexdigest()
        verify_output(partial, record, receipts, cfg, signature)
        require(stamp == (source.stat().st_size, source.stat().st_mtime_ns) and meta.read_bytes() == meta_bytes, 'Source changed during processing')
        require(not final.exists(), 'Final output appeared during processing')
        result = {'complete': True, 'station': sid, 'fingerprint': signature, 'source_sha256': job['entry']['sha256'],
                  'metadata_sha256': hashlib.sha256(meta_bytes).hexdigest(), 'output_sha256': sha(partial),
                  'file': final.name, 'bytes': partial.stat().st_size, 'run_count': len(receipts), 'runs': receipts,
                  'source_run_count': len(source_record['runs']), 'run_selection': selection,
                  'sample_readback_verified': True, 'native_metadata_verified': True, 'software': v}
        # Receipt first: a crash between these two actions leaves only an orphan
        # receipt, not an unverified final file. A retry safely replaces the receipt.
        save(receipt_path, result)
        partial.rename(final)
        print('VERIFIED LEVEL 1 ' + sid, flush=True)
        return result
    finally:
        lock.unlink(missing_ok=True)


def run_batch(cfg, mode, workers):
    jobs, pre = preflight(cfg)
    out, reports = roots(cfg)[2:]
    reports.mkdir(parents=True, exist_ok=True)
    save(reports / ('preflight_' + mode + '.json'), pre)
    save(reports / 'run_selection.json', {'policy': cfg['run_selection'], 'source_run_count': pre['source_run_count'],
         'retained_run_count': pre['run_count'], 'excluded_run_count': pre['excluded_run_count'],
         'retained_channel_instances': pre['channel_instances'], 'stations': pre['run_selection']})
    if mode == 'preflight':
        print(json.dumps(pre, indent=2))
        return
    if mode == 'pilot':
        jobs = [j for j in jobs if j['station'] == 'HS074']
    elif mode == 'all':
        pilot = load(reports / 'pilot_verification.json')
        require(pilot['complete'] and pilot['fingerprint'] == fingerprint(cfg), 'Matching HS074 pilot must pass first')
    results, errors = [], []
    if workers == 1:
        for job in jobs:
            results.append(process_station(job))
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            tasks = {pool.submit(process_station, job): job['station'] for job in jobs}
            for future in as_completed(tasks):
                try:
                    results.append(future.result())
                except Exception as exc:
                    errors.append({'station': tasks[future], 'error': repr(exc)})
                    print('FAILED', tasks[future], repr(exc), flush=True)
    summary = {'complete': not errors and len(results) == len(jobs), 'mode': mode, 'created_utc': now(),
               'fingerprint': fingerprint(cfg), 'station_count': len(results),
               'run_count': sum(x['run_count'] for x in results),
               'source_run_count': sum(x['source_run_count'] for x in results),
               'excluded_run_count': sum(len(x['run_selection']['excluded_run_ids']) for x in results),
               'channel_instances': sum(len(r['channels']) for x in results for r in x['runs']),
               'output_sample_rate_hz': cfg['output_sample_rate_hz'], 'reference_frame': 'geomagnetic',
               'magnetic_sensitivity_mv_per_nt': cfg['magnetic_sensitivity_mv_per_nt'],
               'release': cfg['release'], 'supersedes_release': cfg.get('supersedes_release'),
               'geographic_rotation_applied': False, 'resampling_applied': False, 'source_level0_unchanged': True,
               'uncorrected_stations': cfg['uncorrected_stations'], 'errors': errors,
               'output_directory': str(out), 'stations': sorted(results, key=lambda x: x['station'])}
    save(reports / (mode + '_verification.json'), summary)
    require(summary['complete'], 'Some stations failed; inspect the verification report and job output')
    if mode == 'all':
        require(summary['station_count'] == cfg['expected_stations'] and summary['run_count'] == pre['run_count']
                and summary['channel_instances'] == pre['channel_instances']
                and summary['excluded_run_count'] == pre['excluded_run_count'], 'Final selected totals differ')
        save(out / 'release_manifest.json', summary)
    print(f'COMPLETE {mode}: {len(results)} stations; report: {reports / (mode + "_verification.json")}', flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=['preflight', 'pilot', 'all'])
    p.add_argument('--config', type=Path, default=PACKAGE / 'config.json')
    p.add_argument('--workers', type=int, default=1)
    args = p.parse_args()
    require(args.workers >= 1, 'Workers must be positive')
    run_batch(load(args.config), args.mode, args.workers)


if __name__ == '__main__':
    main()
