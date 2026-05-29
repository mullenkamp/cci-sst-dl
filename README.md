# cci-sst-dl

Minimal standalone script to mirror ESA CCI SST v3.0 L4 daily NetCDFs from CEDA into your own S3 bucket, clipped to a region of interest. Intended as a companion to [wrf-auto-runs](https://github.com/envlib/wrf-auto-runs) when using CCI SST as the boundary SST source.

Sibling in spirit to [era5-download](https://github.com/envlib/era5-download), but deliberately much smaller: one script, argparse CLI, no Python package.

## What it does

For each date in the requested range:

1. Downloads the global daily NetCDF (~16 MB) from CEDA anonymously over HTTP.
2. Clips to a user-specified lat/lon bbox with `ncks` (NCO).
3. Uploads the clipped file to an rclone-configured S3 (or local) destination, preserving the original filename and placing it under `{remote.path}/{YYYY}/`.

Re-runs are idempotent: before touching CEDA, the script checks the destination with `rclone lsf` and skips any day already present. Set `check_target = false` in the TOML (or pass `--no-check-target`) to force re-download and re-upload — useful when you change the bbox and want to refresh existing files.

### Handling transient CEDA failures

CEDA occasionally returns `error reading source root directory: error listing "": directory not found` for files that actually exist — backend hiccups that resolve themselves within minutes-to-hours. The script handles these in two ways:

1. **Per-date exponential-backoff retries during the run.** If *both* the CDR and ICDR variants fail for a date, the script sleeps and tries the pair again. Defaults: 3 retries, 30 s initial delay, 2.0× backoff → base delays of 30 s, 60 s, 120 s, with ±50% uniform jitter applied per worker (so eight parallel workers don't all wake at the same instant and re-trigger CEDA's rate limiter). Configurable via `max_retries` / `retry_initial_delay` / `retry_backoff` in TOML or the matching `--max-retries` / `--retry-initial-delay` / `--retry-backoff` CLI flags. The common-case success path (CDR succeeds first try) has zero overhead — no sleeps, no extra log lines.

2. **Failures-list file for surgical re-runs.** At end of run the script writes a one-date-per-line file (default: `failures.txt` alongside `parameters.toml`) listing dates that still failed after retries. Re-run the script with `--from-failures path/to/failures.txt` to re-process **only** those dates, skipping the `[dates]` range entirely. The file is overwritten on every run so it always reflects the latest state; iterating until it's empty converges to a complete archive without re-downloading any successful days.

   ```bash
   # First run — full range
   python cci_sst_download.py parameters.toml
   # → writes ./failures.txt with whatever didn't recover after retries

   # Re-run later (CEDA usually recovers within hours)
   python cci_sst_download.py parameters.toml --from-failures ./failures.txt
   # → overwrites ./failures.txt with what STILL failed; ideally empty
   ```

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

- `rclone` — downloads from CEDA and uploads to the destination (single tool for all network I/O; respects standard `HTTPS_PROXY` env and also reads `~/.config/rclone/rclone.conf` for any globally-configured proxy)
- `cdo` ([Climate Data Operators](https://code.mpimet.mpg.de/projects/cdo)) — spatial clipping via `sellonlatbox`. Unlike `ncks`, `cdo` handles dateline-wrapping bboxes (e.g. `max_lon=195` on a `-180..180` source) and produces a contiguous monotonic lon array.

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
| (root) | `max_retries` | int | Number of retries per date when both CEDA variants fail. Default 3. CLI: `--max-retries`. |
| (root) | `retry_initial_delay` | int | Seconds to wait before the first retry. Default 30. CLI: `--retry-initial-delay`. |
| (root) | `retry_backoff` | float | Multiplier applied to the delay after each retry. Default 2.0. CLI: `--retry-backoff`. |
| (root) | `failures_file` | string | Path to write the post-run failures list (one `YYYY-MM-DD` per line, empty if all succeeded). Default `<parameters.toml dir>/failures.txt`. CLI: `--failures-file`. |
| `[dates]` | `start_date` | string | `YYYY-MM-DD`. Inclusive. Ignored when `--from-failures` is given. |
| `[dates]` | `end_date` | string | `YYYY-MM-DD`. Inclusive. Ignored when `--from-failures` is given. |
| `[bounds]` | `min_lon`/`max_lon` | float | Longitude bbox (-180..180). |
| `[bounds]` | `min_lat`/`max_lat` | float | Latitude bbox (-90..90). |
| `[remote]` | various | - | rclone config. `type`, `provider`, `endpoint`, `access_key_id`, `secret_access_key`, `path` for S3. Or `type='local'` + `path` for a local directory. |

CLI-only flags (no TOML equivalent):

- `--from-failures PATH` — read input dates from a one-`YYYY-MM-DD`-per-line file instead of using `[dates].start_date/end_date`. Blank lines and lines starting with `#` are ignored. Pair with a previous run's `failures.txt` for surgical re-runs.

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

The image bundles `cdo` and `rclone`. The `user:` directive in `docker-compose.yml` defaults to `1000:1000` if `UID`/`GID` are not exported — override by exporting them in your shell first (as above).

For `[remote]` type='local', uncomment the `~/data/sst/cci_v3:/data/output` volume mount in `docker-compose.yml` (and set `path = '/data/output'` in your TOML) so the clipped NetCDFs land on your host filesystem owned by your user.

The host project dir is mounted at `/work` and the entry-point passes `--failures-file /work/failures.txt`, so the post-run failures list appears in the host project dir alongside `parameters.toml` automatically. To re-run only those failures:

```bash
docker compose run --rm cci-sst-dl --from-failures /work/failures.txt
```

### Behind an HTTP proxy (university / corporate networks)

`docker-compose.yml` already forwards `HTTP_PROXY`, `HTTPS_PROXY`, and `NO_PROXY` (plus lowercase variants) from the host. Just export them before running:

```bash
export HTTPS_PROXY=http://proxy.example.ac.nz:8080
export HTTP_PROXY=http://proxy.example.ac.nz:8080
export NO_PROXY=localhost,127.0.0.1,.internal.example.ac.nz
docker compose run --rm cci-sst-dl
```

Both `curl` (CEDA download) and `rclone` (Mega upload) respect these. If your proxy uses a self-signed CA, you'll also need to add its cert to the container — ask if you hit that.

## Notes

- CEDA serves anonymously over HTTPS; no login needed.
- The script tries `CDR3.0` first for years ≤ 2021 and `ICDR3.0` for 2022+, falling back to the other on 404 (robust to future boundary shifts).
- Genuinely-missing dates (e.g. post-2024-06) will still fail after retries are exhausted — they end up in `failures.txt` like any other failure, and the run exits non-zero. Inspect `failures.txt` to distinguish "CEDA was flaky, try again later" from "this date doesn't exist".
- Clipped file sizes depend on bbox; a ~30°×30° region is typically ~300-500 KB vs 16 MB unclipped.
