# Hillside 2012 — MT processing code

Python and PBS scripts used to prepare Adelaide University's Hillside 2012 magnetotelluric (MT) survey for the AuScope/NCI data collection.

## What's in this repository?

| Folder | Purpose |
|---|---|
| [01_workflow](src/level_0/01_workflow) | Level 0: raw-file inventory, recording selection, ASCII time series, metadata and MTH5 creation (10 stages). |
| [02_metadata_update](src/level_0/02_metadata_update) | Hillside Level 0 metadata corrections and updated MTH5 products. |
| [03_ascii_split](src/level_0/03_ascii_split) | Final Level 0 ASCII format: one header-free file per run and available component (EX, EY, BX, BY, BZ). |
| [level_1](src/level_1) | Field calibration and polarity corrections; retains 1,000 Hz without resampling or geographic rotation. |
| [level_2](src/level_2) | Converts existing EDI transfer functions to MTH5 without recalculating them. |
| [metadata_provenance](src/metadata_provenance) | Updates MTH5 metadata and provenance. |
| [packed_raw](src/packed_raw) | Packs original recordings into one ZIP archive per station. |

**Level 0 order:** 01_workflow → 02_metadata_update → 03_ascii_split. The ASCII split was performed **after** MTH5 generation; do not apply it before creating Level 0 MTH5 files.

## How to use the Level 0 workflow

This is the original processing code, not a one-command installer. It requires access to original recordings, the survey metadata workbook and an NCI Gadi environment (or adapted equivalents). Those datasets are **not** included.

1. Clone this repository and open `src/level_0/01_workflow/`.
2. Copy `configs/hillside.json` to your own configuration file. Update `raw_root`, `metadata_workbook`, `batch_dir` and any survey-specific rules. Keep the original file for reference.
3. Install or load the software listed in `requirements.txt` and `requirements-mpi.txt`.
4. Check the configuration, then run the numbered processing stages **in order**, using the PBS scripts on Gadi.

```bash
cd src/level_0/01_workflow
python3 workflow.py plan --config configs/my_survey.json

# After adapting PBS project settings and configuration:
qsub -v CONFIG="$PWD/configs/my_survey.json" 01_inventory.sh
# Once each job finishes, submit 02_duplicates.sh through 10_mth5.sh in sequence.
```

The September metadata-update scripts are specific to the historical Hillside processing state. The October ASCII-split PBS files refer to the original Gadi staging paths and modify existing ASCII files; **do not rerun them on other datasets unchanged**.

## Notes

- HSSL03 run 0003 has EX, BX, BY and BZ but no EY channel.
- The corrected Level 1 processing uses a magnetic sensitivity of **200 mV/nT**.
- Level 2 MTH5 files were generated from EDI responses processed in 2025 by **Relly Margiono**, Adelaide University. The transfer functions were not recalculated.
- Raw recordings, EDI files and generated MTH5/HDF5 datasets are not stored in this code repository.

The repository is currently private. Source-code attribution, ownership and licensing should be agreed before public release.
