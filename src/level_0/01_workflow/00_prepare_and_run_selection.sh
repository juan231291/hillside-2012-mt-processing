#!/bin/bash
#PBS -N mt_l0_select
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
cd "${PBS_O_WORKDIR:?Submit from the v1.2 package directory}"
sha256sum --quiet -c CODE_SHA256SUMS.txt
module use /g/data/up99/modulefiles
module load NCI-geophys/26.03
set -u
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1
export PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
python3 prepare_selection_batch.py \
    --previous-package ../mt_level0_workflow_v1_1 \
    --previous-config ../mt_level0_workflow_v1_1/configs/hillside.json \
    --config configs/hillside.json --run-selection --revision "${REVISION:-v1}" \
    --expectations configs/hillside_selection_expectations.json
