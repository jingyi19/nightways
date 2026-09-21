# Backend development

Run `python backend/setup_gisco.py` once to install GISCO LAU 2024 at
`data/geo/lau-2024.gpkg`. The file is excluded from Git; set
`NIGHTWAYS_GISCO_LAU_PATH` to override its location. It is required for the
internal arbitrary-origin destination-normalization path.

Run `python backend/setup_gb_bua.py` to install the pinned OS Open Built Up
Areas 2026-04 artifact at `data/geo/nightways-gb-bua-2026-04-v1.gpkg`.
Set `NIGHTWAYS_GB_BUA_PATH` (or pass `--path`) to choose another location;
`--force` replaces an existing file only after a new copy passes validation.
The installer uses the fixed GitHub Release asset and checks its SHA-256,
GeoPackage schema, spatial index, London membership, and seven station points.
This dataset is not wired into runtime locality resolution yet.
