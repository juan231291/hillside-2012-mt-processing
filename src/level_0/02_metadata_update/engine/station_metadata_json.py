"""Lossless MT survey interchange JSON. No mt_metadata defaults are introduced.

This is the documented input format for the next MT survey MTH5 writer, not a
native Experiment.to_json() document. Unknown numeric fields remain JSON null.
"""
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import re

SCHEMA = 'mtl0.level0.station_metadata/2'
CHANNELS = ('ex', 'ey', 'bx', 'by', 'bz')
LIST_FIELDS = {'station.channels_recorded', 'station.run_list',
               'run.channels_recorded_electric', 'run.channels_recorded_magnetic'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def strict_load(path):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'Duplicate JSON key: ' + key)
            result[key] = value
        return result
    def nonfinite(value):
        raise ValueError('Nonstandard JSON number: ' + value)
    return json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=pairs, parse_constant=nonfinite)


def normalized(field, value):
    if value is None or isinstance(value, str) and value.strip() in ('', '-'):
        return None
    if isinstance(value, float):
        require(math.isfinite(value), 'Nonfinite Excel number in ' + field)
    if field in LIST_FIELDS:
        result = json.loads(value) if isinstance(value, str) else value
        require(isinstance(result, list) and all(isinstance(v, str) and v for v in result)
                and len(set(result)) == len(result), 'Invalid or repeated list entries: ' + field)
        return result
    if re.sub(r'_(ex|ey|bx|by|bz)(?=\.|$)', '', field).endswith('.id'):
        require(not isinstance(value, bool), 'Boolean instrument/station ID')
        return str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)
    if field.endswith(('.start_date', '.end_date')):
        parsed = datetime.fromisoformat(str(value))
        require(parsed.time().isoformat() == '00:00:00', 'Date field includes a non-midnight time: ' + field)
        return parsed.date().isoformat()
    return value


def flat_fields(headers, values):
    require(len(headers) == len(values), 'Row width differs from header width')
    fields = {}
    for field, raw in zip(headers, values):
        require(isinstance(field, str) and field, 'Missing metadata header')
        value = normalized(field, raw)
        if field in fields:
            require(field == 'station.id' and fields[field] == value, 'Conflicting or unexpected duplicate header: ' + field)
        fields[field] = value
    return fields


def nested(fields):
    result = {}
    for path, value in fields.items():
        parts = path.split('.')
        target = result
        for part in parts[:-1]:
            require(part and (part not in target or isinstance(target[part], dict)), 'Conflicting metadata path: ' + path)
            target = target.setdefault(part, {})
        require(parts[-1] not in target, 'Conflicting metadata field: ' + path)
        target[parts[-1]] = value
    return result


def section(fields, name):
    prefix = name + '.'
    return nested({k[len(prefix):]: v for k, v in fields.items() if k.startswith(prefix)})


def channel_fields(fields, component):
    family = 'electric' if component.startswith('e') else 'magnetic'
    generic, specific = {}, {}
    for field, value in fields.items():
        if not field.startswith(family + '.'):
            continue
        name = field[len(family) + 1:]
        labels = re.findall(r'_(ex|ey|bx|by|bz)(?=\.|$)', name)
        if labels:
            if labels != [component]:
                continue
            specific[re.sub('_' + component + r'(?=\.|$)', '', name)] = value
        else:
            generic[name] = value
    # Explicit per-channel values take precedence even when they are unknown.
    result = nested({**generic, **specific})
    result['component'] = component
    result['type'] = family
    if 'measurement_tilt' not in result:
        result['measurement_tilt'] = None
    return result


def stamp_ns(value):
    match = re.fullmatch(r'(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?Z', value)
    require(match is not None, 'Expected ISO 8601 UTC timestamp ending in Z: ' + str(value))
    stamp = datetime.strptime(match[1], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)
    delta = stamp - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 10**9 + int((match[2] or '').ljust(9, '0'))


def assemble(prepared):
    headers = prepared['headers']
    require(len(prepared['rows']) == len(prepared['run_records']) > 0, 'Metadata row/run evidence count differs')
    records = {}
    exports = {(r['station'], r['run_id']): r for r in prepared['context']['runs']}
    original_stations = {r['station']: r for r in prepared['station_records']}
    all_survey = None
    for values, proof in zip(prepared['rows'], prepared['run_records']):
        fields = flat_fields(headers, values)
        require({k.split('.')[0] for k in fields} <= {'survey', 'station', 'run', 'electric', 'magnetic', 'battery'},
                'Unmapped metadata field family')
        survey, station, run = (section(fields, prefix) for prefix in ('survey', 'station', 'run'))
        sid, rid = station['id'], run['id']
        require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', sid) is not None, 'Unsafe station filename')
        require((sid, rid) == (proof['station'], proof['run_id']), 'Metadata identity differs from exported run')
        # A survey can cite different source datasets for different stations.
        # Each station JSON/MTH5 file retains its own complete dataset citation.
        # Shared survey descriptors must still agree across the batch.
        shared_survey = {k: v for k, v in survey.items() if k != 'citation_dataset'}
        if all_survey is None:
            all_survey = shared_survey
        require(shared_survey == all_survey,
                'Shared survey metadata differs at station ' + sid + ', run ' + rid +
                '; fields: ' + ', '.join(sorted(k for k in set(shared_survey) | set(all_survey)
                    if shared_survey.get(k) != all_survey.get(k))))
        if sid not in records:
            records[sid] = {'schema': SCHEMA, 'data_level': 0, 'station_id': sid,
                'survey': survey, 'station': station, 'runs': [],
                'source': {**prepared['source'], 'headers': headers, 'rows': []},
                'selection_comments': original_stations[sid]['added_station_comments'],
                'processing': {'calibration_applied': False, 'unit_conversion_applied': False,
                               'rotation_applied': False, 'polarity_correction_applied': False,
                               'resampling_applied': False},
                'conventions': {'timestamp_reference': 'UTC assumed; source timestamps were not corrected',
                    'metadata_end_time': 'last sample; exclusive boundaries are stored in sample_coverage',
                    'channel_components': 'Original ex, ey, bx, by, bz names; no B-to-H conversion',
                    'missing_values': 'Empty cells and lone dash markers become null; measured zero stays zero',
                    'field_measurements': 'Deployment measurements copied from Excel; repeated values are not new measurements for each run',
                    'calibration': 'No response file is added or applied in this conversion'}}
        record = records[sid]
        require(record['survey'] == survey,
                'Survey metadata (including dataset citation) differs between run rows: ' + sid)
        require(record['station'] == station, 'Station-level metadata differs between run rows: ' + sid)
        require(not any(r['id'] == rid for r in record['runs']), 'Repeated run ID: ' + sid + '/' + rid)
        export = exports[sid, rid]
        order = proof['channels']
        require(run['channels_recorded_electric'] == [c for c in order if c.startswith('e')] and
                run['channels_recorded_magnetic'] == [c for c in order if c.startswith('b')],
                'Workbook channels differ from exported columns')
        require(not {'channels', 'sample_coverage', 'ascii', 'deployment_measurements'} & set(run),
                'Run metadata conflicts with reserved interchange fields')
        run['channels'] = {c: channel_fields(fields, c) for c in order}
        run['sample_coverage'] = {k: proof[k] for k in ('samples_per_channel', 'duration_seconds',
            'start_utc_assumed', 'last_sample_utc_assumed', 'end_exclusive_utc_assumed')}
        run['sample_coverage']['duration_seconds'] = int(Fraction(proof['duration_seconds'])) if Fraction(proof['duration_seconds']).denominator == 1 else float(Fraction(proof['duration_seconds']))
        run['sample_coverage']['duration_seconds_exact'] = proof['duration_seconds']
        run['sample_coverage']['rate_evidence'] = export['plan']['rate_bases']
        run['ascii'] = {'relative_path': export['relative_path'], 'sha256': export['ascii_sha256'],
                        'column_order': order, 'sample_sha256_by_channel': export['sample_sha256_by_channel'],
                        'sample_hash_encoding': export['sample_hash_encoding']}
        run['deployment_measurements'] = {'battery': section(fields, 'battery')}
        record['runs'].append(run)
        record['source']['rows'].append({'row': proof['metadata_row'], 'run_id': rid, 'values': values})
    require(len({sid.casefold() for sid in records}) == len(records), 'Station identifiers differ only by case')
    for record in records.values():
        validate_station(record)
    return records


def validate_station(record):
    require(record['schema'] == SCHEMA and record['data_level'] == 0, 'Unsupported station metadata schema/level')
    sid = record['station_id']
    require(record['station']['id'] == sid, 'Station identifier differs')
    require(all(v is False for v in record['processing'].values()), 'Unexpected Level 0 processing')
    runs = record['runs']
    require(runs and [r['id'] for r in runs] == record['station']['run_list'], 'Station run list differs')
    require(len({r['id'] for r in runs}) == len(runs), 'Repeated run IDs')
    require([c for c in CHANNELS if any(c in r['channels'] for r in runs)] == record['station']['channels_recorded'],
            'Station channel union differs')
    starts, ends, previous_boundary = [], [], None
    for run in runs:
        channels, coverage = run['channels'], run['sample_coverage']
        require(channels and set(channels) <= set(CHANNELS), 'Unknown/empty channel set')
        require(list(channels) == run['ascii']['column_order'], 'ASCII column order differs')
        rate = run['sample_rate']
        require(isinstance(rate, (int, float)) and not isinstance(rate, bool) and math.isfinite(rate) and rate > 0,
                'Invalid sample rate')
        rate = Fraction(str(rate))
        count = coverage['samples_per_channel']
        require(isinstance(count, int) and not isinstance(count, bool) and count > 0, 'Invalid sample count')
        start, end = (stamp_ns(run['time_period'][k]) for k in ('start', 'end'))
        boundary = stamp_ns(coverage['end_exclusive_utc_assumed'])
        require(end - start == (count - 1) * Fraction(10**9, 1) / rate and
                boundary - start == count * Fraction(10**9, 1) / rate, 'Timing/count/rate mismatch')
        require(coverage['start_utc_assumed'] == run['time_period']['start'] and
                coverage['last_sample_utc_assumed'] == run['time_period']['end'], 'Sample coverage differs')
        require(Fraction(coverage['duration_seconds_exact']) == count / rate and
                coverage['duration_seconds'] == float(count / rate), 'Duration differs')
        require(previous_boundary is None or start >= previous_boundary, 'Run times overlap or are unsorted')
        previous_boundary = boundary
        starts.append(start); ends.append(end)
        require(run['channels_recorded_electric'] == [c for c in channels if c.startswith('e')] and
                run['channels_recorded_magnetic'] == [c for c in channels if c.startswith('b')], 'Run channel lists differ')
        require(run['ascii']['relative_path'] == f'{sid}/{sid}_{run["id"]}.txt', 'Unexpected relative ASCII path')
        require(re.fullmatch('[0-9a-f]{64}', run['ascii']['sha256']) is not None and
                set(run['ascii']['sample_sha256_by_channel']) == set(channels) and
                all(re.fullmatch('[0-9a-f]{64}', h) for h in run['ascii']['sample_sha256_by_channel'].values()) and
                run['ascii']['sample_hash_encoding'] == 'signed_int64_little_endian_values_v1',
                'ASCII verification hashes absent or invalid')
        for c, metadata in channels.items():
            require(metadata['component'] == c and metadata['type'] == ('electric' if c.startswith('e') else 'magnetic'),
                    'Channel type or component differs')
            require(metadata['time_period'] == run['time_period'] and metadata['sample_rate'] == run['sample_rate'],
                    'Channel timing/rate differs from run')
            require(metadata['units'] == 'ADC counts', 'Level 0 expects original ADC counts')
            angle = metadata.get('measurement_azimuth')
            require(angle is None or isinstance(angle, (int, float)) and math.isfinite(angle) and 0 <= angle <= 360,
                    'Invalid acquisition azimuth')
    require(stamp_ns(record['station']['time_period']['start']) == min(starts) and
            stamp_ns(record['station']['time_period']['end']) == max(ends), 'Station coverage differs from runs')
    # Strict JSON rejects NaN and infinity instead of publishing nonstandard numeric tokens.
    json.dumps(record, allow_nan=False)
    return record


def write_station(path, record):
    validate_station(record)
    with Path(path).open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False, indent=2) + '\n')
    require(strict_load(path) == record, 'JSON readback differs')
    validate_station(strict_load(path))


def read_station(path):
    """Read and validate this interchange format for downstream tools."""
    return validate_station(strict_load(path))
