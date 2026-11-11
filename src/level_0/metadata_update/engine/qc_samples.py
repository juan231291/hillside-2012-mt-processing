"""Bounded, read-only sample inspection for numeric ASCII and MiniSEED 2.

Integer ASCII takes a vectorised path after full lexical validation. Unusual
numeric tokens use Decimal, so malformed rows, overflow and non-finite values
are not silently accepted. No calibration, trimming or resampling.
"""
from __future__ import annotations
from collections import Counter
from decimal import Decimal
from fractions import Fraction
import hashlib
import math
import re
import warnings
import numpy as np

CHANNELS = ('ex', 'ey', 'bx', 'by', 'bz')
BLOCK = 1024 * 1024
MAX_LINE = 4096
MAX_MSEED_BYTES = 64 * 1024**2
MAX_MSEED_SAMPLES = 16_000_000
INTEGER_LINES = re.compile(rb'(?:[ \t]*[+-]?[0-9]{1,18}[ \t]*\r?\n)+')
NUMBER = re.compile(rb'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?')


def stamp(path):
    s = path.stat()
    return (s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_ino)


def ascii_blocks(path, info=None, hasher=None):
    """Yield int64 arrays or exact Decimal lists; ignore only blank lines."""
    with path.open('rb') as f:
        first, line_number = True, 0
        while True:
            block = f.read(BLOCK)
            if not block:
                break
            if not block.endswith(b'\n'):
                block += f.readline(MAX_LINE + 1)
            if hasher:
                hasher.update(block)
            if first:
                block = block.removeprefix(b'\xef\xbb\xbf')
                first = False
            # A missing final newline is valid. Blocks end at a full line.
            normal = block if block.endswith(b'\n') else block + b'\n'
            if INTEGER_LINES.fullmatch(normal):
                values = np.fromstring(normal.decode('ascii'), dtype=np.int64, sep=' ')
                count = normal.count(b'\n')
                if len(values) != count:
                    raise ValueError('Integer parser count disagrees with validated line count')
                line_number += count
                yield values
                continue
            values = []
            lines = block.split(b'\n')
            if block.endswith(b'\n'):
                lines.pop()
            for line in lines:
                line_number += 1
                if len(line) > MAX_LINE:
                    raise ValueError(f'ASCII line {line_number} exceeds {MAX_LINE} bytes')
                token = line.strip()
                if not token:
                    if info is not None:
                        info['blank_lines_ignored'] += 1
                    continue
                if len(token) > 256 or not NUMBER.fullmatch(token):
                    raise ValueError(f'Invalid numeric sample at ASCII line {line_number}; row not skipped')
                value = Decimal(token.decode('ascii'))
                if not value.is_finite() or abs(value.adjusted()) > 400:
                    raise ValueError(f'Non-finite or unsupported numeric exponent at line {line_number}')
                values.append(value)
            if values:
                if all(v == v.to_integral_value() and -(2**63) <= v <= 2**63-1 for v in values):
                    yield np.array([int(v) for v in values], dtype=np.int64)
                else:
                    yield values


def mseed_read(path, headonly):
    from obspy import read
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        with path.open('rb') as f:
            result = read(f, format='MSEED', headonly=headonly, apply_calib=False, check_compression=False)
    if caught:
        raise ValueError('MiniSEED parser warning: ' + '; '.join(str(w.message) for w in caught))
    if not result:
        raise ValueError('MiniSEED reader returned no traces')
    return sorted(result, key=lambda t: (int(t.stats.starttime.ns), t.id))


def decoded_mseed(path, channel):
    from survey_settings import configuration
    limits=configuration().get('limits',{})
    if path.stat().st_size > limits.get('miniseed_bytes',MAX_MSEED_BYTES):
        raise ValueError('MiniSEED exceeds the 64 MiB decode limit')
    headers = mseed_read(path, True)
    if sum(int(t.stats.npts) for t in headers) > limits.get('miniseed_samples',MAX_MSEED_SAMPLES):
        raise ValueError('MiniSEED headers exceed the 16 million sample decode limit')
    traces = mseed_read(path, False)
    def key(t):
        return t.id, int(t.stats.starttime.ns), str(float(t.stats.sampling_rate)), int(t.stats.npts)
    if [key(t) for t in headers] != [key(t) for t in traces]:
        raise ValueError('Decoded trace metadata differs from header-only inspection')
    for t in traces:
        if str(t.stats.channel).casefold() != channel:
            raise ValueError('Header channel differs from filename extension; no mapping assumed')
        if t.data.ndim != 1 or t.data.dtype.kind not in 'iuf' or len(t.data) != int(t.stats.npts):
            raise ValueError('Unsupported or inconsistent MiniSEED sample array')
        if np.ma.isMaskedArray(t.data) and np.any(np.ma.getmaskarray(t.data)):
            raise ValueError('Masked MiniSEED samples; no gap filling')
        if not np.all(np.isfinite(t.data)):
            raise ValueError('Non-finite MiniSEED samples')
        if not math.isfinite(float(t.stats.sampling_rate)) or t.stats.sampling_rate <= 0 or not len(t.data):
            raise ValueError('Invalid MiniSEED rate or count')
    return traces


def inspect(path, channel):
    before = stamp(path)
    result = {'path': str(path), 'channel': channel, 'bytes': before[0], 'source_stamp': list(before),
              'format': None, 'sample_count': None, 'blank_lines_ignored': 0, 'minimum': None,
              'maximum': None, 'file_sha256': None, 'sample_sha256': None, 'sample_hash_encoding': None,
              'segments': [], 'flags': [], 'status': 'error'}
    if not before[0]:
        result.update(status='empty', format='empty', sample_count=0, flags=['empty_channel_file'],
                      file_sha256=hashlib.sha256(b'').hexdigest())
        return result
    with path.open('rb') as f:
        preview = f.read(64)
    integer_hash = hashlib.sha256()
    all_int64, count, low, high = True, 0, None, None
    def consume(values):
        nonlocal all_int64, count, low, high
        count += len(values)
        if isinstance(values, np.ndarray):
            a, b = values.min().item(), values.max().item()
            if values.dtype.kind in 'iu' and -(2**63) <= a and b <= 2**63-1:
                integer_hash.update(values.astype('<i8', copy=False).tobytes())
            else:
                all_int64 = False
        else:
            a, b = min(values), max(values)
            all_int64 = False
        # Exact decimal comparison avoids rounding large integer extrema.
        a = Decimal.from_float(a) if isinstance(a, float) else Decimal(a)
        b = Decimal.from_float(b) if isinstance(b, float) else Decimal(b)
        low = a if low is None else min(low, a)
        high = b if high is None else max(high, b)
    try:
        if re.fullmatch(rb'[0-9 ]{6}[DRQM] ', preview[:8]):
            result['format'] = 'MiniSEED2'
            traces = decoded_mseed(path, channel)
            h = hashlib.sha256()
            with path.open('rb') as f:
                for block in iter(lambda: f.read(BLOCK), b''):
                    h.update(block)
            result['file_sha256'] = h.hexdigest()
            previous_end = None
            for trace in traces:
                rate = Fraction(str(float(trace.stats.sampling_rate)))
                start = Fraction(int(trace.stats.starttime.ns))
                step = Fraction(10**9) / rate
                n = len(trace.data)
                end = start + n * step
                if previous_end is not None and start != previous_end:
                    result['flags'].append('within_file_gap' if start > previous_end else 'within_file_overlap')
                result['segments'].append({'sample_offset': count, 'samples': n,
                    'start_ns': str(start), 'step_ns': str(step), 'end_exclusive_ns': str(end),
                    'rate_hz': str(rate), 'rate_basis': 'decoded_miniseed_header',
                    'seed_id': trace.id, 'time_basis': 'miniseed_header_utc'})
                consume(trace.data)
                previous_end = end
            if len({s['rate_hz'] for s in result['segments']}) > 1:
                result['flags'].append('within_file_rate_change')
            # SEED identifiers are retained as evidence. An ID change alone is
            # not a timing problem when channel/rate/time alignment is valid.
        elif preview.startswith(b'MS\x03'):
            raise ValueError('MiniSEED 3 requires a different reader')
        else:
            result['format'] = 'numeric_ASCII'
            h = hashlib.sha256()
            for values in ascii_blocks(path, result, h):
                consume(values)
            result['file_sha256'] = h.hexdigest()
        if not count:
            raise ValueError('No numeric samples')
        if stamp(path) != before:
            raise ValueError('Source changed during sample inspection')
        result.update(status='ok', sample_count=count, minimum=str(low), maximum=str(high))
        if all_int64:
            result.update(sample_sha256=integer_hash.hexdigest(), sample_hash_encoding='signed_int64_little_endian_values_v1')
        if low == high:
            result['flags'].append('constant_channel')
    except (OSError, ValueError, ArithmeticError) as exc:
        result.update(status='error', error=str(exc), samples_read_before_error=count, sample_count=None)
        result['flags'].append('sample_read_or_validation_error')
    result['flags'] = sorted(set(result['flags']))
    return result


def canonical(value):
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float):
        return Decimal.from_float(value)
    return Decimal(value)


def slice_values(path, info, start, count):
    if list(stamp(path)) != info['source_stamp']:
        raise ValueError('Source changed since QC scan')
    stop, position, chunks = start + count, 0, []
    if start < 0 or stop > info['sample_count']:
        raise ValueError('Overlap sample indices outside file')
    if info['format'] == 'MiniSEED2':
        blocks = (t.data for t in decoded_mseed(path, info['channel']))
    else:
        blocks = ascii_blocks(path)
    try:
        for block in blocks:
            end = position + len(block)
            a, b = max(start, position), min(stop, end)
            if b > a:
                chunks.append(block[a-position:b-position])
            position = end
            if position >= stop:
                break
    finally:
        blocks.close()
    if sum(len(c) for c in chunks) != count or list(stamp(path)) != info['source_stamp']:
        raise ValueError('Incomplete overlap read or changed source')
    return chunks


def compare_slice(left, right, left_index, right_index, count, max_samples):
    if count > max_samples:
        return {'status': 'comparison_limit', 'samples_per_channel': count}
    try:
        a = slice_values(left['_path'], left, left_index, count)
        b = slice_values(right['_path'], right, right_index, count)
        if left['sample_hash_encoding'] == right['sample_hash_encoding'] == 'signed_int64_little_endian_values_v1':
            aa, bb = np.concatenate(a).astype(np.int64), np.concatenate(b).astype(np.int64)
            # All integer values scanned into supported int64; no float coercion.
            difference = np.flatnonzero(aa != bb)
            first = int(difference[0]) if len(difference) else None
        else:
            ai = (canonical(v) for chunk in a for v in chunk)
            bi = (canonical(v) for chunk in b for v in chunk)
            first = next((i for i, (x, y) in enumerate(zip(ai, bi)) if x != y), None)
        return {'status': 'identical_samples' if first is None else 'different_samples',
                'samples_per_channel': count, 'first_difference_index_in_overlap': first,
                'comparison': 'exact_numeric_values_no_tolerance'}
    except (OSError, ValueError, ArithmeticError) as exc:
        return {'status': 'comparison_error', 'error': str(exc), 'samples_per_channel': count}
