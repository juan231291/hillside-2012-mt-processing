"""Validate report provenance for metadata preparation without reading samples."""
from fractions import Fraction
import importlib.util
from pathlib import Path, PurePosixPath
import re
import sys


def load_module(path, name):
    sys.path.insert(0, str(path.parent))
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_inputs(original, export, selection, qc, decisions, policy, step06_code, step07_code):
    seven=load_module(step07_code,'step07_for_metadata')
    from ascii_export import require,sha,iso_ns
    six=seven.load_step06(step06_code)
    selections,plans,sm,hashes,selection_hash,summary=seven.load_plan(selection,qc,decisions,policy,step06_code)
    evidence_path=export/'export_evidence.jsonl'
    records=six.read_jsonl(evidence_path)
    m=next(r for r in records if r['id']=='manifest:ascii_export')
    require(m['schema']=='mtl0.ascii_export/1' and m['complete'] is True and m['all_output_values_verified'] is True,
            'Completed and verified Step 07 export required')
    require(m['selection_evidence_sha256']==selection_hash,'Export used a different selection')
    require(six.portable_hashes(hashes)==six.portable_hashes(m['input_sha256']), 'Export input chain differs')
    import qc_samples
    code_files={
        '07_concatenate.py':step07_code,'ascii_export.py':step07_code.with_name('ascii_export.py'),
        'qc_samples.py':Path(qc_samples.__file__),'06_select_runs.py':step06_code,
        'selection_reports.py':step06_code.with_name('selection_reports.py')}
    require(set(m['code_sha256'])==set(code_files),'Unexpected export code manifest')
    for name,path in code_files.items():
        require(sha(path)==m['code_sha256'][name],'Export code differs: '+name)
    require(sha(export/'export_review.xlsx')==m['export_review_xlsx_sha256'],'Export workbook differs from its evidence')
    require(m['selection_counts']==sm['counts'] and m['summary']==summary, 'Export totals differ from the selected plan')
    require(all(m[field] is False for field in ('source_files_modified','metadata_modified','mth5_created',
            'timestamps_corrected','resampling_applied','calibration_applied','rotation_applied')),
            'Unexpected transformations in this Level 0 export')
    runs=[r for r in records if r['record_type']=='verified_ascii_run']
    bykey={(p['station'],p['run_id']):p for p in plans}
    require(len(runs)==len(bykey)==len(records)-1 and {(r['station'],r['run_id']) for r in runs}==set(bykey),
            'Export run coverage differs')
    seen_files=[];seen_groups=[];total_values=total_source_bytes=0
    for r in runs:
        p=bykey[r['station'],r['run_id']]
        require(r['plan']==p and r['header_verified'] is True and r['sample_values_verified'] is True,
                'Export run differs from the selected plan or is not verified')
        require(r['readback_samples_per_channel']==p['samples_per_channel'],'Export count differs')
        require(r['relative_path']==r['station']+'/'+r['station']+'_'+r['run_id']+'.txt','Unexpected ASCII relative path')
        require(bool(re.fullmatch('[0-9a-f]{64}',r['ascii_sha256'])),'Missing ASCII checksum')
        require(set(r['sample_sha256_by_channel'])==set(p['channels']) and
                all(re.fullmatch('[0-9a-f]{64}',h) for h in r['sample_sha256_by_channel'].values()),
                'Invalid output sample hashes')
        require(r['sample_hash_encoding']=='signed_int64_little_endian_values_v1','Unsupported sample hash representation')
        header=dict(line[2:].split(': ',1) for line in r['header_ascii'].splitlines())
        last=iso_ns(Fraction(p['end_exclusive_ns'])-Fraction(10**9)/Fraction(p['rate_hz']))
        expected={'schema':'mtl0.ascii_run/1','station':p['station'],'run_id':p['run_id'],
            'start_utc_assumed':p['start_utc_assumed'],'end_exclusive_utc_assumed':p['end_exclusive_utc_assumed'],
            'last_sample_utc_assumed':last,'sample_rate_hz':p['rate_hz'],
            'samples_per_channel':str(p['samples_per_channel']),'columns':' '.join(c.upper() for c in p['channels']),
            'selection_evidence_sha256':selection_hash}
        require(all(header.get(k)==v for k,v in expected.items()),'Export header differs from the plan')
        require([a['group_id'] for a in r['source_verification']]==p['group_ids'],'Source verification order differs')
        for proof in r['source_verification']:
            gid=proof['group_id'];s=selections[gid]
            require(s['action']=='include' and set(proof['channels'])==set(p['channels']),'Excluded/unexpected source in export')
            seen_groups.append(gid)
            for c,v in proof['channels'].items():
                original_info=s['qc_snapshot']['channels'][c]
                require(v['file_id']==original_info['file_id'] and v['sample_count']==original_info['sample_count']
                        and v['source_sha256']==original_info['file_sha256'] and v['sample_sha256']==original_info['sample_sha256']
                        and v['sample_hash_encoding']==original_info['sample_hash_encoding']
                        and v['source_unchanged_during_read'] is True, 'Source verification differs from QC')
                seen_files.append(v['file_id']);total_values+=v['sample_count'];total_source_bytes+=original_info['bytes']
    require(len(seen_groups)==len(set(seen_groups))==summary['included_recording_groups'], 'Repeated/missing exported groups')
    require(set(seen_groups)=={gid for gid,s in selections.items() if s['action']=='include'},'Selected group coverage differs')
    require(len(seen_files)==len(set(seen_files))==summary['source_channel_files'] and
            total_values==summary['sample_values'] and total_source_bytes==summary['selected_source_bytes'],
            'Source totals differ from the export summary')
    previous=six.read_jsonl(decisions/'decision_evidence.jsonl')
    dm=next(r for r in previous if r['id']=='manifest:station_decisions')
    original_hash=sha(original)
    expected_original={h for p,h in dm['input_sha256'].items() if PurePosixPath(p).suffix.lower()=='.xlsx'}
    require(expected_original=={original_hash},'Original Excel differs from the workbook used for station decisions')
    hashes.update({str(original.resolve()):original_hash,str(evidence_path.resolve()):sha(evidence_path),
                   str((export/'export_review.xlsx').resolve()):m['export_review_xlsx_sha256']})
    return {'runs':runs,'selections':selections,'prior_decisions':[r for r in previous if r['record_type']=='station_decision'],
            'input_sha256':hashes,'original_sha256':original_hash,'selection_manifest':sm,'export_manifest':m,
            'export_evidence_sha256':sha(evidence_path),'code_files':code_files}
