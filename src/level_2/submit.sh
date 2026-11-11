#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
sha256sum --quiet -c SHA256SUMS.txt
if [[ -f submitted_jobs.txt ]]; then
    echo 'This package already has a submission record. Check that job before submitting again.' >&2
    cat submitted_jobs.txt
    exit 1
fi
job=$(qsub level2.pbs)
printf 'level2=%s\n' "$job" > submitted_jobs.txt
echo "Level 2 import: $job"
