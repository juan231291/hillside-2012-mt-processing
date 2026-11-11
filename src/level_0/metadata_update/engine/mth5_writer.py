"""Write one station per MTH5, then independently read all samples and metadata."""
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import h5py
import numpy as np
from mth5.mth5 import MTH5
from station_metadata_json import require, sha, read_station
from mth5_ascii import CheckedAscii, header_for, stamp
from mth5_native import native_station, verify_native


def versions():
    return {'python':platform.python_version(), **{k:importlib.metadata.version(k) for k in
            ('mth5','mt_metadata','h5py','numpy','pydantic')}}


def require_versions():
    found = versions()
    require(found['mth5']=='0.6.4' and found['mt_metadata']=='1.0.4',
            'This writer is tested with mth5 0.6.4 and mt_metadata 1.0.4; use NCI-geophys/26.03')
    return found


def ascii_path(root, relative):
    root = Path(root).resolve()
    path = root.joinpath(*relative.split('/')).resolve()
    require(root in path.parents and path.is_file(),'ASCII source is missing or outside the input root: '+relative)
    return path


def remove_unknown_attrs(handle, base, mapping):
    for field in mapping['omitted_unknown_native_fields']:
        if field in handle[base].attrs:
            del handle[base].attrs[field]


def check_omissions(group,mapping):
    for field in mapping['omitted_unknown_native_fields']:
        require(field not in group.attrs,'Unrecorded location was stored as a measured value: '+field)


def verify_file(path,record,mapping,payloads,chunk_rows=65536):
    sid, survey_id = record['station_id'],record['survey']['name']
    reports = []
    with MTH5() as m:
        m.open_mth5(path,mode='r')
        require(m.validate_file() and m.file_version=='0.2.0' and int(m.data_level)==0,'MTH5 format/level validation failed')
        require(m.surveys_group.groups_list==[survey_id],'Unexpected survey set')
        survey = m.get_survey(survey_id)
        require(survey.stations_group.groups_list==[sid],'Unexpected station set')
        verify_native(survey.metadata,mapping['survey'],'survey')
        station = m.get_station(sid,survey=survey_id)
        verify_native(station.metadata,mapping['station'],'station')
        stored_runs = {k for k,v in station.hdf5_group.items() if str(v.attrs.get('mth5_type','')).lower()=='run'}
        require(stored_runs=={r['id'] for r in record['runs']},'Stored run set differs')
        require(set(station.metadata.channels_recorded)==set(record['station']['channels_recorded']),'Station channel list differs')
        require(set(station.metadata.run_list)==set(record['station']['run_list']),'Station run list differs')
        for info in record['runs']:
            run = m.get_run(sid,info['id'],survey=survey_id)
            verify_native(run.metadata,mapping['runs'][info['id']],info['id'])
            require(set(run.hdf5_group)==set(info['channels']),'Missing or invented run channels')
            require(set(run.metadata.channels_recorded_electric)==set(info['channels_recorded_electric']) and
                    set(run.metadata.channels_recorded_magnetic)==set(info['channels_recorded_magnetic']), 'Run channel metadata differs')
            count = info['sample_coverage']['samples_per_channel']
            channel_reports = {}
            for c in info['ascii']['column_order']:
                channel = run.get_channel(c)
                data = channel.hdf5_dataset
                require(data.shape==(count,) and data.dtype==np.dtype('<i8'),'Channel shape/dtype differs')
                require(data.compression=='gzip' and data.compression_opts==4 and data.shuffle and data.fletcher32,
                        'Channel storage settings differ: expected gzip 4, shuffle and Fletcher32')
                cm = mapping['runs'][info['id']]['channels'][c]
                verify_native(channel.metadata,cm,info['id']+'/'+c)
                check_omissions(data,cm)
                require(channel.metadata.filters==[],'Unexpected response filter on Level 0 channel')
                digest = hashlib.sha256()
                lo,hi = None,None
                for start in range(0,count,chunk_rows):
                    block = data[start:min(count,start+chunk_rows)]
                    digest.update(block.astype('<i8',copy=False).tobytes())
                    lo = int(block.min()) if lo is None else min(lo,int(block.min()))
                    hi = int(block.max()) if hi is None else max(hi,int(block.max()))
                require(digest.hexdigest()==info['ascii']['sample_sha256_by_channel'][c], 'MTH5 sample readback differs: '+c)
                channel_reports[c] = {'samples':count,'sample_sha256':digest.hexdigest(),'min':lo,'max':hi,
                                      'metadata_verified':True,'samples_verified':True,'storage_verified':True,
                                      'storage':{'compression':data.compression,'compression_opts':data.compression_opts,
                                                 'shuffle':bool(data.shuffle),'fletcher32':bool(data.fletcher32)}}
            reports.append({'run_id':info['id'],'channels':channel_reports,'sample_rate':info['sample_rate'],
                            'start_utc_assumed':info['time_period']['start'],'last_sample_utc_assumed':info['time_period']['end']})
        summary = m.channel_summary.to_dataframe()
        require(len(summary)==sum(len(r['channels']) for r in record['runs']),'MTH5 channel summary count differs')
    with h5py.File(path,'r') as handle:
        group = handle['MT_Level0_Provenance']
        require(set(group)==set(payloads),'Embedded provenance documents differ')
        for name,payload in payloads.items():
            observed = group[name][()].tobytes()
            require(observed==payload and hashlib.sha256(observed).hexdigest()==group[name].attrs['sha256'],
                    'Embedded provenance readback differs: '+name)
    return reports


def write_station(job):
    library_versions = require_versions()
    json_path, ascii_root, out = Path(job['json_path']),Path(job['ascii_root']),Path(job['out'])
    json_bytes = json_path.read_bytes()
    require(hashlib.sha256(json_bytes).hexdigest()==job['json_sha256'],'Station JSON changed')
    record = read_station(json_path)
    sid = record['station_id']
    survey_meta, station_meta, native_runs, mapping = native_station(record)
    final = out/(sid+'_L0.mth5')
    partial = out/(sid+'_L0.partial.mth5')
    require(not final.exists() and not partial.exists(),'Output station file already exists')
    receipts, source_stamps = [],{}
    base = f'/Experiment/Surveys/{survey_meta.id}/Stations/{sid}'
    with MTH5(file_version='0.2.0',data_level=0,compression='gzip',compression_opts=4,shuffle=True) as m:
        m.open_mth5(partial,mode='x')
        survey = m.add_survey(survey_meta.id,survey_metadata=survey_meta)
        station = m.add_station(sid,survey=survey_meta.id,station_metadata=station_meta)
        for info in record['runs']:
            path = ascii_path(ascii_root,info['ascii']['relative_path'])
            source_stamps[str(path)] = stamp(path)
            count = info['sample_coverage']['samples_per_channel']
            native = native_runs[info['id']]
            run = station.add_run(info['id'],run_metadata=native['metadata'])
            channels = {}
            for c in info['ascii']['column_order']:
                channels[c] = run.add_channel(c,'electric' if c.startswith('e') else 'magnetic',data=None,
                    shape=(count,),max_shape=(count,),chunks=(min(65536,count),),channel_dtype='<i8',
                    channel_metadata=native['channels'][c], **m.dataset_options)
            reader = CheckedAscii(path,header_for(record,info,job['selection_hash']),info)
            offset = 0
            print(f'WRITING {sid}/{info["id"]}: {count} samples per channel',flush=True)
            for block in reader.blocks():
                for column,c in enumerate(info['ascii']['column_order']):
                    channels[c].hdf5_dataset[offset:offset+len(block)] = block[:,column]
                offset += len(block)
            require(reader.receipt is not None,'ASCII verification did not complete')
            receipts.append(reader.receipt)
            for channel in channels.values():channel.write_metadata()
            run.update_metadata()
        station.update_metadata()
        # Calling survey.update_metadata() would narrow the survey dates to
        # this file. Keep the original whole-survey dates from the JSON.
        sm = survey.metadata
        sm.time_period.start_date = record['survey']['time_period']['start_date']
        sm.time_period.end_date = record['survey']['time_period']['end_date']
        survey.write_metadata()
        m.channel_summary.summarize()
    conversion = {'schema':'mtl0.level0.mth5_conversion/1','station_id':sid,'mapping':mapping,
        'input_ascii_verification':receipts,'software':library_versions,'writer_code_sha256':job['code_sha256'],
        'storage':{'dtype':'signed_int64','compression':'gzip level 4','shuffle':True,'fletcher32':True,'sample_values_changed':False},
        'metadata_policy':{'native_units':'digital counts','source_units':'ADC counts','unit_scale_changed':False,
            'native_comments':'Pipes replaced by semicolons to avoid the mt_metadata 1.0.4 comment delimiter parser; exact source text is embedded.',
            'unknown_floats':'NaN where accepted. Unknown coordinate attributes rejecting NaN are omitted and listed in mapping.',
            'schema_defaults':'Unprovided library defaults are recorded in mapping and are not source measurements.',
            'calibration_response_added':False,'source_utc_assumption_retained':True}}
    payloads = {'metadata_json':json_bytes,'metadata_json_manifest':job['manifest_bytes'],
                'conversion_json':json.dumps(conversion,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf-8')}
    with h5py.File(partial,'a') as handle:
        for info in record['runs']:
            for c in info['channels']:
                remove_unknown_attrs(handle,base+'/'+info['id']+'/'+c,mapping['runs'][info['id']]['channels'][c])
        group = handle.create_group('MT_Level0_Provenance')
        group.attrs['encoding'] = 'UTF-8 JSON stored as uint8 byte datasets'
        for name,payload in payloads.items():
            dataset = group.create_dataset(name,data=np.frombuffer(payload,dtype=np.uint8),compression='gzip')
            dataset.attrs['sha256'] = hashlib.sha256(payload).hexdigest()
    verified = verify_file(partial,record,mapping,payloads)
    require(sha(json_path)==job['json_sha256'],'JSON changed while the MTH5 was created')
    require(all(stamp(Path(p))==s for p,s in source_stamps.items()),'ASCII input changed while the MTH5 was created')
    require(not final.exists(),'Final output appeared while writing')
    partial.rename(final)
    print(f'VERIFIED MTH5: {sid}, {len(verified)} runs; all samples and mapped metadata checked',flush=True)
    return {'station':sid,'file':final.name,'sha256':sha(final),'bytes':final.stat().st_size,
            'run_count':len(verified),'runs':verified,'json_sha256':job['json_sha256'],
            'all_samples_verified':True,'native_metadata_verified':True,'embedded_json_verified':True,
            'channel_storage_verified':True,
            'software':library_versions}
