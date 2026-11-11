# Processing-code provenance

The project code covers historical Level 0 operations, corrected Level 1 calibration, the Level 2 EDI-to-MTH5 importer, metadata/provenance updates, and archive organisation.

Originally, Level 0 exported combined five-column ASCII runs with headers. In October 2026, the publication staging files were split into individual header-free component files. This change occurred after the original MTH5 creation step, and it did not regenerate those files.

The Hillside Level 1 product retains 1,000 Hz. Its corrected source uses **200 mV/nT** magnetic sensitivity. The earlier 400 mV/nT source is superseded.

Level 2 MTH5 products were made from EDI transfer functions processed in 2025 by Relly Margiono. Those numerical transfer functions were not recalculated.

Original MTH5 files were renamed to station-specific .h5 files during archive preparation. EDI, time-series and MTH5/HDF5 datasets remain outside this code repository.