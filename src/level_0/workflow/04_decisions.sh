#!/bin/bash
#PBS -N mt_l0_04
#PBS -P tq84
#PBS -q normalsr
#PBS -l ncpus=1
#PBS -l mem=8GB
#PBS -l walltime=02:00:00
#PBS -l jobfs=1GB
#PBS -l storage=gdata/tq84+gdata/up99
#PBS -l wd
#PBS -j oe
set -eo pipefail
exec bash "${SCRIPT_DIR:-${PBS_O_WORKDIR:?Submit from the package directory}}/pbs_run.sh" 4
