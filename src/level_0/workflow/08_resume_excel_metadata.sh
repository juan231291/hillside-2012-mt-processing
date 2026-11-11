#!/bin/bash
#PBS -N mt_l0_08_review
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
SCRIPT_DIR=${SCRIPT_DIR:-${PBS_O_WORKDIR:?Submit from the new package directory}}
cd "$SCRIPT_DIR"
sha256sum --quiet -c CODE_SHA256SUMS.txt
module use "${MODULE_PATH:-/g/data/up99/modulefiles}"
module load "${GEOPHYS_MODULE:-NCI-geophys/26.03}"
set -u
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1
export PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
args=(--config "${CONFIG:-$SCRIPT_DIR/configs/hillside.json}" --revision "${REVISION:-v3}")
if [[ -n ${PREVIOUS_PACKAGE:-} ]]; then args+=(--previous-package "$PREVIOUS_PACKAGE"); fi
if [[ -n ${PREVIOUS_CONFIG:-} ]]; then args+=(--previous-config "$PREVIOUS_CONFIG"); fi
python3 "$SCRIPT_DIR/resume_metadata.py" "${args[@]}"
