#!/usr/bin/env python3
"""Import the accepted Hillside EDI collection without changing its responses."""
from pathlib import Path
from datetime import datetime, timezone
import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import sys
import uuid

import h5py
import numpy as np
from mth5.mth5 import MTH5
from mt_metadata.timeseries import Station, Survey
from mt_metadata.transfer_functions.core import TF
from mt_metadata.transfer_functions.tf.statistical_estimate import StatisticalEstimate
from loguru import logger

PACKAGE=Path(__file__).resolve().parent
RELEASE='v2_20260921_existing_edi'
TF_ID='edi_20260102'
BASE='/Experiment/Surveys/Hillside/Stations'

def require(ok,message):
    if not ok: raise ValueError(message)
def sha(data): return hashlib.sha256(data).hexdigest()
def now(): return datetime.now(timezone.utc).isoformat()
def encoded(value): return json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False).encode('utf-8')
def save(path,value):
    tmp=path.with_name(path.name+'.tmp')
    tmp.write_bytes(encoded(value)+b'\n'); tmp.replace(path)
def parse_attribute(value):
    if isinstance(value,bytes): value=value.decode()
    if isinstance(value,str) and value.startswith(('[','{')): return json.loads(value)
    return value
def same_time(a,b):
    return datetime.fromisoformat(str(a).replace('Z','+00:00'))==datetime.fromisoformat(str(b).replace('Z','+00:00'))

def raw_response(raw):
    text=raw.decode('utf-8-sig').replace('\r','')
    blocks={}
    keys=['FREQ','ZROT']+[z+s for z in ('ZXX','ZXY','ZYX','ZYY') for s in ('R','I','.VAR')]
    for m in re.finditer(r'^>([^\n]+)\n([^>]+)',text,re.M):
        name=m.group(1).split()[0].upper()
        if name in keys:
            require(name not in blocks,'Repeated EDI block '+name)
            blocks[name]=np.array([float(v.replace('D','E').replace('d','e')) for v in re.findall(r'[-+]?(?:\d+\.\d*|\d*\.\d+|\d+)(?:[eEdD][-+]?\d+)?',m.group(2))])
    require(set(keys)<=set(blocks),'Required EDI response blocks missing')
    n=len(blocks['FREQ'])
    require(all(len(blocks[k])==n for k in keys),'EDI block lengths differ')
    require(np.all(blocks['FREQ']>0) and np.all(np.diff(blocks['FREQ'])<0),'Frequencies must be positive and strictly decreasing')
    require(all(np.all(np.isfinite(v)) and np.all(np.abs(v)<1e30) for v in blocks.values()),'Missing/sentinel values require a separate import policy')
    z=np.zeros((n,2,2),complex); var=np.zeros((n,2,2))
    for key,i,j in [('ZXX',0,0),('ZXY',0,1),('ZYX',1,0),('ZYY',1,1)]:
        z[:,i,j]=blocks[key+'R']+1j*blocks[key+'I']; var[:,i,j]=blocks[key+'.VAR']
    require(np.all(var>=0),'Negative impedance variance')
    require(not re.search(r'^>T[XY]',text,re.M),'This release expects impedance-only EDI files')
    return blocks['FREQ'],blocks['ZROT'],z,var

def parameters(record):
    p=[f"source_edi_data_id = {record['original_data_id']}",
       f"source_edi_file_date = {record['header_file_date']}",
       'processed_date_basis = EDI FILEDATE (file date; estimation date not independently supplied)',
       f"edi_header_program_version = {record['header_program_version']}",
       f"source_acquisition_sample_rate_hz = {record['source_acquisition_sample_rate_hz']:g}",
       f"header_job_sample_rate_label = {record['header_job_sample_rate_label']}",
       f"header_reported_processing_start = {record['reported_processing_window']['start']}",
       f"header_reported_processing_end = {record['reported_processing_window']['end']}",
       'merge_status = merged',
       f"processing_table_note = 2nd Processing!{record['final_processing_note_cell']}",
       'response_values_changed_during_import = false']
    if record['recorded_original_polarity_corrections']:
        p.append('original_processing_polarity_corrections_from_table = '+','.join(record['recorded_original_polarity_corrections']))
    for k,v in record['edi_processing_options'].items(): p.append(k.lower().replace(' ','_')+' = '+v)
    return p

def prepare_tf(record):
    path=PACKAGE/record['edi_file']; raw=path.read_bytes()
    require(sha(raw)==record['edi_sha256'],'EDI checksum changed: '+record['station'])
    freq,rot,z,var=raw_response(raw)
    tf=TF(); tf.read(path)
    location=record['adopted_station_context']['location']
    require(round(tf.latitude,5)==location['latitude'] and round(tf.longitude,5)==location['longitude'] and tf.elevation==location['elevation'],'EDI and adopted station location differ beyond coordinate precision')
    require(len(tf.period)==record['frequency_count'],'Frequency count differs')
    require(np.array_equal(tf.period,1/freq),'Parser period grid differs from EDI')
    require(np.array_equal(np.asarray(tf.impedance),z),'Parser impedance differs from EDI')
    require(np.array_equal(np.asarray(tf.impedance_error),np.sqrt(var)),'Parser errors differ from EDI variances')
    require(not tf.has_tipper(),'Unexpected tipper')
    require(np.array_equal(np.broadcast_to(np.asarray(tf._rotation_angle),rot.shape),rot),'Parser rotation differs from EDI')
    return tf,raw,freq,rot,z,var

def verify(path,record,signature,workbook_bytes):
    tf,raw,freq,rot,z,var=prepare_tf(record)
    sid=record['station']; station_path=BASE+'/'+sid; group_path=station_path+'/Transfer_Functions/'+TF_ID
    with h5py.File(path,'r') as f:
        require(f.attrs['data_level']==2,'Not Level 2')
        station=f[station_path]; g=f[group_path]; p=f['MT_Level2_Provenance']
        require(set(station['Transfer_Functions'].keys())=={TF_ID},'Unexpected response version')
        require(p.attrs['fingerprint']==signature,'Import fingerprint differs')
        require(set(g.keys())=={'period','impedance','impedance_error'},'Unexpected estimates, missing estimates, or invented tipper')
        require(np.array_equal(g['period'][()],tf.period),'Saved periods changed')
        require(g['period'].attrs['units']=='second','Period units are not seconds')
        for estimate in ('impedance','impedance_error'):
            require(g[estimate].attrs['units']==g.attrs['units'],'Estimate units differ from transfer-function units')
        require(np.array_equal(g['impedance'][()],z),'Saved impedance changed')
        require(np.array_equal(g['impedance_error'][()],np.sqrt(var)),'Saved errors changed')
        require(np.array_equal(p['source_frequency_hz'][()],freq),'Source frequencies changed')
        require(np.array_equal(p['source_zrot_degrees'][()],rot),'Source rotation array changed')
        require(np.array_equal(p['source_impedance_variance'][()],var),'Source variances changed')
        require(p['source_edi'][()].tobytes()==raw,'Original EDI bytes differ')
        require(p['source_processing_workbook'][()].tobytes()==workbook_bytes,'Original processing workbook differs')
        require(json.loads(p['processing_record_json'][()].tobytes())==record,'Processing record differs')
        for name in ('source_edi','source_processing_workbook','processing_record_json','import_json'):
            ds=p[name]; require(sha(ds[()].tobytes())==ds.attrs['sha256'],'Provenance checksum differs: '+name)
        require(station.attrs['comments']==record['processing_comment'],'Processing comment differs')
        for name,value in record['adopted_station_context']['location'].items():
            require(station.attrs['location.'+name]==value,'Adopted station location differs')
        require(station.attrs['orientation.reference_frame']=='geomagnetic' and g.attrs['coordinate_system']=='geomagnetic','Reference frame differs')
        require(parse_attribute(g.attrs['remote_references'])==[record['remote_reference']],'Remote reference differs')
        require(g.attrs['processed_by.author']==record['processed_by']['author'],'Processing author differs')
        require(parse_attribute(g.attrs['processing_parameters'])==parameters(record),'Processing parameters differ')
        require(parse_attribute(g.attrs['runs_processed'])==[],'Invented linkage to Level 1 runs')
        require(all(v.attrs.get('mth5_type','')!='Run' for v in station.values()),'Unexpected time-series run groups')
        require(same_time(station.attrs['time_period.start'],record['reported_processing_window']['start']) and same_time(station.attrs['time_period.end'],record['reported_processing_window']['end']),'Header-reported times differ')
        provenance=json.loads(p['import_json'][()].tobytes())
        require(same_time(station.attrs['provenance.creation_time'],provenance['imported_utc']),'Import creation date differs')
        require(g['impedance'].shape==(record['frequency_count'],2,2),'Wrong impedance dimensions')
    return {'station':sid,'complete':True,'file':path.name,'file_sha256':sha(path.read_bytes()),'frequency_count':len(freq),
            'frequency_min_hz':float(freq.min()),'frequency_max_hz':float(freq.max()),'has_impedance':True,'has_tipper':False,
            'reference_frame':'geomagnetic','remote_reference':record['remote_reference'],'processed_by':record['processed_by']['author'],'source_edi_sha256':record['edi_sha256'],
            'processing_comment':record['processing_comment'],'response_arrays_verified_against_source_edi':True,'source_edi_and_workbook_preserved':True}

def convert(record,out,signature,manifest,workbook_bytes,versions):
    sid=record['station']; final=out/f'{sid}_L2.mth5'
    if final.exists():
        receipt=verify(final,record,signature,workbook_bytes)
        print('VERIFIED EXISTING '+sid,flush=True); return receipt
    tf,raw,freq,rot,z,var=prepare_tf(record)
    stamp=now()
    sm=Station(id=sid)
    context=record['adopted_station_context']
    sm.acquired_by.author=context['acquired_by']
    sm.channel_layout=context['channel_layout']
    sm.location.latitude=context['location']['latitude']; sm.location.longitude=context['location']['longitude']; sm.location.elevation=context['location']['elevation']
    sm.location.datum='WGS 84'
    sm.location.declination.value=float('nan')
    sm.orientation.reference_frame='geomagnetic'
    sm.comments=record['processing_comment']
    sm.geographic_name='Yorke Peninsula'
    sm.channels_recorded=['ex','ey','hx','hy']
    sm.time_period.start=record['reported_processing_window']['start']
    sm.time_period.end=record['reported_processing_window']['end']
    sm.provenance.creation_time=stamp
    sm.provenance.creator.author='Juan Herrera'
    sm.provenance.software.name='Hillside existing-EDI Level 2 import'
    sm.provenance.software.version=RELEASE
    survey=Survey(id='Hillside',name='Hillside',project='Hillside')
    survey.geographic_name='Yorke Peninsula'
    survey.summary='Previously processed Hillside impedance responses imported from the supplied merged EDI collection.'
    survey.time_period.start_date=record['reported_processing_window']['start'][:10]
    survey.time_period.end_date=record['reported_processing_window']['end'][:10]
    metadata=tf.station_metadata.transfer_function.copy()
    metadata.id=TF_ID; metadata.coordinate_system='geomagnetic'; metadata.remote_references=[record['remote_reference']]
    metadata.processed_by.author=record['processed_by']['author']
    metadata.processing_type='remote reference'
    metadata.processing_parameters=parameters(record)
    metadata.runs_processed=[]
    metadata.processing_config='MT_Level2_Provenance/processing_record_json'
    partial=out/f'{sid}_{uuid.uuid4().hex[:10]}.partial.mth5'
    with MTH5(file_version='0.2.0',data_level=2) as m:
        m.open_mth5(partial,mode='w')
        sg=m.add_survey('Hillside',survey_metadata=survey)
        st=m.add_station(sid,survey='Hillside',station_metadata=sm)
        g=st.transfer_functions_group.add_transfer_function(TF_ID)
        g.metadata.update(metadata); g.write_metadata()
        g.add_statistical_estimate('period',tf.period,estimate_metadata=StatisticalEstimate(name='period',data_type='float64',units='second',description='Periods at which impedance is estimated'),chunks=True,max_shape=(None,))
        for name,data in [('impedance',tf.impedance),('impedance_error',tf.impedance_error)]:
            em=StatisticalEstimate(name=name,units=metadata.units,description='Source EDI impedance' if name=='impedance' else 'Square root of source EDI impedance variance')
            g.add_statistical_estimate(name,data.rename(name),estimate_metadata=em)
        m.tf_summary.summarize()
        st.metadata.update(sm); st.write_metadata()
    provenance={'release':RELEASE,'imported_utc':stamp,'software':versions,'fingerprint':signature,
        'source_archive':manifest['source_archive'],'source_archive_sha256':manifest['source_archive_sha256'],
        'source_workbook_sha256':manifest['source_workbook_sha256'],
        'source_acquisition_metadata':{'channel_azimuths_degrees':record['edi_info_channel_azimuths'],'dipoles_m':record['edi_info_dipoles_m'],
             'scope':'Source EDI INFO acquisition descriptions retained as history; not a new measurement of tensor output axes.'},
        'lineage':record['lineage'],'numerical_processing_applied':False,'polarity_corrections_applied_during_import':False,
        'rotation_applied_during_import':False,'calibration_applied_during_import':False,
        'time_coverage_basis':'Header-reported original processing interval; full merged constituent windows are not supplied.',
        'native_processed_date_basis':'EDI FILEDATE, retained as the source file date; original estimation date not independently supplied.',
        'original_estimation_algorithm':'Not established by supplied inputs; Geotools is recorded as the EDI header program identification.',
        'processing_note_scope':'Table records original procedure; full rows, earlier notes and final follow-up are preserved. No table sign corrections are applied again.'}
    with h5py.File(partial,'a') as f:
        p=f.create_group('MT_Level2_Provenance'); p.attrs['fingerprint']=signature
        for name,payload in [('source_edi',raw),('source_processing_workbook',workbook_bytes),('processing_record_json',encoded(record)),('import_json',encoded(provenance))]:
            ds=p.create_dataset(name,data=np.frombuffer(payload,dtype=np.uint8),compression='gzip'); ds.attrs['sha256']=sha(payload)
        for name,arr in [('source_frequency_hz',freq),('source_zrot_degrees',rot),('source_impedance_variance',var)]: p.create_dataset(name,data=arr,compression='gzip')
    receipt=verify(partial,record,signature,workbook_bytes)
    require(not final.exists(),'Output appeared during import')
    partial.rename(final); receipt['file']=final.name
    print('VERIFIED LEVEL 2 '+sid,flush=True)
    return receipt

def run(batch,station_ids=None):
    logger.remove()
    versions={k:importlib.metadata.version(k) for k in ('mth5','mt_metadata','numpy','h5py')}
    require(versions['mth5']=='0.6.4' and versions['mt_metadata']=='1.0.4','Use mth5 0.6.4 and mt_metadata 1.0.4')
    manifest_bytes=(PACKAGE/'sources/processing_manifest.json').read_bytes(); manifest=json.loads(manifest_bytes)
    require(manifest['release']==RELEASE and len(manifest['records'])==71,'Unexpected source manifest')
    require({x['station'] for x in manifest['missing_edi_stations']}=={'HS055','HS078'},'Unexpected missing stations')
    workbook_bytes=(PACKAGE/'sources'/manifest['source_workbook']).read_bytes()
    require(sha(workbook_bytes)==manifest['source_workbook_sha256'],'Processing workbook checksum differs')
    signature=sha(Path(__file__).read_bytes()+manifest_bytes+workbook_bytes)
    out=batch/'level2'/('12_mth5_'+RELEASE); reports=batch/'reports'/('hillside_level2_'+RELEASE)
    out.mkdir(parents=True,exist_ok=True); reports.mkdir(parents=True,exist_ok=True)
    lock=out/'import.lock'
    fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    with os.fdopen(fd,'w') as stream: json.dump({'pid':os.getpid(),'pbs_job':os.environ.get('PBS_JOBID'),'started':now()},stream)
    results=[]; errors=[]
    try:
        records=manifest['records']
        if station_ids:
            require(set(station_ids)<={x['station'] for x in records},'Unknown requested station')
            records=[x for x in records if x['station'] in station_ids]
        for record in records:
            try:
                receipt=convert(record,out,signature,manifest,workbook_bytes,versions)
                save(reports/(record['station']+'.json'),receipt); results.append(receipt)
            except Exception as exc:
                errors.append({'station':record['station'],'error':repr(exc)})
                print('FAILED '+record['station']+': '+repr(exc),flush=True)
        full=not station_ids
        summary={'complete':not errors and len(results)==len(records),'scope':'full_survey' if full else 'selected_station_check',
                 'release':RELEASE,'created_utc':now(),'data_level':2,'station_count':len(results),
                 'stations_without_supplied_edi':[x['station'] for x in manifest['missing_edi_stations']],
                 'missing_edi_reason':'Not merged because too noisy (processing table F6 and F37).',
                 'has_tipper':False,'numerical_processing_applied':False,'fingerprint':signature,
                 'source_archive_sha256':manifest['source_archive_sha256'],'source_workbook_sha256':manifest['source_workbook_sha256'],
                 'source_differences':manifest['source_differences'],'errors':errors,'stations':results}
        report=reports/('all_verification.json' if full else 'selected_verification.json')
        save(report,summary)
        require(summary['complete'],'Import failed; inspect '+str(report))
        if full: save(out/'release_manifest.json',summary)
        print(f'COMPLETE Level 2: {len(results)} stations; report: {report}',flush=True)
        return summary
    finally:
        lock.unlink(missing_ok=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--batch',type=Path,default=Path('/g/data/tq84/jh7824/hillside_workflow/Hillside_all_level0_v3'))
    parser.add_argument('--stations',nargs='+')
    args=parser.parse_args()
    run(args.batch,args.stations)
