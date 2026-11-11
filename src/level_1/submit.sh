#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
sha256sum --quiet -c SHA256SUMS.txt
if [[ -f submitted_jobs.txt ]]; then
    echo 'This package already has a submission record. Check those jobs before submitting again.' >&2
    cat submitted_jobs.txt
    exit 1
fi
pilot=$(qsub pilot.pbs)
printf 'pilot=%s\n' "$pilot" > submitted_jobs.txt
echo "Pilot: $pilot"
if batch=$(qsub -W "depend=afterok:$pilot" batch.pbs); then
    printf 'batch=%s\n' "$batch" >> submitted_jobs.txt
    echo "Survey batch: $batch (starts only after the pilot succeeds)"
else
    echo 'The pilot was submitted but batch submission failed. Its job ID is saved; do not submit a second pilot.' >&2
    exit 1
fi
