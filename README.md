# Hillside 2012 — MT processing code

Python and PBS scripts used to prepare the Adelaide University Hillside 2012 magnetotelluric (MT) survey for AuScope/NCI archiving.

## Processing folders

| Folder | What it does |
|---|---|
| [Level 0 — 01_workflow](src/level_0/01_workflow) | Inventories recordings, selects runs, generates ASCII and metadata, then creates Level 0 MTH5 files. |
| [Level 0 — 02_metadata_update](src/level_0/02_metadata_update) | Applies the final Hillside metadata corrections and produces the updated Level 0 release. |
| [Level 0 — 03_ascii_split](src/level_0/03_ascii_split) | Separates combined ASCII runs into header-free EX, EY, BX, BY and BZ files for publication. |
| [Level 1](src/level_1) | Calibrates electric and magnetic time series and applies documented polarity corrections. |
| [Level 2](src/level_2) | Imports previously processed EDI transfer functions into MTH5, without recalculating them. |
| [Metadata provenance](src/metadata_provenance) | Updates MTH5 metadata and processing provenance. |
| [Packed raw](src/packed_raw) | Packages original recordings in per-station ZIP archives. |

**Level 0 order:** 01_workflow → 02_metadata_update → 03_ascii_split. The ASCII split occurred **after** Level 0 MTH5 creation; it should not be applied beforehand.

## How to use the code

The repository contains processing scripts, **not the original datasets**. The original routines were written for NCI Gadi and include paths, PBS project settings and processing records specific to Hillside. Adapt these for another installation or survey.

### Level 0

**Inputs:** original station recordings, acquisition metadata workbook, selection/ownership rules and a configured output directory.

1. In `src/level_0/01_workflow`, copy and edit `configs/hillside.json` (particularly `raw_root`, `metadata_workbook`, `batch_dir`).
2. Use the dependencies in `requirements.txt` and `requirements-mpi.txt` or an equivalent NCI software environment.
3. Execute the numbered stages in order with the matching PBS scripts.

```bash
cd src/level_0/01_workflow
python3 workflow.py plan --config configs/my_survey.json

# On Gadi, after adapting PBS resources and CONFIG:
qsub -v CONFIG="$PWD/configs/my_survey.json" 01_inventory.sh
# After each stage completes, continue with 02_duplicates.sh ... 10_mth5.sh.
```

The Level 0 metadata update is a **Hillside-specific follow-up** to the original workflow. The ASCII splitting scripts also refer to historical Gadi storage paths and modify existing files; they are not general-purpose first-run commands.

### Level 1

**Inputs:** the completed Hillside Level 0 working batch (station MTH5 files, per-station JSON metadata and release records). `config.json` specifies the input batch and processing settings. The magnetic response file `l120new.rsp` is included.

```bash
cd src/level_1
python3 level1.py preflight --config config.json
```

The historical Gadi procedure uses `pilot.pbs`, `batch.pbs` and `submit.sh`: a pilot station precedes the complete survey. **The original pilot launcher references an omitted companion file (`test_level1.py`), so the supplied submission chain cannot run unchanged from this checkout.** Its paths and source-release assumptions also need to match the original working batch.

This Hillside release uses **200 mV/nT** magnetic sensitivity and retains **1,000 Hz**, without resampling or geographic rotation.

### Level 2

**Inputs:** the existing EDI transfer functions, `sources/processing_manifest.json`, and the original processing workbook referenced by that manifest. The EDI paths recorded in the manifest must resolve relative to `src/level_2/`. **These source files are intentionally not included in GitHub.**

After arranging the source inputs in their documented layout and adapting the working batch path:

```bash
cd src/level_2
python3 level2.py --batch /path/to/Hillside_working_batch
```

On Gadi, the original PBS entry point is `submit.sh` (which submits `level2.pbs`). This imports **71 existing impedance transfer functions**; HS055 and HS078 have no retained Level 2 EDI response. It does **not** reprocess time series or calculate new transfer functions.

## Additional notes

- **HSSL03 run 0003** contains EX, BX, BY and BZ; EY is absent.
- The original Level 2 EDI responses were processed by **Relly Margiono (2025)**, Adelaide University. The 2026 scripts organise those responses into MTH5.
- The production scripts originally wrote `_L0.mth5`, `_L1.mth5` and `_L2.mth5` filenames; the publication copies were subsequently renamed to `STATION.h5` without regenerating their data.
- Raw recordings, EDI files, processed HDF5/MTH5 datasets and processing workbooks are not part of this source-code repository.

This repository is currently **private**, pending agreement on source-code credit, ownership and licensing.
