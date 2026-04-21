# cci-sst-dl

Minimal standalone script to mirror ESA CCI SST v3.0 L4 daily NetCDFs from CEDA into your own S3 bucket, clipped to a region of interest. Intended as a companion to [wrf-auto-runs](https://github.com/envlib/wrf-auto-runs) when using CCI SST as the boundary SST source.

Sibling in spirit to [era5-download](https://github.com/envlib/era5-download), but deliberately much smaller: one script, argparse CLI, no Python package.

## What it does

For each date in the requested range:

1. Downloads the global daily NetCDF (~16 MB) from CEDA anonymously over HTTP.
2. Clips to a user-specified lat/lon bbox with `ncks` (NCO).
3. Uploads the clipped file to an rclone-configured S3 (or local) destination, preserving the original filename and placing it under `{remote.path}/{YYYY}/`.

Re-runs are idempotent: before touching CEDA, the script checks the destination with `rclone lsf` and skips any day already present. Set `check_target = false` in the TOML (or pass `--no-check-target`) to force re-download and re-upload — useful when you change the bbox and want to refresh existing files.

## Data source

ESA Sea Surface Temperature Climate Change Initiative (ESA SST_cci) Level 4 Analysis, version 3.0, hosted by CEDA:

- **Base URL**: https://dap.ceda.ac.uk/neodc/eocis/data/global_and_regional/sea_surface_temperature/CDR_v3/Analysis/L4/v3.0.1
- **Coverage**: 1980-01-01 to ~2024-06-22 (the archive has not been updated since mid-2024 despite the catalogue claiming daily ICDR updates)
- **Grid**: 0.05° global lat-lon
- **Variables**: `analysed_sst` (K), `sea_ice_fraction`, `mask`, `analysis_uncertainty`
- **Filename**: `YYYYMMDD120000-ESACCI-L4_GHRSST-SSTdepth-OSTIA-GLOB_{CDR3.0|ICDR3.0}-v02.0-fv01.0.nc` — `CDR3.0` for years ≤ 2021, `ICDR3.0` for 2022+
- **License**: CC-BY 4.0 (cite Merchant et al. 2019, doi:[10.1038/s41597-019-0236-x](https://doi.org/10.1038/s41597-019-0236-x))

## Prerequisites

External binaries on `PATH`:

- `curl` — downloading from CEDA
- `ncks` (part of [NCO](https://nco.sourceforge.net/)) — spatial clipping
- `rclone` — upload to the destination

Python ≥ 3.10 with `pendulum`. On 3.10 also install `tomli`; on 3.11+ the stdlib's `tomllib` is used:

```bash
pip install pendulum
# if Python < 3.11:
pip install tomli
```

## Usage

```bash
cp parameters_example.toml parameters.toml
# edit parameters.toml
python cci_sst_download.py parameters.toml
```

CLI overrides for the TOML values:

```bash
python cci_sst_download.py parameters.toml \
    -s 2023-06-01 -e 2023-06-30 \
    --min-lon 145 --max-lon 195 --min-lat -60 --max-lat -20 \
    -n 8

# Force re-download + re-upload even if files already exist on the mirror
# (e.g. after changing the bbox).
python cci_sst_download.py parameters.toml --no-check-target
```

See `python cci_sst_download.py --help`.

## TOML fields

| Section | Key | Type | Description |
|---------|-----|------|-------------|
| (root) | `n_tasks` | int | Parallel workers. Default 4. |
| (root) | `check_target` | bool | Skip days already present on the destination. Default `true`. Set `false` to overwrite (e.g. after changing bbox). CLI: `--check-target` / `--no-check-target`. |
| `[dates]` | `start_date` | string | `YYYY-MM-DD`. Inclusive. |
| `[dates]` | `end_date` | string | `YYYY-MM-DD`. Inclusive. |
| `[bounds]` | `min_lon`/`max_lon` | float | Longitude bbox (-180..180). |
| `[bounds]` | `min_lat`/`max_lat` | float | Latitude bbox (-90..90). |
| `[remote]` | various | - | rclone config. `type`, `provider`, `endpoint`, `access_key_id`, `secret_access_key`, `path` for S3. Or `type='local'` + `path` for a local directory. |

## Output layout on the destination

```
{remote.path}/
├── 2023/
│   ├── 20230201120000-ESACCI-L4_GHRSST-SSTdepth-OSTIA-GLOB_ICDR3.0-v02.0-fv01.0.nc
│   ├── 20230202120000-ESACCI-L4_GHRSST-SSTdepth-OSTIA-GLOB_ICDR3.0-v02.0-fv01.0.nc
│   └── ...
├── 2024/
│   └── ...
```

Preserving original filenames keeps scientific provenance visible (`CDR3.0` vs `ICDR3.0`, version string, etc.).

## Docker

Optional. Build once and run anywhere:

```bash
docker compose build
# Export your host UID/GID so files written to mounted volumes are owned by you.
# (Only matters when [remote] uses type='local'; S3 targets don't care.)
export UID=$(id -u) GID=$(id -g)
docker compose run --rm cci-sst-dl
```

The image bundles `curl`, `nco` (for `ncks`), and `rclone`. The `user:` directive in `docker-compose.yml` defaults to `1000:1000` if `UID`/`GID` are not exported — override by exporting them in your shell first (as above).

For `[remote]` type='local', uncomment the `~/data/sst/cci_v3:/data/output` volume mount in `docker-compose.yml` (and set `path = '/data/output'` in your TOML) so the clipped NetCDFs land on your host filesystem owned by your user.

## Notes

- CEDA serves anonymously over HTTPS; no login needed.
- The script tries `CDR3.0` first for years ≤ 2021 and `ICDR3.0` for 2022+, falling back to the other on 404 (robust to future boundary shifts).
- If a date is outside the archive's coverage (e.g. post-2024-06), the script logs the failure for that date and continues, exiting non-zero at the end.
- Clipped file sizes depend on bbox; a ~30°×30° region is typically ~300-500 KB vs 16 MB unclipped.
