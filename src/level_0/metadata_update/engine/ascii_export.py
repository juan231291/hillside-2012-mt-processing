"""Bounded, exact integer export and independent ASCII readback.

Uses the previously validated Step 05 source readers. Format is one run per
file, with a commented header and tab-separated unscaled sample columns.
"""
from __future__ import annotations
from collections import Counter
from datetime import datetime, timedelta, timezone
from fractions import Fraction
import hashlib
from pathlib import Path, PurePosixPath
import re
import warnings

import numpy as np
import qc_samples

ENCODING = 'signed_int64_little_endian_values_v1'
CHANNELS = ('ex', 'ey', 'bx', 'by', 'bz')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024**2), b''):
            h.update(block)
    return h.hexdigest()


def iso_ns(value):
    seconds, remainder = divmod(round(Fraction(value)), 10**9)
    stamp = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)
    return stamp.strftime('%Y-%m-%dT%H:%M:%S') + ('.' + f'{remainder:09d}'.rstrip('0') if remainder else '') + 'Z'


def source_key(snapshot):
    role, label, _ = snapshot['group_id'].removeprefix('group:').split('/', 2)
    return role + '/' + label


def source_path(snapshot, channel, roots):
    info = snapshot['channels'][channel]
    root = Path(roots[source_key(snapshot)]).resolve(strict=True)
    rel = PurePosixPath(info['relative_path'])
    require(not rel.is_absolute() and '..' not in rel.parts and '\\' not in str(rel) and ':' not in str(rel),
            'Unexpected relative source path')
    require(rel.suffix.lower() == '.' + channel, 'Source extension/channel mismatch')
    path = root.joinpath(*rel.parts)
    require(not any(p.is_symlink() for p in (path, *path.parents) if root in p.parents),
            'Nested source symlink requires review')
    actual = path.resolve(strict=True)
    require(root in actual.parents and actual.is_file(), 'Source is outside its root or is not a regular file')
    require(actual.stat().st_size == info['bytes'], 'Source size differs from QC: ' + str(actual))
    return actual


class CheckedChannel:
    """Read an entire source once; validate source and decoded-sequence hashes."""
    def __init__(self, path, info):
        require(info['status'] == 'ok' and info.get('sample_hash_encoding') == ENCODING,
                'This exporter requires QC-verified signed int64 samples; no conversion/rounding is permitted')
        self.path, self.info = path, info
        self.before = qc_samples.stamp(path)
        self.file_hash, self.value_hash = hashlib.sha256(), hashlib.sha256()
        self.count = 0
        self.pending = np.empty(0, dtype=np.int64)
        self.position = 0
        self.verified = False
        self.iterator = self._blocks()

    def _blocks(self):
        if self.info['format'] == 'numeric_ASCII':
            blocks = qc_samples.ascii_blocks(self.path, hasher=self.file_hash)
        elif self.info['format'] == 'MiniSEED2':
            with self.path.open('rb') as f:
                for block in iter(lambda: f.read(1024**2), b''):
                    self.file_hash.update(block)
            traces = qc_samples.decoded_mseed(self.path, self.info['channel'])
            require(len(traces) == len(self.info['segments']), 'MiniSEED segment count differs from QC')
            for trace, segment in zip(traces, self.info['segments'], strict=True):
                require(Fraction(int(trace.stats.starttime.ns)) == Fraction(segment['start_ns'])
                        and Fraction(str(float(trace.stats.sampling_rate))) == Fraction(segment['rate_hz'])
                        and len(trace.data) == segment['samples'] and trace.id == segment['seed_id'],
                        'MiniSEED timing, count or identifier differs from QC')
            blocks = (trace.data for trace in traces)
        else:
            raise ValueError('Unsupported source format: ' + str(self.info['format']))
        try:
            for values in blocks:
                require(isinstance(values, np.ndarray) and values.ndim == 1 and values.dtype.kind in 'iu',
                        'Source is not an exact integer sequence; no float conversion is permitted')
                require(len(values) > 0 and int(values.min()) >= -(2**63) and int(values.max()) <= 2**63-1,
                        'Source sample exceeds signed int64 range')
                values = values.astype('<i8', copy=False)
                self.count += len(values)
                require(self.count <= self.info['sample_count'], 'Source has more samples than QC recorded')
                self.value_hash.update(values.tobytes())
                yield values
        finally:
            blocks.close()
        require(self.count == self.info['sample_count'], 'Source sample count differs from QC')
        require(self.file_hash.hexdigest() == self.info['file_sha256'], 'Source bytes differ from QC: ' + str(self.path))
        require(self.value_hash.hexdigest() == self.info['sample_sha256'], 'Source sample sequence differs from QC')
        require(qc_samples.stamp(self.path) == self.before, 'Source changed during export')
        self.verified = True

    def take(self, count):
        result = np.empty(count, dtype=np.int64)
        written = 0
        while written < count:
            if self.position == len(self.pending):
                try:
                    self.pending = next(self.iterator)
                except StopIteration as exc:
                    raise ValueError('Source ended before the selected sample count') from exc
                self.position = 0
            take = min(count - written, len(self.pending) - self.position)
            result[written:written+take] = self.pending[self.position:self.position+take]
            self.position += take
            written += take
        return result

    def finish(self):
        require(self.position == len(self.pending), 'Unconsumed source samples remain')
        try:
            next(self.iterator)
        except StopIteration:
            pass
        else:
            raise ValueError('Unexpected source samples after the selected end')
        require(self.verified, 'Source verification did not finish')
        return {'file_id': self.info['file_id'], 'path_used': str(self.path),
                'sample_count': self.count, 'source_sha256': self.file_hash.hexdigest(),
                'sample_sha256': self.value_hash.hexdigest(), 'sample_hash_encoding': ENCODING,
                'source_unchanged_during_read': True}

    def close(self):
        self.iterator.close()


def header_for(run, selection_hash):
    fields = [
        ('schema', 'mtl0.ascii_run/1'), ('station', run['station']), ('run_id', run['run_id']),
        ('start_utc_assumed', run['start_utc_assumed']),
        ('end_exclusive_utc_assumed', run['end_exclusive_utc_assumed']),
        ('last_sample_utc_assumed', iso_ns(Fraction(run['end_exclusive_ns']) - Fraction(10**9)/Fraction(run['rate_hz']))),
        ('sample_rate_hz', run['rate_hz']), ('samples_per_channel', run['samples_per_channel']),
        ('columns', ' '.join(c.upper() for c in run['channels'])),
        ('sample_representation', 'signed_int64'), ('values', 'unscaled source samples'),
        ('rate_basis', '; '.join(run['rate_bases'])),
        ('time_index', 'zero-based sample i occurs at start + i/sample_rate_hz'),
        ('selection_evidence_sha256', selection_hash)]
    require(all('\n' not in str(v) and '\r' not in str(v) for _,v in fields), 'Invalid header value')
    return ''.join(f'# {key}: {value}\n' for key,value in fields).encode('ascii')


def verify_ascii(path, header, run, expected_hashes):
    """Parse complete tabular output independently of the source parser/writer."""
    columns = len(run['channels'])
    token = rb'-?(?:0|[1-9][0-9]{0,18})'
    row = token + (rb'\t' + token) * (columns - 1) + rb'\n'
    valid = re.compile(rb'(?:' + row + rb')+')
    count = 0
    file_hash = hashlib.sha256()
    channel_hashes = {c: hashlib.sha256() for c in run['channels']}
    with path.open('rb') as f:
        observed = f.read(len(header))
        require(observed == header, 'Exported header differs from the plan')
        file_hash.update(observed)
        while True:
            block = f.read(1024**2)
            if not block:
                break
            if not block.endswith(b'\n'):
                block += f.readline(256)
            require(bool(valid.fullmatch(block)), 'Invalid ASCII row, column count or final newline')
            file_hash.update(block)
            rows = block.count(b'\n')
            # Python integer fallback prevents np.fromstring overflow from
            # accepting a changed 19-digit token as the same boundary value.
            if re.search(rb'[0-9]{19}', block):
                values = [int(v) for v in block.split()]
                require(all(-(2**63) <= v <= 2**63-1 for v in values), 'ASCII integer overflow')
                array = np.array(values, dtype=np.int64)
            else:
                with warnings.catch_warnings():
                    warnings.simplefilter('error')
                    array = np.fromstring(block.decode('ascii'), dtype=np.int64, sep=' ')
            require(len(array) == rows * columns, 'ASCII parser count differs from row/column counts')
            array = array.reshape(rows, columns)
            count += rows
            require(count <= run['samples_per_channel'], 'ASCII output has extra rows')
            for i,c in enumerate(run['channels']):
                channel_hashes[c].update(array[:,i].astype('<i8', copy=False).tobytes())
    observed_hashes = {c:h.hexdigest() for c,h in channel_hashes.items()}
    require(count == run['samples_per_channel'], 'ASCII output sample count differs')
    require(observed_hashes == expected_hashes, 'ASCII readback values or channel order differ')
    return {'ascii_sha256': file_hash.hexdigest(), 'sample_sha256_by_channel': observed_hashes,
            'readback_samples_per_channel': count, 'ascii_bytes': path.stat().st_size,
            'header_verified': True, 'sample_values_verified': True}


def export_run(run, selections, roots, station_dir, selection_hash, chunk_rows=16384):
    final = station_dir / (run['station'] + '_' + run['run_id'] + '.txt')
    partial = final.with_suffix('.txt.partial')
    require(not final.exists() and not partial.exists(), 'Export path already exists')
    header = header_for(run, selection_hash)
    hashes = {c:hashlib.sha256() for c in run['channels']}
    sources = []
    print(f"Exporting {run['station']}/{run['run_id']}: {run['samples_per_channel']} rows", flush=True)
    with partial.open('x', encoding='ascii', newline='\n') as f:
        f.write(header.decode('ascii'))
        for index, gid in enumerate(run['group_ids'], 1):
            g = selections[gid]['qc_snapshot']
            readers = {}
            try:
                for c in run['channels']:
                    readers[c] = CheckedChannel(source_path(g,c,roots), g['channels'][c])
                count = selections[gid]['samples_per_channel']
                for offset in range(0, count, chunk_rows):
                    n = min(chunk_rows, count-offset)
                    values = np.column_stack([readers[c].take(n) for c in run['channels']])
                    for i,c in enumerate(run['channels']):
                        hashes[c].update(values[:,i].astype('<i8', copy=False).tobytes())
                    np.savetxt(f, values, fmt='%d', delimiter='\t')
                sources.append({'group_id': gid, 'channels': {c:reader.finish() for c,reader in readers.items()}})
            finally:
                for reader in readers.values():
                    reader.close()
            if index % 25 == 0:
                print(f"{run['station']}/{run['run_id']}: {index}/{len(run['group_ids'])} source groups", flush=True)
    expected = {c:h.hexdigest() for c,h in hashes.items()}
    proof = verify_ascii(partial, header, run, expected)
    require(not final.exists(), 'Final export appeared while writing; refusing to replace it')
    partial.rename(final)
    print(f"VERIFIED {run['station']}/{run['run_id']}: {proof['readback_samples_per_channel']} rows, {len(run['channels'])} channels", flush=True)
    return {'record_type': 'verified_ascii_run', 'id': 'ascii:' + run['station'] + '/' + run['run_id'],
            'station': run['station'], 'run_id': run['run_id'], 'relative_path': run['station'] + '/' + final.name,
            'plan': run, 'header_ascii': header.decode('ascii'), 'sample_hash_encoding': ENCODING,
            'source_verification': sources, **proof}


def export_station(station, runs, selections, roots, out, selection_hash, chunk_rows):
    station_dir = Path(out)/station
    station_dir.mkdir(exist_ok=False)
    return [export_run(r,selections,roots,station_dir,selection_hash,chunk_rows) for r in runs]
