#!/usr/bin/env python3
"""Create metadata-corrected copies of the completed v5 Level 1 station files.

The original release and its manifests are preserved. No numerical processing
is rerun. Use --check to see the station count and additional storage required.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid

import h5py
import numpy as np

from native_provenance import REVISION, FIELDS, plan_file, apply_plan, verify_native, require, utcnow


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(8*1024**2), b''):
            digest.update(block)
    return digest.hexdigest()


def save(path, value):
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    tmp.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    os.replace(tmp, path)


def metadata_snapshot(path, changes):
    def plain(value):
        if isinstance(value, np.ndarray): return plain(value.tolist())
        if isinstance(value, np.generic): return plain(value.item())
        if isinstance(value, bytes): return value.decode('utf-8', errors='replace')
        if isinstance(value, (tuple, list)): return [plain(x) for x in value]
        if isinstance(value, float) and not np.isfinite(value): return str(value)
        if isinstance(value, (str, int, float, bool)) or value is None: return value
        return str(value)
    result = {}
    with h5py.File(path, 'r') as f:
        def visit(name, obj):
            attrs = {k: plain(v) for k,v in obj.attrs.items()
                     if not (obj.name in changes and k in FIELDS)}
            result[obj.name] = {'attrs': attrs}
            if isinstance(obj, h5py.Dataset):
                result[obj.name].update(shape=list(obj.shape), dtype=str(obj.dtype))
        visit('', f); f.visititems(visit)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--creator', default='Juan Herrera')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    source = args.input_dir.resolve(strict=True)
    output = args.output_dir.resolve()
    require(source.is_dir() and source != output and source not in output.parents, 'Use a separate output directory outside the source release')
    require(not args.output_dir.is_symlink(), 'Output directory must not be a symlink')
    manifest_path = source / 'release_manifest.json'
    require(manifest_path.is_file(), 'Source release_manifest.json is required')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    require(manifest.get('complete') is True and not manifest.get('errors'), 'Source release is not complete')
    require(manifest.get('release') == 'v5_20260921_200mv', 'Expected the current v5 release')
    entries = {e['station']:e for e in manifest['stations']}
    paths = sorted(source.glob('*_L1.mth5'))
    require(bool(paths), 'No completed station files found')
    require(len(entries) == len(paths) == manifest['station_count'], 'Source file inventory differs from the release manifest')
    jobs = []
    for path in paths:
        require(not path.is_symlink(), 'Linked source file refused: '+str(path))
        plan = plan_file(path, args.creator)
        sid = plan['station']
        require(sid in entries and path.name == entries[sid]['file'], 'Source filename differs from its manifest entry')
        jobs.append((path, plan))
    state_path = output / 'native_provenance_update.json'
    state = {'revision':REVISION, 'creator':args.creator, 'source_directory':str(source), 'stations':{}}
    if state_path.exists():
        previous = json.loads(state_path.read_text(encoding='utf-8'))
        require(all(previous.get(k)==state[k] for k in ('revision','creator','source_directory')), 'Existing update uses different inputs/settings')
        state = previous
    for path, plan in jobs:
        target = output / path.name
        require(not target.exists() or plan['station'] in state['stations'], 'Existing output has no update receipt: '+str(target))
    needed = sum(path.stat().st_size for path,plan in jobs if not (output/path.name).exists())
    ancestor = output
    while not ancestor.exists(): ancestor = ancestor.parent
    free = shutil.disk_usage(ancestor).free
    print(f'Stations: {len(jobs)}. Additional copied file content: {needed/2**30:.2f} GiB.', flush=True)
    print('Original MTH5 files and original release reports remain unchanged. Project quota also applies.', flush=True)
    require(free >= needed + 64*1024**2, 'Insufficient filesystem space for separate corrected copies')
    if args.check: return
    output.mkdir(parents=True, exist_ok=True)
    lock = output / '.native_provenance_update.lock'
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(fd, (str(os.getpid())+'\n').encode()); os.close(fd)
    try:
        for path, plan in jobs:
            sid = plan['station']; target = output / path.name
            expected = entries[sid]['output_sha256']
            if target.exists():
                receipt = state['stations'][sid]
                require(receipt['source_sha256']==expected and sha(target)==receipt['output_sha256'], 'Existing corrected output differs: '+sid)
                verify_native(target, plan, samples=False)
                print('Verified existing corrected file: '+sid, flush=True)
                continue
            before = metadata_snapshot(path, plan['changes'])
            partial = output / (path.stem+'.'+uuid.uuid4().hex+'.partial.mth5')
            stamp = (path.stat().st_size,path.stat().st_mtime_ns)
            digest = hashlib.sha256()
            with path.open('rb') as src, partial.open('xb') as dst:
                for block in iter(lambda:src.read(8*1024**2), b''):
                    digest.update(block); dst.write(block)
                dst.flush(); os.fsync(dst.fileno())
            require(digest.hexdigest()==expected, 'Source file checksum differs from release manifest: '+sid)
            require(stamp==(path.stat().st_size,path.stat().st_mtime_ns), 'Source changed during copying')
            apply_plan(partial,plan)
            verify_native(partial,plan,samples=True)
            require(metadata_snapshot(partial, plan['changes'])==before, 'Metadata outside the planned provenance fields changed: '+sid)
            receipt={'station':sid,'source_sha256':expected,'output_sha256':sha(partial),
                     'bytes':partial.stat().st_size,'verified_utc':utcnow(),'native_mth5_readback_verified':True,
                     'all_waveform_hashes_unchanged':True,'other_metadata_unchanged':True,'changes':plan['changes']}
            state['stations'][sid]=receipt
            save(state_path,state)
            # Publish the new copy without overwriting an existing target.
            os.link(partial,target); partial.unlink()
            print('Verified corrected metadata and unchanged waveforms: '+sid, flush=True)
        result=copy.deepcopy(manifest)
        result['output_directory']=str(output)
        result['native_provenance_revision']=REVISION
        result['metadata_updated_utc']=utcnow()
        result['source_release_directory']=str(source)
        for item in result['stations']:
            receipt=state['stations'][item['station']]
            item['previous_output_sha256']=item['output_sha256']
            item['output_sha256']=receipt['output_sha256']; item['bytes']=receipt['bytes']
            item['native_provenance_revision']=REVISION
        save(output/'release_manifest.json',result)
        print(f'COMPLETE: {len(jobs)} station files with standard native provenance. No time-series processing was rerun.',flush=True)
    finally:
        lock.unlink(missing_ok=True)


if __name__=='__main__': main()
