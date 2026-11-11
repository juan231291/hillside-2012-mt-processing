#!/bin/bash
set -eo pipefail
STEP=${1:?Stage number required}
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$SCRIPT_DIR"
sha256sum --quiet -c CODE_SHA256SUMS.txt
module use "${MODULE_PATH:-/g/data/up99/modulefiles}"
module load "${GEOPHYS_MODULE:-NCI-geophys/26.03}"
set -u
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1
export PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
CONFIG=${CONFIG:-$SCRIPT_DIR/configs/hillside.json}
REVISION=${REVISION:-v1}
WORKERS=${WORKERS:-${PBS_NCPUS:-1}}
[[ "$WORKERS" =~ ^[1-9][0-9]*$ ]] || { echo 'Invalid WORKERS'; exit 2; }
(( WORKERS <= ${PBS_NCPUS:-1} )) || { echo 'WORKERS exceeds allocation'; exit 2; }
case "$STEP" in
  1|01|5|05|7|07|10)
    mpirun -np "${PBS_NCPUS:-1}" python3 "$SCRIPT_DIR/workflow.py" run --config "$CONFIG" \
      --step "$((10#$STEP))" --revision "$REVISION" --workers "$WORKERS" --mpi
    ;;
  *)
    python3 "$SCRIPT_DIR/workflow.py" run --config "$CONFIG" --step "$((10#$STEP))" \
      --revision "$REVISION" --workers 1
    ;;
esac
