# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`cci-sst-dl` mirrors ESA CCI SST v3.0 L4 daily NetCDFs from CEDA into an rclone-configured destination (S3 or local), clipped to a regional bbox. Intended as a companion to `wrf-auto-runs` when CCI SST is used as the boundary SST source (selected via `[sst].source = 'cci'` in that project's TOML).

Deliberately minimal: **single-file script**, argparse CLI, no Python package install required. Sibling in spirit to `era5-download` but much smaller. Sole config input is a TOML file (see `parameters_example.toml`); `parameters.toml` is gitignored.

## Project Layout

```
cci-sst-dl/
├── cci_sst_dl/
│   ├── cci_sst_download.py     ← the entry-point script (single source of truth)
│   ├── __init__.py             ← empty placeholder
│   └── tests/                  ← placeholder; no tests written yet
├── parameters_example.toml     ← copy to parameters.toml and edit
├── parameters.toml             ← gitignored
├── Dockerfile                  ← bundles cdo + rclone, ENTRYPOINT runs the script
├── docker-compose.yml          ← passes parameters.toml at /parameters.toml inside container
├── conda/                      ← placeholder; not currently used
├── docs/, mkdocs.yml           ← placeholder; not currently published
└── pyproject.toml              ← deps = [], no install needed
```

The `cci_sst_dl/` package dir, `conda/`, mkdocs scaffolding, and `tests/` are kept as placeholders in case this tool is later graduated into a proper Python package. For now, **only `cci_sst_download.py` is real**. Don't treat it as a packaged module until told otherwise.

## Per-Day Pipeline

`main()` → for each date (concurrent via `ThreadPoolExecutor`, `n_tasks` workers) → `process_one_day(date)`:

1. `remote_has_day(date)` — `rclone lsf` against destination; if `check_target=true` and a file exists, return `'skip'`. (Filename-only check — does not validate size or integrity.)
2. `download_day(date)` — `rclone copyto <source>:<path>`, sources in `sources_for_date` order (`jasmin` first from `JASMIN_FROM` = 2024-06-23, `ceda` first before), and within each the year-appropriate variant (CDR for ≤2021, ICDR for ≥2022) then the other. Wrapped in a Python-level **exponential-backoff retry loop** only when *every* source and variant fails in a round (a 404 from the source that does not hold the date must never trigger a sleep) (transient CEDA "directory not found" handling; see *Failure Handling* below).
3. `clip_file(src, dst)` — `cdo sellonlatbox` to the requested bbox. Uses `cdo` (not `ncks`) because cdo handles dateline-wrapping bboxes (e.g. lon 145..195 on a -180..180 source) and produces a contiguous monotonic lon array.
4. `upload_file(clip_path, date)` — `rclone copy` to `<remote.path>/<YYYY>/<original_filename>`. Original filename is preserved so the CDR/ICDR provenance stays visible.

After the worker pool drains, `main()` writes `failures.txt` (one date per line, empty if all succeeded), prints the summary, and `sys.exit(1)` if any failures remain.

## Failure Handling

Two independent layers — both are user-tunable:

**1. In-run retries on the download** (`download_day`). When every source and variant (CEDA and JASMIN, CDR and ICDR) fails in one round, sleep with exponential backoff and retry them all. Defaults: `max_retries=3`, `retry_initial_delay=30`, `retry_backoff=2.0` → base delays 30s, 60s, 120s, with **±50% uniform jitter** applied per worker (the actual sleep is `int(base * (0.5 + random.random()))`). The jitter is load-bearing: without it, eight parallel workers all wake at the exact same instant and re-trigger CEDA's rate limiter as a thundering herd. Common-case success (CDR works first try) has zero overhead. rclone's own `--retries 3 --low-level-retries 5` flags only handle transient *connection* errors within a single invocation — a 404 is treated as permanent and gives up immediately, so this outer Python loop is what catches the CEDA backend hiccups.

**2. Post-run failures-list resume.** At end of run, write `failures.txt` (default location: `<parameters.toml dir>/failures.txt`) with surviving failure dates. On a follow-up invocation pass `--from-failures path/to/failures.txt` to re-process **only** those dates, bypassing the `[dates]` range entirely. The file is overwritten on every run, so iterating until it's empty converges to a complete archive without re-processing successful dates.

The file is **always written** (empty if nothing failed) so downstream automation can read it unconditionally.

**Implementation note:** `failures.txt` MUST be written *after* the `with tempfile.TemporaryDirectory(...)` block exits (the workdir is wiped on context exit). The default *Python* location is `<parameters.toml dir>/failures.txt`. In the Docker setup the Dockerfile ENTRYPOINT explicitly passes `--failures-file /work/failures.txt` and docker-compose mounts `./:/work`, so the file lands in the host project dir alongside `parameters.toml`. Without that explicit pin, the default would resolve to `/failures.txt` inside the container (because parameters.toml is bind-mounted at `/parameters.toml` — and `/` is not a mounted volume), and the file would vanish on container exit.

## Commands

```bash
# Local run (no Docker — Python ≥3.10 with pendulum, plus external rclone + cdo)
pip install pendulum                            # + tomli if Python 3.10
python cci_sst_dl/cci_sst_download.py parameters.toml

# Docker run (bundles rclone + cdo)
docker compose build
export UID=$(id -u) GID=$(id -g)               # so output files are owned by you
docker compose run --rm cci-sst-dl

# Re-run only the failures from a previous run.
# docker-compose.yml mounts the host project dir at /work (rw) and the Dockerfile
# ENTRYPOINT already passes `--failures-file /work/failures.txt`, so failures.txt
# lands on the host next to parameters.toml automatically. To consume it:
docker compose run --rm cci-sst-dl --from-failures /work/failures.txt

# Force re-download even if destination already has the file
python cci_sst_dl/cci_sst_download.py parameters.toml --no-check-target
```

CLI flags shadow corresponding TOML keys (CLI overrides TOML overrides hardcoded default). See `python cci_sst_dl/cci_sst_download.py --help` for the full list and `parameters_example.toml` for the TOML schema.

## Key Constraints

- **External binaries required**: `rclone` (all network I/O) and `cdo` (spatial clip). Both are in the Docker image; for local runs they must be on `PATH`.
- **Python 3.10+** with `pendulum`. On 3.10 also needs `tomli`; on 3.11+ uses the stdlib `tomllib`.
- **Anonymous CEDA and JASMIN access** — no auth needed; rclone has the `ceda:` and `jasmin:` remotes pre-configured via `create_rclone_config`. (The `[remote]` in TOML is the *destination*; the sources are hardcoded in `CEDA_BASE` and `JASMIN_BASE`.)
- **CDR/ICDR split at year 2021/2022.** The boundary is in `CDR_LAST_YEAR = 2021`. If CEDA shifts the boundary (e.g. extends CDR coverage), update this constant; the year-aware variant selection in `expected_variants_for_year` reads it.
- **The ICDR moved off CEDA (found 2026-10-27).** CEDA's tree ends 2024-06-22; the ICDR extension continues from 2024-06-23 in the public JASMIN workspace (`JASMIN_BASE`), same layout and filenames, files identical to what CEDA served. Dates beyond the ICDR's latest day (about a month behind present) exhaust retries and land in `failures.txt` like any other failure.

## What NOT to Do

- Don't add Python dependencies casually — the deliberate design is `dependencies = []` in `pyproject.toml`. Anything new should justify its weight.
- Don't restructure into a real Python package yet (move modules into `cci_sst_dl/`, add a console_script entry, etc.) without explicit go-ahead. The placeholder structure is intentional.
- Don't add an `--ignore-failures` flag or change the `sys.exit(1)`-on-any-failure behavior. The non-zero exit is what makes the failures-list workflow viable for orchestrators.
- Don't widen the retry to cover `upload_file` or `remote_has_day` without asking — different failure modes (Mega S3 outages vs CEDA hiccups) and the user hasn't asked for it.
