#!/bin/bash
#PBS -N mt_l0_10
#PBS -P tq84
#PBS -q normalsr
#PBS -l ncpus=73
#PBS -l mem=292GB
#PBS -l walltime=08:00:00
#PBS -l jobfs=1GB
#PBS -l storage=gdata/tq84+gdata/up99
#PBS -l wd
#PBS -j oe
set -eo pipefail
exec bash "${SCRIPT_DIR:-${PBS_O_WORKDIR:?Submit from the package directory}}/pbs_run.sh" 10
