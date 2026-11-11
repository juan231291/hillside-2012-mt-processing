"""Explicit, source-pinned metadata edits; no changes to run timing or samples."""
import os
from pathlib import Path
import re


def attach(context, original):
    from survey_settings import configuration
    from station_metadata_json import strict_load, sha, require
    value = configuration().get('metadata_rules')
    context['metadata_review'] = None
    if value is None:
        return
    path = Path(value)
    if not path.is_absolute():
        path = Path(os.environ['MTL0_CONFIG']).resolve().parent / path
    path = path.resolve(strict=True)
    digest = sha(path)
    rules = strict_load(path)
    require(set(rules) == {'schema', 'reviewer', 'original_workbook_sha256', 'edits'},
            'Unexpected metadata review keys')
    require(rules['schema'] == 'mtl0.metadata_rules/1', 'Unknown metadata review schema')
    require(isinstance(rules['reviewer'], str) and rules['reviewer'].strip(), 'Metadata reviewer required')
    require(rules['original_workbook_sha256'] == sha(original), 'Metadata review source workbook differs')
    require(isinstance(rules['edits'], list), 'Metadata edits must be a list')
    seen = set()
    for edit in rules['edits']:
        require(isinstance(edit, dict) and set(edit) == {'station', 'field', 'expected', 'value', 'reason'},
                'Unexpected metadata edit keys')
        require(all(isinstance(edit[k], str) and edit[k].strip() for k in ('station', 'field', 'reason')),
                'Metadata edit station, field and reason required')
        field = edit['field']
        require(field in ('station.acquired_by.comments', 'run.metadata_by.author',
                      'survey.summary', 'station.comments', 'magnetic.sensor.type') or
                re.fullmatch(r'(electric\.measurement_azimuth_e[xy]|magnetic\.measurement_azimuth_b[xyz])', field),
                'Unsupported reviewed field (run timing/identity are export-derived): ' + field)
        require(all(v is None or type(v) in (str, int, float, bool) for v in (edit['expected'], edit['value'])),
                'Metadata edit values must be scalar')
        if field in ('station.acquired_by.comments', 'run.metadata_by.author',
                      'survey.summary', 'station.comments', 'magnetic.sensor.type'):
            require(edit['value'] is None or isinstance(edit['value'], str), 'Comment/author must be text or null')
        else:
            v = edit['value']
            require(v in (None, '-') or type(v) in (int, float) and 0 <= v < 360,
                    'Azimuth must be unknown or a number in [0, 360)')
        key = (edit['station'], field)
        require(key not in seen, 'Repeated metadata edit: ' + str(key))
        seen.add(key)
    require(sha(path) == digest, 'Metadata rules changed while being read')
    context['metadata_review'] = rules
    context['input_sha256'][str(path)] = digest


def plan(context, source_rows, indices):
    """Validate all old values before creating any output workbook."""
    from station_metadata_json import require
    rules = context.get('metadata_review')
    result = {}
    if rules is None:
        return result
    for edit in rules['edits']:
        station, field = edit['station'], edit['field']
        require(station in source_rows, 'Reviewed station has no exported metadata row: ' + station)
        require(field in indices and len(indices[field]) == 1, 'Reviewed field is missing or repeated: ' + field)
        old = source_rows[station][indices[field][0]].value
        require(old == edit['expected'] and type(old) == type(edit['expected']),
                'Reviewed source value differs: ' + station + '/' + field)
        result.setdefault(station, []).append(edit)
    return result
