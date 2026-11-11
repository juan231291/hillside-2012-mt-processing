#!/bin/bash
#PBS -P tq84
#PBS -q normal
#PBS -N hs_l1_metadata
#PBS -l ncpus=1
#PBS -l mem=4GB
#PBS -l walltime=04:00:00
#PBS -l jobfs=100MB
#PBS -l storage=gdata/tq84+gdata/up99
#PBS -j oe
#PBS -l wd
set -euo pipefail
if [[ -n "${PBS_O_WORKDIR:-}" ]]; then
    cd -- "$PBS_O_WORKDIR"
else
    cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
fi
module use /g/data/up99/modulefiles
module load NCI-geophys/26.03
export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
sha256sum --quiet -c SHA256SUMS.txt
python3 update_provenance.py \
    --input-dir /g/data/tq84/jh7824/hillside_workflow/Hillside_all_level0_v3/level1/11_mth5_v5_20260921_200mv_1000hz \
    --output-dir /g/data/tq84/jh7824/hillside_workflow/Hillside_all_level0_v3/level1/11_mth5_v5_20260921_200mv_native_provenance_v3_1000hz \
    --creator 'Juan Herrera' "$@"
