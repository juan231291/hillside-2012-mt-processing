"""Generate PBS launchers for this installation and a different project/config."""
import argparse
from pathlib import Path
import re
import shlex

NAMES={1:'inventory',2:'duplicates',3:'recorder_logs',4:'decisions',5:'timing_qc',6:'run_selection',
       7:'ascii',8:'excel_metadata',9:'json_metadata',10:'mth5'}
HEAVY={1,5,7,10}


def create(out,project,queue,cpus,memory_per_cpu,storage,config=None,preset=False):
    for value in (project,queue,storage):
        if not re.fullmatch(r'[A-Za-z0-9_+/-]+',value):raise ValueError('Invalid PBS resource value')
    if cpus<1 or memory_per_cpu<1:raise ValueError('Positive CPU and memory values required')
    out.mkdir(parents=True,exist_ok=preset)
    root=Path(__file__).resolve().parent
    for step,name in NAMES.items():
        heavy=step in HEAVY;n=cpus if heavy else 1;mem=n*memory_per_cpu if heavy else 8
        text=f'''#!/bin/bash
#PBS -N mt_l0_{step:02d}
#PBS -P {project}
#PBS -q {queue}
#PBS -l ncpus={n}
#PBS -l mem={mem}GB
#PBS -l walltime={'08:00:00' if heavy else '02:00:00'}
#PBS -l jobfs=1GB
#PBS -l storage={storage}
#PBS -l wd
#PBS -j oe
set -eo pipefail
'''
        if config:text+='export CONFIG='+shlex.quote(str(config.resolve()))+'\n'
        if preset:text+='exec bash "${SCRIPT_DIR:-${PBS_O_WORKDIR:?Submit from the package directory}}/pbs_run.sh" '+str(step)+'\n'
        else:text+='exec bash '+shlex.quote(str(root/'pbs_run.sh'))+' '+str(step)+'\n'
        path=out/f'{step:02d}_{name}.sh'
        with path.open('x',encoding='utf-8',newline='\n') as f:f.write(text)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--project',required=True);p.add_argument('--queue',default='normalsr')
    p.add_argument('--cpus',type=int,required=True);p.add_argument('--memory-per-cpu',type=int,default=4)
    p.add_argument('--storage',required=True);p.add_argument('--config',type=Path,required=True)
    create(**vars(p.parse_args()))
