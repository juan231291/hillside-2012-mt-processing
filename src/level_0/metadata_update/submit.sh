#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
sha256sum --quiet -c SHA256SUMS.txt
if [[ -e submitted_jobs.txt ]]; then
  echo 'Jobs were already submitted. Inspect submitted_jobs.txt and qstat; do not submit twice.' >&2
  exit 1
fi
metadata_job=$(qsub 01_metadata.pbs)
printf 'Metadata job: %s\n' "$metadata_job"
# Record the first submission immediately, including if the dependent submission fails.
printf 'Metadata job: %s\n' "$metadata_job" > submitted_jobs.txt
if ! mth5_job=$(qsub -W "depend=afterok:$metadata_job" 02_mth5.pbs); then
  echo 'MTH5 submission failed. The metadata job above is still submitted; do not resubmit both jobs.' >&2
  echo "After resolving the submission error, use: qsub -W depend=afterok:$metadata_job 02_mth5.pbs" >&2
  exit 1
fi
printf 'MTH5 job: %s (starts only after metadata succeeds)\n' "$mth5_job" | tee -a submitted_jobs.txt
printf 'Check progress with: qstat -u jh7824\n'
