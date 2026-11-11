# Hillside 2012 MT processing

Python and PBS source code used to prepare the Adelaide University Hillside 2012 magnetotelluric survey for the AuScope / NCI data collection.

## Code layout

- `src/level_0/workflow/`: inventory, duplicate screening, time information, run selection, concatenated time series, metadata and MTH5 creation.
- `src/level_0/metadata_update/`: subsequent Level 0 metadata updates.
- `src/level_0/ascii_split/`: conversion of five-column per-run ASCII into separate header-free EX, EY, BX, BY and BZ files.
- `src/level_1/`: 1,000 Hz processing, electric and magnetic calibration, and channel polarity handling (corrected 200 mV/nT magnetic sensitivity).
- `src/level_2/`: conversion of pre-existing EDI transfer functions to MTH5.
- `src/metadata_provenance/`: MTH5 metadata and provenance updates.
- `src/packed_raw/`: per-station raw ZIP archive packaging.

## Processing notes

The ASCII split was performed after the initial Level 0 MTH5 creation; it does not regenerate the MTH5 products. HSSL03 run 0003 contains EX, BX, BY and BZ only; no EY observations were created. Level 1 retains 1,000 Hz with no resampling or geographic rotation.

The original Level 2 EDI transfer functions were processed in 2025 by Relly Margiono at Adelaide University. The 2026 Level 2 scripts import those existing responses into MTH5 without recalculating them.

Gadi-specific paths and settings in the original scripts refer to the working environment used for the Hillside survey. Acquisition recordings, EDI files, MTH5/HDF5 data and processed outputs are not included here.

This code is currently retained in a private repository pending agreement on source-code attribution, ownership and licensing.
