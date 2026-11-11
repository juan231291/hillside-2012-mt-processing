#!/usr/bin/env python3
"""Reuse verified stages 01-05, then run the accepted Step 06 selection."""
import argparse
from pathlib import Path
import sys
PACKAGE=Path(__file__).resolve().parent
sys.path[:0]=[str(PACKAGE),str(PACKAGE/'engine')]
import workflow as wf
from qc_batch_reuse import reuse_qc,check_selection

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--previous-package',type=Path,required=True)
    p.add_argument('--previous-config',type=Path,required=True)
    p.add_argument('--config',type=Path,default=PACKAGE/'configs/hillside.json')
    p.add_argument('--run-selection',action='store_true')
    p.add_argument('--revision',default='v1')
    p.add_argument('--expectations',type=Path)
    a=p.parse_args()
    c,cp=reuse_qc(wf,a.previous_package,a.previous_config,a.config)
    if a.run_selection:
        wf.run_stage(c,cp,6,a.revision,1)
        if a.expectations:check_selection(wf,c,a.expectations)

if __name__=='__main__':main()
