# Hillside 2012 — Magnetotelluric Processing Code

Python and Gadi PBS workflows for the Adelaide University Hillside 2012 magnetotelluric survey, prepared for AuScope/NCI archiving.

## Processing stages

- **Level 0:** recording inventory, run selection, ASCII concatenation, metadata preparation and station MTH5 creation.
- **Level 1:** calibration and documented polarity corrections using **200 mV/nT** magnetic sensitivity, retaining the original **1,000 Hz** sampling rate without resampling or geographic rotation.
- **Level 2:** conversion of existing 2025 EDI transfer functions to MTH5, without recalculating the transfer functions. Original EDI processing: Relly Margiono.
- **Archive organisation:** per-station raw ZIP packaging, provenance updates and final NCI directory structure.
- **October corrections:** per-run ASCII split into individual header-free EX/EY/BX/BY/BZ channels; MTH5 publication filenames changed to .h5.

HSSL03 run 0003 contains only EX, BX, BY and BZ; EY is absent.

## Intended dataset layout

Packed_Raw_Time_Series_Archive; level_0/Concatenated_Time_Series_ASCII_per_run; level_0/Concatenated_Time_Series_MTH5_per_run; level_1/Concatenated_Rotated_Time_Series_MTH5; level_2/MTH5/Adelaide_University; level_2/EDI/Adelaide_University/Metadata.

## Repository status

This repository is **private**. The processing scripts have been organised separately for upload. The source code is not yet present in this GitHub repository.

Geophysical datasets, EDI files, HDF5/MTH5 outputs and station metadata workbooks are excluded. Software ownership, contributor credit and licensing should be agreed before public distribution. See docs/CODE_PROVENANCE.md and docs/PUBLISHING.md.