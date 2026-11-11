"""Compile explicit decisions and evidence-backed selection settings."""
from collections import Counter
from copy import deepcopy
from fractions import Fraction
import json
from pathlib import Path
from survey_settings import fingerprint,checked_group_rule


def read_records(path):
    with Path(path).open(encoding='utf-8') as f:return [json.loads(s) for s in f if s.strip()]


def selection_policy(qc,decisions,rules,stations):
    if rules.get('schema')!='mtl0.selection_rules/1':raise ValueError('Unknown selection rules schema')
    if set(rules)-{'schema','reviewer','default','stations','exclude_stations','confirmed_overlap_pairs','reviewed_rate_sequences'}:
        raise ValueError('Unknown selection rules key')
    allowed={'output_native_rate_hz','accept_logged_ascii_rate_estimates','startup_policy','exclude_startup_groups',
             'exclude_timing_conflict_groups','allow_partial_groups','exclude_reviewed_groups',
             'acknowledge_group_flags','reviewed_fingerprints'}
    for s in [rules['default'],*rules.get('stations',{}).values()]:
        if set(s)-allowed:raise ValueError('Unknown station selection setting')
        rate=s.get('output_native_rate_hz','any')
        if rate!='any' and Fraction(str(rate))<=0:raise ValueError('Selected rate must be positive')
        if type(s.get('accept_logged_ascii_rate_estimates',False)) is not bool:raise ValueError('ASCII estimate acceptance must be boolean')
        startup=s.get('startup_policy',{})
        if set(startup)-{'enabled','rates_hz'}:raise ValueError('Unknown startup policy setting')
        if type(startup.get('enabled',False)) is not bool:raise ValueError('Startup enabled must be boolean')
        if any(Fraction(str(r))<=0 for r in startup.get('rates_hz',[])):raise ValueError('Startup rates must be positive')
    groups={r['group_id']:r for r in read_records(qc/'qc_evidence.jsonl') if r['record_type']=='recording_qc'}
    from reviewed_rates import project, workbook_reference, canonical_sha
    sequences=deepcopy(rules.get('reviewed_rate_sequences',[]))
    reference=workbook_reference() if sequences else None
    rate_views=project(groups,sequences,reference)
    default=rules['default']
    result={'schema':'mtl0.run_selection_policy/1','stations':{},'confirmed_overlap_pairs':rules.get('confirmed_overlap_pairs',[]),
            'excluded_stations':rules.get('exclude_stations',{}),'rule_application':[]}
    if sequences:
        result.update(reviewed_rate_sequences=sequences,reviewed_rate_workbook=reference)
    extra=set(rules.get('stations',{}))-set(stations)
    if extra:raise ValueError('Selection rules name unknown stations: '+str(sorted(extra)))
    if set(result['excluded_stations'])-set(stations):raise ValueError('Unknown excluded station')
    for station in stations:
        s=deepcopy({**default,**rules.get('stations',{}).get(station,{})})
        settings={k:s.get(k,{}) for k in ('exclude_startup_groups','exclude_timing_conflict_groups','allow_partial_groups','exclude_reviewed_groups','acknowledge_group_flags')}
        settings.update(output_native_rate_hz=str(s.get('output_native_rate_hz','any')),
                        accept_logged_ascii_rate_estimates=s.get('accept_logged_ascii_rate_estimates',False))
        gg={gid:g for gid,g in groups.items() if g['station']==station}
        proofs=s.get('reviewed_fingerprints',{})
        for category in ('exclude_startup_groups','exclude_timing_conflict_groups','allow_partial_groups','exclude_reviewed_groups','acknowledge_group_flags'):
            for gid,rule in settings[category].items():
                if gid not in gg:raise ValueError('Reviewed rule group not present in current QC: '+gid)
                expected=rule.get('qc_fingerprint') if isinstance(rule,dict) else None
                if (expected or proofs.get(gid))!=fingerprint(gg[gid]):
                    raise ValueError('Reviewed rule content differs or has no fingerprint: '+gid)
                if isinstance(rule,dict) and rule.get('qc_record_sha256'):
                    if canonical_sha(gg[gid])!=rule['qc_record_sha256']:
                        raise ValueError('Reviewed rule QC evidence differs: '+gid)
        if station in result['excluded_stations']:
            reason=result['excluded_stations'][station]
            if not isinstance(reason,str) or not reason.strip():raise ValueError('Excluded station needs a reason')
            settings['exclude_reviewed_groups']={gid:{'reason':reason,'reviewer':rules.get('reviewer',''),
                                                       'qc_fingerprint':fingerprint(g)} for gid,g in gg.items()}
        startup=s.get('startup_policy',{})
        if startup.get('enabled'):
            target=settings['output_native_rate_hz']
            if target=='any':raise ValueError('Startup exclusion needs a specific selected production rate')
            # Exclude lower-rate startup groups only before the first usable
            # complete production-rate group. Clock-conflicted groups stay held.
            from importlib import import_module
            six=import_module('06_select_runs')
            native=[]
            for g in gg.values():
                c=six.classify(rate_views[g['group_id']],{**settings,'exclude_startup_groups':{}})
                if c['action']=='include':native.append(Fraction(c['start_ns']))
            first=min(native) if native else None
            allowed={Fraction(str(v)) for v in startup.get('rates_hz',[])}
            for gid,g in gg.items():
                if gid in settings['exclude_startup_groups'] or g['clock_uncertain']:continue
                rates={Fraction(v) for v in g['rates_hz']}
                if first is None or len(rates)!=1 or not rates<=allowed:continue
                if not settings['accept_logged_ascii_rate_estimates'] and g['has_inferred_ascii_rate']:continue
                try:
                    layouts=[six.layout(g['channels'][c]) for c in six.CHANNELS]
                    if len(set(layouts))!=1 or layouts[0][1]>first:continue
                    if set(g['flags'])-six.CONTEXT_FLAGS-{'repeated_five_channel_sample_sequence'}:continue
                except (ValueError,KeyError):continue
                settings['exclude_startup_groups'][gid]={'expected_rate_hz':str(next(iter(rates))),
                    'reason':'Configured startup policy: aligned lower-rate recording ends before the first usable production-rate recording. Original retained.',
                    'qc_fingerprint':fingerprint(g)}
                result['rule_application'].append({'group_id':gid,'rule':'startup_before_first_production','qc_fingerprint':fingerprint(g)})
        result['stations'][station]=settings
    return result


def apply_ownership(folder,rule_document,logs):
    """Modify generated decisions only, retaining the automatic assessment."""
    import csv
    from workflow_common import sha
    if rule_document.get('schema')!='mtl0.ownership_rules/1':raise ValueError('Unknown ownership rules schema')
    if set(rule_document)-{'schema','reviewer','log_evidence_sha256','groups'}:raise ValueError('Unknown ownership rules key')
    records=read_records(folder/'decision_evidence.jsonl')
    manifest=records[0]
    rules=rule_document.get('groups',{})
    if not rules:return
    if rule_document.get('log_evidence_sha256')!=sha(logs/'log_evidence.jsonl'):
        raise ValueError('Ownership review refers to different recorder evidence')
    if not rule_document.get('reviewer'):raise ValueError('Ownership review needs reviewer attribution')
    details={r['group_id']:r for r in records if r['record_type']=='station_decision'}
    stations={r['station'] for r in records if r['record_type']=='metadata_reference'}
    if set(rules)-set(details):raise ValueError('Ownership review names unknown group')
    for gid,rule in rules.items():
        if set(rule)-{'decision','assigned_station','reason','metadata_comment'}:raise ValueError('Unknown ownership override key')
        if rule.get('decision') not in ('retain_for_qc','exclude_reviewed') or not rule.get('reason'):
            raise ValueError('Manual ownership decisions are retain_for_qc or exclude_reviewed, with a reason')
        if 'assigned_station' not in rule:raise ValueError('Reviewed rule must explicitly state assigned_station, or null for an excluded unknown owner')
        owner=rule['assigned_station']
        if owner not in stations:
            if rule['decision']!='exclude_reviewed' or owner not in (None,''):
                raise ValueError('Unknown reviewed station')
            # Exclusion is a disposition, not evidence of station identity.
            # Keep CSV and JSON decision rows identical using an empty string;
            # human_review retains the explicit null supplied by the reviewer.
            owner=''
        item=details[gid];row=item['decision']
        item['automatic_decision_before_review']=row.copy()
        item['human_review']={'reviewer':rule_document['reviewer'],**rule}
        row.update(assigned_station=owner,decision=rule['decision'],reason=rule['reason'],
                   retained_group_id=gid if rule['decision']=='retain_for_qc' else '',metadata_comment=rule.get('metadata_comment',''))
    rows=[r['decision'] for r in details.values()]
    byid={r['group_id']:r for r in rows}
    for r in rows:
        if r['decision']=='exclude_duplicate':
            owner=byid[r['retained_group_id']]
            if owner['decision']!='retain_for_qc' or owner['assigned_station']!=r['assigned_station']:
                raise ValueError('Reviewed override would break an existing retained-copy link')
    path=folder/'decision.csv'
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    manifest.update(counts=dict(Counter(r['decision'] for r in rows)),decision_csv_sha256=sha(path),
                    all_station_assignments_resolved=not any(r['decision']=='unresolved' for r in rows),human_review=rule_document)
    with (folder/'decision_evidence.jsonl').open('w',encoding='utf-8',newline='\n') as f:
        for r in records:f.write(json.dumps(r,allow_nan=False,separators=(',',':'))+'\n')
    print('Reviewed ownership decisions applied. Final counts:',manifest['counts'],flush=True)
