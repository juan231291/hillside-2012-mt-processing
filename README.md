# Hillside 2012 — magnetotelluric processing and archiving

> **Private review repository — incomplete source recovery. Not a public software release.**

This repository documents the historical processing, quality control, metadata preparation and NCI/AuScope archive preparation for Adelaide University's **Hillside 2012 magnetotelluric survey**.

The purpose is to preserve the scripts **as actually used**, document later publication-format corrections separately, and make the complete processing provenance auditable. Dataset publication is separate from software publication.

## Processing history

- **Packed raw:** original recordings grouped by station into ZIP archives.
- **Level 0 ASCII:** original processing selected runs and generated combined five-column, headered ASCII files. On **24 October 2026**, the staged files were split *after MTH5 creation* into one header-free file per run and available component (`.EX`, `.EY`, `.BX`, `.BY`, `.BZ`).
- **Level 0 MTH5:** station files generated from the original processing workflow; publication filenames changed from `STATION_L0.mth5` to `STATION.h5`. This is a rename, not regeneration.
- **Level 1 MTH5:** the Hillside product was **not resampled**; do not label it "Resampled." Final production scripts are still being recovered for the repository.
- **Level 2:** MTH5 files were generated using **previously processed EDI transfer functions**, not by reprocessing raw time series. Original EDI processing attribution must be preserved.

The confirmed ASCII job logs report **73 stations, 210 runs, 1,049 separate channel files**, including **HSSL03/run_0003**, which has EX, BX, BY and BZ, but **no EY**. This must not be filled with invented data.

## Final reported NCI staging layout

```text
Hillside_2012/
├── Packed_Raw_Time_Series_Archive/
├── level_0/
│   ├── Concatenated_Time_Series_ASCII_per_run/
│   └── Concatenated_Time_Series_MTH5_per_run/
├── level_1/
│   └── Concatenated_Rotated_Time_Series_MTH5/
└── level_2/
    ├── MTH5/Adelaide_University/
    └── EDI/Adelaide_University/
        └── Metadata/
```

Level 0 per-day ASCII was not produced for this survey. The directory layout above is the operator-reported final staging layout; it is **not** independent verification of NCI catalogue publication.

## Repository status

This is a **review-stage scaffold**. The complete Level 0 source, final Level 1 and Level 2 workflows, archive-organisation scripts, provenance updates, tests and actual PBS execution artefacts still need to be reconciled and included. See [missing source packages](docs/MISSING_FILES.md) and [code provenance](docs/CODE_PROVENANCE.md).

Raw recordings, time-series datasets, EDI files, MTH5/HDF5 datasets, individual station metadata and sensitive material are **not** intended to be committed to GitHub.

## Publication and attribution

No public licence or software DOI is asserted. Permission and contributor credit for original code must be reviewed with relevant Adelaide University, NCI and AuScope collaborators before any public release. See [publication checklist](docs/PUBLISHING.md).
