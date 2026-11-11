"""Scan each source independently, then build survey-wide name links."""
import importlib
import os
from pathlib import Path
from parallel_jobs import map_jobs


def scan_source(source,preview_bytes,mseed_limit):
    scan=importlib.import_module('01_inventory').scan_file
    files,headers,errors=[],{},[]
    for directory,dirs,names in os.walk(source['root'],followlinks=False,onerror=lambda e:errors.append(str(e))):
        dirs.sort()
        links=[n for n in dirs if (Path(directory)/n).is_symlink()]
        dirs[:]=[n for n in dirs if n not in links]
        for name in sorted(names+links):
            files.append(scan(Path(directory)/name,source['root'],source['role'],source['label'],preview_bytes,mseed_limit,headers))
    print(f"SCANNED {source['label']}: {len(files)} entries",flush=True)
    return files,headers,errors


def scan_sources(sources,preview_bytes,mseed_limit):
    results=map_jobs(scan_source,[(s,preview_bytes,mseed_limit) for s in sources],int(os.getenv('MTL0_WORKERS','1')))
    files,headers,errors=[],{},[]
    for f,h,e in results:files.extend(f);headers.update(h);errors.extend(e)
    return files,headers,errors
