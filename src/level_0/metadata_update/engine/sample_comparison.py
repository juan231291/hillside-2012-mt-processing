"""Exact numeric comparison for ASCII and MiniSEED 2; no waveform transforms.

ASCII decimal values are compared exactly. IEEE floating-point MiniSEED values
are converted to their exact decimal representations, not rounded display text.
Timing/identity evidence remains distinct from equality of sample sequences.
"""
from __future__ import annotations

import hashlib
import math
import re
import warnings
from decimal import Decimal
from fractions import Fraction
from itertools import zip_longest

MAX_MSEED_BYTES = 64 * 1024**2
MAX_DECODED_SAMPLES = 16_000_000
NUMBER = re.compile(rb'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?')


class SampleComparisonError(ValueError):
    pass


def file_stamp(path):
    s = path.stat()
    return s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_ino


def canonical(value):
    """Context-independent decimal normalization; no precision loss or tolerance."""
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SampleComparisonError('Non-finite sample value')
        number = Decimal.from_float(value)
    elif isinstance(value, Decimal):
        number = value
    else:
        number = Decimal(value)
    if not number.is_finite():
        raise SampleComparisonError('Non-finite sample value')
    if number.is_zero():
        return b'0\n'
    sign, digits, exponent = number.as_tuple()
    digits = ''.join(map(str, digits))
    trimmed = digits.rstrip('0')
    exponent += len(digits) - len(trimmed)
    return (('-' if sign else '') + trimmed + 'e' + str(exponent) + '\n').encode('ascii')


def ascii_samples(path, info):
    with path.open('rb') as f:
        line_number = 0
        while True:
            line = f.readline(4097)
            if not line:
                break
            line_number += 1
            if len(line) > 4096:
                raise SampleComparisonError(f'ASCII line {line_number} exceeds 4096 bytes')
            if line_number == 1:
                line = line.removeprefix(b'\xef\xbb\xbf')
            token = line.strip()
            if not token:
                info['blank_lines_ignored'] += 1
                continue
            if len(token) > 256 or not NUMBER.fullmatch(token):
                raise SampleComparisonError(f'Invalid numeric ASCII sample at line {line_number}; no rows were silently skipped')
            try:
                value = canonical(Decimal(token.decode('ascii')))
            except ArithmeticError as exc:
                raise SampleComparisonError(f'Invalid numeric ASCII sample at line {line_number}: {exc}') from exc
            yield value


def mseed_read(path, headonly):
    from obspy import read
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            with path.open('rb') as f:
                result = read(f, format='MSEED', headonly=headonly, apply_calib=False, check_compression=False)
        if caught:
            raise SampleComparisonError('MiniSEED parser warning: ' + '; '.join(str(w.message) for w in caught))
        if not result:
            raise SampleComparisonError('MiniSEED reader returned no traces')
        return result
    except SampleComparisonError:
        raise
    except Exception as exc:
        raise SampleComparisonError('MiniSEED decode failed: ' + str(exc)) from exc


def timeline(traces, expected_channel, info):
    """Retain trace metadata; overlapping traces require review, never trimming."""
    spans = []
    previous_end = None
    for tr in sorted(traces, key=lambda t: (t.stats.starttime.ns, t.id)):
        s = tr.stats
        rate, count = float(s.sampling_rate), int(s.npts)
        if not math.isfinite(rate) or rate <= 0 or count <= 0:
            raise SampleComparisonError('Invalid MiniSEED sample rate or count')
        if str(s.channel).casefold() != expected_channel.casefold():
            raise SampleComparisonError(f'MiniSEED channel {s.channel!r} does not match expected {expected_channel!r}; no mapping assumed')
        start = int(s.starttime.ns)
        step = Fraction(1_000_000_000, 1) / Fraction(str(rate))
        end = start + count * step
        if previous_end is not None:
            delta = start - previous_end
            if delta < -1:
                raise SampleComparisonError('Overlapping MiniSEED traces: sample order is ambiguous; no samples removed')
            if delta > 1:
                info['flags'].append('gap_between_traces')
        info['traces'].append({
            'seed_id': tr.id, 'channel': str(s.channel), 'sample_rate_hz': rate,
            'samples': count, 'start_utc': str(s.starttime), 'start_ns': start,
            'last_sample_utc': str(s.endtime), 'end_exclusive_utc': str(s.starttime + count / rate),
        })
        if spans and spans[-1][1] == step and abs(start - (spans[-1][0] + spans[-1][2] * step)) <= 1:
            spans[-1][2] += count
        else:
            spans.append([start, step, count])
        previous_end = end
    if len({r['seed_id'] for r in info['traces']}) > 1:
        info['flags'].append('header_identity_changes_within_file')
    if len({r['sample_rate_hz'] for r in info['traces']}) > 1:
        info['flags'].append('sample_rate_changes_within_file')
    info['flags'] = sorted(set(info['flags']))
    info['timeline'] = [[start, str(step), count] for start, step, count in spans]


def prepare(path, channel, max_mseed_bytes, max_decoded_samples):
    import numpy as np
    with path.open('rb') as f:
        preview = f.read(4096)
    info = {'format': None, 'traces': [], 'flags': [], 'timeline': None,
            'blank_lines_ignored': 0, 'sample_count': 0, 'sample_sha256': None}
    is_mseed = len(preview) >= 48 and re.fullmatch(rb'[0-9 ]{6}[DRQM] ', preview[:8])
    if is_mseed:
        info['format'] = 'MiniSEED2'
        if path.stat().st_size > max_mseed_bytes:
            raise SampleComparisonError('MiniSEED file exceeds the configured decode byte limit')
        head = mseed_read(path, True)
        expected_count = sum(int(t.stats.npts) for t in head)
        if expected_count > max_decoded_samples:
            raise SampleComparisonError('MiniSEED headers exceed the configured decoded-sample limit')
        timeline(head, channel, {'traces': [], 'flags': []})
        traces = mseed_read(path, False)
        count = sum(len(t.data) for t in traces)
        if count != expected_count or count > max_decoded_samples:
            raise SampleComparisonError('MiniSEED decoded count differs from headers or exceeds the limit')
        timeline(traces, channel, info)
        ordered = sorted(traces, key=lambda t: (t.stats.starttime.ns, t.id))
        for t in ordered:
            if len(t.data) != int(t.stats.npts) or t.data.ndim != 1 or t.data.dtype.kind not in 'iuf':
                raise SampleComparisonError('Unsupported MiniSEED sample array')
            if np.ma.isMaskedArray(t.data) and np.any(np.ma.getmaskarray(t.data)):
                raise SampleComparisonError('Masked/missing MiniSEED samples; no gap filling performed')
        def values():
            for t in ordered:
                for value in t.data:
                    yield canonical(value.item())
        return values(), info
    if preview.startswith(b'MS\x03'):
        raise SampleComparisonError('MiniSEED 3 is not supported by this MiniSEED 2 decoder')
    try:
        preview.removeprefix(b'\xef\xbb\xbf').decode('ascii')
    except UnicodeDecodeError as exc:
        raise SampleComparisonError('Unsupported binary format for sample comparison') from exc
    if b'\x00' in preview:
        raise SampleComparisonError('Unsupported binary format for sample comparison')
    info['format'] = 'numeric_ASCII'
    return ascii_samples(path, info), info


def compare_samples(left, right, channel, max_mseed_bytes=MAX_MSEED_BYTES, max_decoded_samples=MAX_DECODED_SAMPLES):
    before = [file_stamp(p) for p in (left, right)]
    iterators, info = [], []
    try:
        for p in (left, right):
            values, meta = prepare(p, channel, max_mseed_bytes, max_decoded_samples)
            iterators.append(values)
            info.append(meta)
        hashes = [hashlib.sha256(), hashlib.sha256()]
        missing, first_difference = object(), None
        for index, values in enumerate(zip_longest(*iterators, fillvalue=missing)):
            if (missing in values or values[0] != values[1]) and first_difference is None:
                first_difference = index
            for i, value in enumerate(values):
                if value is not missing:
                    info[i]['sample_count'] += 1
                    hashes[i].update(value)
        if any(m['sample_count'] == 0 for m in info):
            raise SampleComparisonError('A source contains no numeric samples')
        for i, p in enumerate((left, right)):
            if before[i] != file_stamp(p):
                raise SampleComparisonError('Source changed during sample comparison')
            info[i]['sample_sha256'] = hashes[i].hexdigest()
        flags = sorted(set(flag for meta in info for flag in meta['flags']))
        both_timed = all(meta['format'] == 'MiniSEED2' for meta in info)
        if both_timed:
            timing = 'same_header_timeline' if info[0]['timeline'] == info[1]['timeline'] else 'header_timing_conflict'
            if timing == 'header_timing_conflict':
                flags.append(timing)
            if {t['seed_id'] for t in info[0]['traces']} != {t['seed_id'] for t in info[1]['traces']}:
                flags.append('header_identity_differs_between_files')
        else:
            timing = 'not_verifiable_from_ASCII_samples'
        return {'method': 'exact_decoded_numeric_sequence', 'equal_samples': first_difference is None,
                'first_difference_index_zero_based': first_difference, 'files': info,
                'timing_status': timing, 'context_flags': sorted(set(flags)),
                'units_and_gain_verified': False, 'ownership_verified': False}
    finally:
        for it in iterators:
            it.close()
