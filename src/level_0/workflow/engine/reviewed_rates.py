"""Explicit workbook/boundary rate reviews for otherwise untimed numeric ASCII.

This adapter makes separate derived views; the original QC is immutable. It is
not a general fallback to a workbook rate or an automatic clock correction.
"""
from copy import deepcopy
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import json
from pathlib import Path
from openpyxl import load_workbook

RATE_BASIS = 'reviewed_workbook_and_filename_boundary_consistency'
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

def require(condition, message):
    if not condition:
        raise ValueError(message)

def canonical_sha(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()

def sha(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()

def ns(stamp):
    delta = datetime.fromisoformat(stamp.replace('Z', '+00:00')) - EPOCH
    return (delta.days * 86400 + delta.seconds) * 10**9 + delta.microseconds * 1000

def validate_and_project(groups, decision, workbook, filename_offset="+00:00"):
    """Return separately derived rate/timing views; preserve original QC objects."""
    require(decision['schema'] == 'mtl0.accepted_rate_review/1', 'Unknown review schema')
    require(decision['decision'] == 'accept_rate_for_listed_main_groups', 'Unexpected decision')
    require(decision.get('status') == 'approved_by_user_not_applied_on_gadi', 'Rate review is not an accepted decision')
    require(decision.get('reviewer') and decision.get('reason') and decision.get('assumptions'), 'Rate review lacks attribution, reason or assumptions')
    require(decision.get('rate_basis') == RATE_BASIS and decision.get('recorder_confirmed') is False, 'Unexpected rate evidence basis')
    require(decision['station'] and decision['decision_id'], 'Rate review lacks identity')
    source = decision['evidence']['workbook']
    require(sha(workbook) == source['sha256'], 'Workbook content changed')
    wb = load_workbook(workbook, read_only=True, data_only=True)
    try:
        require(Fraction(str(wb[source['sheet']][source['rate_cell']].value)) ==
                Fraction(decision['rate_hz']), 'Workbook rate differs')
    finally:
        wb.close()
    from workflow_common import offset
    filename_zone = offset(filename_offset)
    rate = Fraction(decision['rate_hz'])
    step = Fraction(10**9) / rate
    require(rate > 0 and step.denominator == 1, 'Unsupported sample-time grid')
    entries = decision['groups']
    require(len(entries) >= 2, 'This review basis needs a sequence with at least one checked boundary')
    require(len({r['group_id'] for r in entries}) == len(entries), 'Repeated reviewed group')
    require(len(entries) == decision['coverage']['recording_groups'], 'Review coverage changed')
    projected = {}
    previous_end = None
    total = 0
    for entry in entries:
        gid = entry['group_id']
        g = groups[gid]
        require(canonical_sha(g) == entry['qc_record_sha256'], 'QC evidence changed: ' + gid)
        require(g['station'] == decision['station'] and g['decision'] == 'retain_for_qc',
                'Station assignment changed')
        require(not g['clock_uncertain'] and not g['rates_hz'] and
                g['logged_max_seconds_consensus'] is None, 'New rate/clock/log evidence needs review')
        require(set(g['flags']) == {'ascii_rate_estimate_needs_review', 'ascii_rate_not_estimated',
                                    'logged_duration_missing_or_conflicting'}, 'Additional QC findings')
        require(g['prior_review_flags'] == ['no_matching_log_write'], 'Prior evidence changed')
        require(set(g['channels']) == {'ex', 'ey', 'bx', 'by', 'bz'}, 'Unexpected channel set')
        start = ns(entry['start_utc_assumed'])
        filename_start = datetime.strptime(g['recording_group'][-12:], '%y%m%d%H%M%S').replace(tzinfo=filename_zone)
        require(start == ns(filename_start.isoformat()), 'Review would alter filename-derived start')
        count = entry['samples_per_channel']
        require(type(count) is int and count > 0, 'Invalid sample count')
        end = start + count * step
        require(str(end) == entry['end_exclusive_ns'], 'Timing/count/rate arithmetic differs')
        if previous_end is not None:
            require(start == previous_end, 'Reviewed sequence is not continuous')
        view = deepcopy(g)
        for channel, info in g['channels'].items():
            require(info['status'] == 'ok' and not info['flags'] and not info['segments'],
                    'Channel has new flags or existing timing')
            require(info['sample_count'] == count and info['format'] == 'numeric_ASCII',
                    'Channel count or representation differs')
            require(info['file_sha256'] == entry['source_files'][channel]['raw_file_sha256'] and
                    info['sample_sha256'] == entry['source_files'][channel]['sample_sha256'],
                    'Source content changed')
            view['channels'][channel]['segments'] = [{
                'sample_offset': 0, 'samples': count, 'start_ns': str(start), 'step_ns': str(step),
                'end_exclusive_ns': str(end), 'rate_hz': str(rate), 'rate_basis': RATE_BASIS,
                'time_basis': 'filename_start_with_recorded_utc_offset_assumption',
                'timing_uncertain': False,
            }]
        # The original QC record and its flags are retained separately. This view
        # answers only the what-if question for the reviewed rate interpretation.
        view['flags'] = []
        view['rates_hz'] = [str(rate)]
        view['review_required'] = False
        view['rate_review'] = {'decision_id': decision['decision_id'],
                               'original_qc_record_sha256': entry['qc_record_sha256'],
                               'original_qc_flags': list(g['flags']),
                               'recorder_confirmed': False,
                               'assumptions': list(decision['assumptions'])}
        projected[gid] = view
        previous_end = end
        total += count
    require(total == decision['coverage']['samples_per_channel'], 'Reviewed total differs')
    require(ns(entries[0]['start_utc_assumed']) == ns(decision['coverage']['start_utc_assumed']) and
            previous_end == ns(decision['coverage']['end_exclusive_utc_assumed']), 'Reviewed coverage differs')
    require(not set(projected) & {r['group_id'] for r in decision['initial_groups_still_held']},
            'Unknown-rate initial group was selected')
    return projected


def workbook_reference():
    import os
    from survey_settings import configuration
    c = configuration()
    require(c.get('metadata_workbook') and os.getenv('MTL0_CONFIG'), 'Reviewed rates need the original configured workbook')
    path = Path(c['metadata_workbook'])
    path = (path if path.is_absolute() else Path(os.environ['MTL0_CONFIG']).parent/path).resolve()
    return {'path':str(path), 'sha256':sha(path), 'filename_utc_offset':c['filename_utc_offset']}


def project(groups, sequences, reference):
    if not sequences:
        return groups
    require(isinstance(sequences,list) and reference, 'Reviewed rate sequences need a workbook reference')
    workbook=Path(reference['path'])
    require(sha(workbook)==reference['sha256'], 'Reviewed rate workbook changed')
    seen=set(); derived={}; decision_ids=set()
    for sequence in sequences:
        require(sequence['decision_id'] not in decision_ids,'Repeated rate review identity')
        decision_ids.add(sequence['decision_id'])
        require(sequence['evidence']['workbook']['sha256']==reference['sha256'], 'Rate review refers to another workbook')
        ids={r['group_id'] for r in sequence['groups']}
        require(not ids & seen, 'A recording has multiple reviewed rate assignments')
        seen.update(ids)
        derived.update(validate_and_project(groups,sequence,workbook,reference['filename_utc_offset']))
    require(sha(workbook)==reference['sha256'], 'Reviewed rate workbook changed during validation')
    return {**groups,**derived}
