"""Create verified Level 0 MTH5 files using only Step 09 JSON and Step 07 ASCII."""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import multiprocessing
from pathlib import Path
import re
import station_metadata_json
from station_metadata_json import require, read_station, sha, strict_load
from mth5_writer import ascii_path, write_station, require_versions


def prepare(json_dir,ascii_root,out,workers):
    require(not out.exists(),'Output directory already exists; select a new MTH5 version')
    require(workers>0,'Worker count must be positive')
    for root in (json_dir.resolve(),ascii_root.resolve()):
        require(out.resolve()!=root and root not in out.resolve().parents,'Output must be separate from input directories')
    manifest_path = json_dir/'manifest.json'
    manifest_bytes = manifest_path.read_bytes()
    manifest = strict_load(manifest_path)
    require(manifest['schema']=='mtl0.level0.metadata_json_batch/2' and manifest['complete'] is True and
            manifest['all_json_readback_verified'] is True and manifest['data_level']==0,'Verified Step 09 JSON batch required')
    require(all(manifest[k] is False for k in ('workbook_modified','samples_modified','mth5_created')),'Unexpected input processing')
    require(sha(Path(station_metadata_json.__file__))==manifest['code_sha256']['station_metadata_json.py'],
            'JSON reader differs from the version used for Step 09')
    selection = [h for name,h in manifest['input_sha256'].items() if name.replace('\\','/').rsplit('/',1)[-1]=='selection_evidence.jsonl']
    require(len(selection)==1 and re.fullmatch('[0-9a-f]{64}',selection[0]) is not None,'Missing selection identity for ASCII headers')
    code = {n:sha(Path(__file__).with_name(n)) for n in ('10_create_mth5.py','mth5_writer.py','mth5_native.py','mth5_ascii.py')}
    code['station_metadata_json.py'] = sha(Path(station_metadata_json.__file__))
    jobs = []
    ids = set()
    run_total = channel_total = sample_bytes = 0
    for entry in manifest['stations']:
        sid = entry['station']
        require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*',sid) is not None and sid.casefold() not in ids,
                'Invalid or repeated station identifier')
        ids.add(sid.casefold())
        require(entry['file']==sid+'.json','Unexpected JSON filename')
        path = json_dir/entry['file']
        require(sha(path)==entry['sha256'],'Station JSON checksum differs: '+sid)
        record = read_station(path)
        require(record['station_id']==sid and record['source']=={**manifest['source'],
                'headers':record['source']['headers'],'rows':record['source']['rows']}, 'Station JSON source identity differs')
        require(len(record['runs'])==entry['run_count'] and
                sum(len(r['channels']) for r in record['runs'])==entry['channel_instances'], 'Station totals differ')
        for run in record['runs']:
            ascii_path(ascii_root,run['ascii']['relative_path'])
            sample_bytes += run['sample_coverage']['samples_per_channel']*len(run['channels'])*8
        run_total += entry['run_count']; channel_total += entry['channel_instances']
        jobs.append({'json_path':str(path.resolve()),'json_sha256':entry['sha256'],
                     'ascii_root':str(ascii_root.resolve()),'out':str(out.resolve()),'selection_hash':selection[0],
                     'manifest_bytes':manifest_bytes,'code_sha256':code})
    require(jobs and len(jobs)==manifest['station_count'] and run_total==manifest['run_count'] and
            channel_total==manifest['channel_instances'],'Batch totals differ')
    return jobs,manifest_path,sha(manifest_path),sample_bytes


def main(json_dir,ascii_root,out,workers):
    require_versions()
    jobs,manifest_path,manifest_sha,sample_bytes = prepare(json_dir,ascii_root,out,workers)
    count = min(workers,len(jobs))
    out.mkdir(parents=True,exist_ok=False)
    print(f'Creating {len(jobs)} station MTH5 files with {count} workers. Uncompressed sample payload: {sample_bytes/1024**3:.2f} GiB.',flush=True)
    results = []
    from parallel_jobs import map_jobs
    results=map_jobs(write_station,[(job,) for job in jobs],count)
    require(sha(manifest_path)==manifest_sha,'JSON batch manifest changed while writing')
    require(len(results)==len(jobs) and all(r['all_samples_verified'] and r['channel_storage_verified'] for r in results),'Incomplete station verification')
    result = {'schema':'mtl0.level0.mth5_batch/1','complete':True,'created_utc':datetime.now(timezone.utc).isoformat(),
        'file_version':'0.2.0','data_level':0,'stations':sorted(results,key=lambda r:r['station']),
        'station_count':len(results),'run_count':sum(r['run_count'] for r in results),
        'channel_instances':sum(len(run['channels']) for r in results for run in r['runs']),
        'input_json_manifest_sha256':manifest_sha,'writer_code_sha256':jobs[0]['code_sha256'],
        'source_samples_changed':False,'channel_storage_verified':True,
        'resampling_applied':False,'rotation_applied':False,
        'polarity_correction_applied':False,'calibration_applied':False,'workers':count,
        'verification':'All ASCII headers/bytes/sample hashes checked, followed by full MTH5 sample readback, mapped metadata, embedded source JSON and channel compression/checksum settings checks.'}
    path = out/'manifest.json'
    with path.open('x',encoding='utf-8',newline='\n') as f:f.write(json.dumps(result,ensure_ascii=False,allow_nan=False,indent=2)+'\n')
    require(strict_load(path)==result,'Output manifest readback differs')
    print(f'SUCCESS: {len(results)} verified Level 0 MTH5 files. Output: {out}',flush=True)
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--json-dir',type=Path,required=True)
    p.add_argument('--ascii-root',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--workers',type=int,default=1)
    main(**vars(p.parse_args()))
