# Code provenance and executed changes

The original Level 0 pipeline exported combined, headered ASCII run files. These were used to create station MTH5 files before the October 2026 publication-format correction.

Verified later changes from user-supplied PBS logs:

- **24 October 2026**, job `182176483.gadi-pbs`: ASCII split, 72 stations, 207 runs, 1,035 component files; final structure PASS; exit 0.
- **24 October 2026**, job `182182112.gadi-pbs`: HSSL03 ASCII split, 3 runs, 14 component files; final structure PASS; exit 0. HSSL03 run 0003 lacks EY.
- Level 0 MTH5: 73 original `.mth5` files passed `h5dump -H` using HDF5 1.12.2. Filenames subsequently changed to `.h5`; data contents not regenerated.
- Level 1: no resampling, and final directory naming omits `Resampled`.
- Level 2: generated MTH5 from pre-existing EDI transfer functions; original EDI processing and later MTH5 packaging require separate attribution.

Archive ZIP packaging and other final processing scripts still await execution-provenance reconciliation. Historical code will be kept distinct from October corrections. Never replace an as-executed script with a proposed future version while describing it as original.
