#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Download ESA CCI SST v3.0 L4 daily NetCDFs from CEDA (and, from 2024-06-23, the
public JASMIN workspace that now holds the ICDR extension), clip each to a regional
bbox with cdo, and upload to an rclone-configured S3 (or local) destination.

Intentionally lightweight: one file, argparse-based CLI, no Python package,
no tests. Sibling in spirit to `era5-download` but much smaller.

Sources (same v3.0.1 layout and filenames; each date tries its primary source first, then the other):
  CEDA   https://dap.ceda.ac.uk/neodc/eocis/data/global_and_regional/sea_surface_temperature/CDR_v3/Analysis/L4/v3.0.1
         1980-01-01 .. 2024-06-22 (CDR to 2021, ICDR 2022 onward)
  JASMIN https://gws-access.jasmin.ac.uk/public/cds_c3s_sst/data/ICDR_v3/Analysis/L4/v3.0.1
         2024-06-23 onward (the ICDR extension; checked 2026-10-27: through 2026-10-10, files identical to
         those CEDA served for 2025)
License: CC-BY 4.0 (credit Merchant et al. 2019, doi:10.1038/s41597-019-0236-x)

External binaries required: cdo, rclone.
"""
import argparse
import concurrent.futures
import random
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

import pendulum


######################################################
### Constants

CEDA_BASE = (
    'https://dap.ceda.ac.uk/neodc/eocis/data/global_and_regional/'
    'sea_surface_temperature/CDR_v3/Analysis/L4/v3.0.1'
)

# The ICDR extension moved off CEDA: CEDA's tree ends 2024-06-22 and the public JASMIN workspace starts 2024-06-23
# (both checked 2026-10-27). Dates from JASMIN_FROM try JASMIN first, earlier dates CEDA first; each falls back to
# the other, so a later move in either direction still downloads.
JASMIN_BASE = 'https://gws-access.jasmin.ac.uk/public/cds_c3s_sst/data/ICDR_v3/Analysis/L4/v3.0.1'
JASMIN_FROM = (2024, 6, 23)

# year <= CDR_LAST_YEAR → CDR3.0, else ICDR3.0
CDR_LAST_YEAR = 2021

FILENAME_TMPL = (
    '{yyyymmdd}120000-ESACCI-L4_GHRSST-SSTdepth-OSTIA-GLOB_'
    '{variant}3.0-v02.0-fv01.0.nc'
)

# Use cdo sellonlatbox instead of ncks: handles dateline-wrapping bboxes
# (e.g. lon 145..195 on a -180..180 source produces a single contiguous lon
# array from 145.025 to 194.975). ncks silently clamps such bboxes to 180.
# -f nc4c + -z zip_3 gives NetCDF-4 with deflate compression.
CLIP_STR_FORMAT = (
    'cdo -s -O -f nc4 -z zip_3 '
    'sellonlatbox,{min_lon:.6f},{max_lon:.6f},{min_lat:.6f},{max_lat:.6f} '
    '{src} {dst}'
)


######################################################
### Helpers

def check_external_binaries():
    for bin_ in ('cdo', 'rclone'):
        if shutil.which(bin_) is None:
            print(f'Error: required external binary "{bin_}" not on PATH.', file=sys.stderr)
            sys.exit(1)


def load_params(path):
    with open(path, 'rb') as f:
        return tomllib.load(f)


def apply_overrides(params, args):
    if args.start_date:
        params.setdefault('dates', {})['start_date'] = args.start_date
    if args.end_date:
        params.setdefault('dates', {})['end_date'] = args.end_date
    if args.min_lon is not None:
        params.setdefault('bounds', {})['min_lon'] = args.min_lon
    if args.max_lon is not None:
        params.setdefault('bounds', {})['max_lon'] = args.max_lon
    if args.min_lat is not None:
        params.setdefault('bounds', {})['min_lat'] = args.min_lat
    if args.max_lat is not None:
        params.setdefault('bounds', {})['max_lat'] = args.max_lat
    if args.n_tasks is not None:
        params['n_tasks'] = args.n_tasks
    if args.check_target is not None:
        params['check_target'] = args.check_target
    if args.max_retries is not None:
        params['max_retries'] = args.max_retries
    if args.retry_initial_delay is not None:
        params['retry_initial_delay'] = args.retry_initial_delay
    if args.retry_backoff is not None:
        params['retry_backoff'] = args.retry_backoff
    if args.failures_file is not None:
        params['failures_file'] = str(args.failures_file)
    return params


def load_failures_file(path):
    """Load a list of dates from a one-date-per-line file.

    Format: one ISO date (YYYY-MM-DD) per line. Blank lines and lines whose
    first non-whitespace character is `#` are silently skipped. Returns a sorted
    list of unique pendulum.date objects.
    """
    dates = set()
    with open(path) as f:
        for lineno, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            try:
                d = pendulum.parse(line).date()
            except Exception as exc:
                raise ValueError(
                    f'Bad date on line {lineno} of {path}: {line!r} ({exc})'
                ) from exc
            dates.add(d)
    return sorted(dates)


def date_range(start_date, end_date):
    curr = pendulum.date(start_date.year, start_date.month, start_date.day)
    end = pendulum.date(end_date.year, end_date.month, end_date.day)
    while curr <= end:
        yield curr
        curr = curr.add(days=1)


def build_url(date, variant, base=CEDA_BASE):
    filename = FILENAME_TMPL.format(yyyymmdd=date.format('YYYYMMDD'), variant=variant)
    url = f'{base}/{date.format("YYYY")}/{date.format("MM")}/{date.format("DD")}/{filename}'
    return url, filename


def sources_for_date(date):
    """rclone source remotes in the order to try: the one that holds this date first."""
    if (date.year, date.month, date.day) >= JASMIN_FROM:
        return ('jasmin', 'ceda')
    return ('ceda', 'jasmin')


def create_rclone_config(config_dict, config_path):
    """Write two rclone config entries:
      - 'sst'  : the upload destination (user's S3 or local, from [remote] TOML)
      - 'ceda' : anonymous HTTP remote pointing at the CEDA v3 archive base URL
      - 'jasmin' : anonymous HTTP remote pointing at the JASMIN ICDR workspace (2024-06-23 onward)
    """
    # Upload destination
    type_ = config_dict['type']
    kv = ' '.join(f'{k}={v}' for k, v in config_dict.items() if k != 'type')
    cmd = f'rclone config create sst {type_} {kv} --config={config_path} --non-interactive'
    subprocess.run(shlex.split(cmd), capture_output=True, text=True, check=True)

    # CEDA download source (HTTP remote, no auth)
    cmd = f'rclone config create ceda http url={CEDA_BASE}/ --config={config_path} --non-interactive'
    subprocess.run(shlex.split(cmd), capture_output=True, text=True, check=True)

    # JASMIN download source for the ICDR extension (HTTP remote, no auth)
    cmd = f'rclone config create jasmin http url={JASMIN_BASE}/ --config={config_path} --non-interactive'
    subprocess.run(shlex.split(cmd), capture_output=True, text=True, check=True)


def expected_variants_for_year(year):
    primary = 'CDR' if year <= CDR_LAST_YEAR else 'ICDR'
    fallback = 'ICDR' if primary == 'CDR' else 'CDR'
    return primary, fallback


def remote_has_day(date, remote_path, config_path):
    """Return True if a file for this date already exists on the destination.

    Checks both CDR and ICDR filename variants via `rclone lsf`. If either is
    present under {remote}/{YYYY}/, the day is considered mirrored.
    """
    dst_dir = f'sst:{remote_path.rstrip("/")}/{date.format("YYYY")}/'
    cmd = ['rclone', 'lsf', dst_dir, '--config', str(config_path), '--files-only']
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        # Dir doesn't exist yet (or remote unreachable) — treat as not present
        return False
    listed = p.stdout.strip().splitlines()
    primary, fallback = expected_variants_for_year(date.year)
    for variant in (primary, fallback):
        _, filename = build_url(date, variant)
        if filename in listed:
            return True
    return False


def download_day(date, dl_dir, config_path,
                 max_retries=3, retry_initial_delay=30, retry_backoff=2.0):
    """Download the day's NetCDF via rclone from the 'ceda:' or 'jasmin:' http remote.

    Tries the date's primary source (sources_for_date), then the other; within each, the year-appropriate variant
    first, then the other. Only if EVERY source and variant fails in one round does it sleep with exponential backoff
    and retry -- a 404 from the source that does not hold the date costs one quick request, never a sleep.
    The backoff retries up to max_retries times — this handles transient CEDA "directory not found"
    errors where the file does exist but the server momentarily can't serve it.
    rclone's own --retries / --low-level-retries flags only cover transient
    connection errors within a single invocation; a 404 is treated as permanent
    and gives up immediately, so this outer loop is what saves the day.

    Returns (dst_path, filename, errors) where errors is {"source:variant": rclone_stderr}
    for any attempt that failed in the final round. Successful downloads
    return (path, name, {}).
    """
    primary, fallback = expected_variants_for_year(date.year)
    delay = retry_initial_delay
    errors = {}

    for attempt in range(max_retries + 1):
        errors = {}
        for source in sources_for_date(date):
            for variant in (primary, fallback):
                _, filename = build_url(date, variant)
                # rclone expects a path relative to the remote's base URL (same layout on both sources).
                remote_rel = f'{date.format("YYYY")}/{date.format("MM")}/{date.format("DD")}/{filename}'
                dst = dl_dir / filename
                # `copyto` lets us specify the exact destination filename.
                cmd = [
                    'rclone', 'copyto', f'{source}:{remote_rel}', str(dst),
                    '--config', str(config_path),
                    '--retries', '3',
                    '--low-level-retries', '5',
                    '--contimeout', '30s',
                    '--timeout', '5m',
                ]
                p = subprocess.run(cmd, capture_output=True, text=True)
                if p.returncode == 0 and dst.exists():
                    return dst, filename, {}
                errors[f'{source}:{variant}'] = (p.stderr or p.stdout or f'exit code {p.returncode}').strip()

        # Every source and variant failed this attempt. Back off and try again if we have retries left.
        if attempt < max_retries:
            # Jitter the actual sleep over [0.5*delay, 1.5*delay) so parallel workers
            # don't all wake at the exact same instant and re-trigger CEDA's rate
            # limiter as a thundering herd. `delay` (the base) still grows by
            # retry_backoff per attempt, so worst-case bound is preserved.
            sleep_s = max(1, int(delay * (0.5 + random.random())))
            print(
                f'[retry {attempt+1}/{max_retries}] {date.format("YYYY-MM-DD")}: '
                f'every source and variant failed; sleeping {sleep_s}s before next attempt',
                flush=True,
            )
            time.sleep(sleep_s)
            delay = int(delay * retry_backoff)

    return None, None, errors


def clip_file(src, dst, min_lon, max_lon, min_lat, max_lat):
    """Clip src to the requested bbox via cdo. Returns None on success,
    error message on failure. Verifies the output actually covers the
    requested bbox (within one cell-width tolerance) so silent clipping
    bugs surface immediately."""
    cmd_str = CLIP_STR_FORMAT.format(
        min_lat=min_lat, max_lat=max_lat, min_lon=min_lon, max_lon=max_lon,
        src=str(src), dst=str(dst),
    )
    p = subprocess.run(shlex.split(cmd_str), capture_output=True, text=True)
    if p.returncode != 0:
        return p.stderr or p.stdout

    # Post-clip verification: the output coords must cover the requested bbox.
    # cdo sellonlatbox returns cells whose centers fall inside [lo, hi], so the
    # coord range may differ from the request by up to one grid cell (~0.05 deg
    # for CCI); require coverage within cell-width tolerance.
    cell_tol = 0.06  # slightly > CCI's 0.05 deg grid
    try:
        ok, err = _verify_clip_coverage(dst, min_lon, max_lon, min_lat, max_lat, cell_tol)
    except Exception as e:
        return f'clip output verification failed to open {dst}: {e}'
    if not ok:
        return err
    return None


def _verify_clip_coverage(nc_path, min_lon, max_lon, min_lat, max_lat, cell_tol):
    """Open a cdo-clipped NetCDF and assert its lat/lon span the requested bbox.

    Returns (True, None) on success, (False, error_message) if coverage is short.
    Uses the stdlib-free h5py backend via the `ncdump -h` parse fallback to avoid
    pulling in a full NetCDF Python binding for this small check. In practice
    we can shell out to `cdo griddes` which prints the grid description.
    """
    # Quick approach: use `cdo griddes` to get the grid bounds from the file.
    p = subprocess.run(
        ['cdo', '-s', 'griddes', str(nc_path)],
        capture_output=True, text=True,
    )
    if p.returncode != 0:
        return False, f'cdo griddes failed on {nc_path}: {p.stderr.strip()[:200]}'

    # Parse: look for xfirst/xinc/xsize and yfirst/yinc/ysize from griddes output
    info = {}
    for line in p.stdout.splitlines():
        line = line.strip()
        if '=' in line:
            k, _, v = line.partition('=')
            info[k.strip()] = v.strip()

    try:
        xfirst = float(info['xfirst'])
        xinc = float(info['xinc'])
        xsize = int(info['xsize'])
        yfirst = float(info['yfirst'])
        yinc = float(info['yinc'])
        ysize = int(info['ysize'])
    except (KeyError, ValueError) as e:
        return False, f'could not parse cdo griddes output: {e}'

    xlast = xfirst + (xsize - 1) * xinc
    ylast = yfirst + (ysize - 1) * yinc
    actual_min_lon, actual_max_lon = sorted((xfirst, xlast))
    actual_min_lat, actual_max_lat = sorted((yfirst, ylast))

    problems = []
    if actual_min_lon > min_lon + cell_tol:
        problems.append(f'min_lon: requested {min_lon}, got {actual_min_lon:.4f}')
    if actual_max_lon < max_lon - cell_tol:
        problems.append(f'max_lon: requested {max_lon}, got {actual_max_lon:.4f}')
    if actual_min_lat > min_lat + cell_tol:
        problems.append(f'min_lat: requested {min_lat}, got {actual_min_lat:.4f}')
    if actual_max_lat < max_lat - cell_tol:
        problems.append(f'max_lat: requested {max_lat}, got {actual_max_lat:.4f}')

    if problems:
        return False, 'clipped output does not cover requested bbox: ' + '; '.join(problems)
    return True, None


def upload_file(clip_path, date, remote_path, config_path):
    """rclone copy preserves the filename; the YYYY dst dir gives layout."""
    dst_dir = f'sst:{remote_path.rstrip("/")}/{date.format("YYYY")}/'
    cmd = ['rclone', 'copy', str(clip_path), dst_dir, '--config', str(config_path)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        return p.stderr or p.stdout
    return None


def process_one_day(date, bounds, remote_path, config_path, workdir, check_target,
                    max_retries=3, retry_initial_delay=30, retry_backoff=2.0):
    """Download → clip → upload a single day. Returns (date, status, msg).

    status is one of 'ok', 'skip', 'fail'. If check_target is True and the
    remote already has a file for this date (either CDR or ICDR variant), the
    day is skipped. Set check_target=False to always re-download (e.g. after
    changing the bbox).

    max_retries / retry_initial_delay / retry_backoff control the
    exponential-backoff retry on transient CEDA download failures. See
    download_day for details.
    """
    if check_target and remote_has_day(date, remote_path, config_path):
        return date, 'skip', None

    dl_dir = workdir / 'download'
    clip_dir = workdir / 'clipped'
    dl_dir.mkdir(parents=True, exist_ok=True)
    clip_dir.mkdir(parents=True, exist_ok=True)

    dl_path, filename, dl_errors = download_day(
        date, dl_dir, config_path,
        max_retries=max_retries,
        retry_initial_delay=retry_initial_delay,
        retry_backoff=retry_backoff,
    )
    if dl_path is None:
        # Surface the actual curl diagnostic from each variant attempt
        detail = '; '.join(f'{v}: {msg[:300]}' for v, msg in dl_errors.items())
        return date, 'fail', f'download failed — {detail}'

    clip_path = clip_dir / filename
    err = clip_file(
        dl_path, clip_path,
        bounds['min_lon'], bounds['max_lon'],
        bounds['min_lat'], bounds['max_lat'],
    )
    dl_path.unlink(missing_ok=True)
    if err:
        clip_path.unlink(missing_ok=True)
        return date, 'fail', f'ncks clip failed: {err.strip()[:200]}'

    err = upload_file(clip_path, date, remote_path, config_path)
    clip_path.unlink(missing_ok=True)
    if err:
        return date, 'fail', f'rclone upload failed: {err.strip()[:200]}'

    return date, 'ok', None


######################################################
### Main

def main():
    parser = argparse.ArgumentParser(
        description='Download ESA CCI SST v3 daily NetCDFs from CEDA / JASMIN, clip, upload.'
    )
    parser.add_argument('parameters_path', type=Path, help='TOML config file path.')
    parser.add_argument('-s', '--start-date', help='Override [dates].start_date (YYYY-MM-DD).')
    parser.add_argument('-e', '--end-date', help='Override [dates].end_date (YYYY-MM-DD).')
    parser.add_argument('--min-lon', type=float, help='Override [bounds].min_lon.')
    parser.add_argument('--max-lon', type=float, help='Override [bounds].max_lon.')
    parser.add_argument('--min-lat', type=float, help='Override [bounds].min_lat.')
    parser.add_argument('--max-lat', type=float, help='Override [bounds].max_lat.')
    parser.add_argument('-n', '--n-tasks', type=int, help='Parallel workers (default: TOML value or 4).')
    parser.add_argument(
        '--check-target', dest='check_target', action='store_true', default=None,
        help='Skip days whose file already exists on the destination (default).',
    )
    parser.add_argument(
        '--no-check-target', dest='check_target', action='store_false',
        help='Always re-download and re-upload even if destination file exists. Useful when the bbox changed.',
    )
    parser.add_argument(
        '--max-retries', type=int,
        help='Max retries per date when every source and variant fails (default: TOML or 3).',
    )
    parser.add_argument(
        '--retry-initial-delay', type=int,
        help='Seconds to wait before the first retry (default: TOML or 30).',
    )
    parser.add_argument(
        '--retry-backoff', type=float,
        help='Multiplier applied to delay after each retry (default: TOML or 2.0).',
    )
    parser.add_argument(
        '--failures-file', type=Path,
        help='Where to write the list of dates that still failed after retries '
             '(one YYYY-MM-DD per line, empty if all succeeded). '
             'Default: <parameters.toml dir>/failures.txt.',
    )
    parser.add_argument(
        '--from-failures', type=Path,
        help='Read input dates from this file (one YYYY-MM-DD per line; blank '
             'lines and lines starting with # are ignored). Overrides '
             '[dates].start_date / .end_date.',
    )
    args = parser.parse_args()

    check_external_binaries()

    params = load_params(args.parameters_path)
    params = apply_overrides(params, args)

    dates_cfg = params.get('dates') or {}
    bounds = params.get('bounds') or {}
    remote_cfg = params.get('remote') or {}

    # [dates].start_date/end_date are only required when we're not loading the
    # date list from a --from-failures file.
    if args.from_failures is None:
        for key in ('start_date', 'end_date'):
            if key not in dates_cfg:
                print(f'Error: missing [dates].{key} in {args.parameters_path}', file=sys.stderr)
                sys.exit(1)
    for key in ('min_lon', 'max_lon', 'min_lat', 'max_lat'):
        if key not in bounds:
            print(f'Error: missing [bounds].{key} in {args.parameters_path}', file=sys.stderr)
            sys.exit(1)
    if not remote_cfg:
        print(f'Error: missing [remote] section in {args.parameters_path}', file=sys.stderr)
        sys.exit(1)
    remote_path = remote_cfg.pop('path', '')

    if args.from_failures is not None:
        if not args.from_failures.is_file():
            print(f'Error: --from-failures file not found: {args.from_failures}', file=sys.stderr)
            sys.exit(1)
        dates = load_failures_file(args.from_failures)
        if not dates:
            print(f'-- --from-failures: no dates to process in {args.from_failures}; exiting cleanly')
            return
        start_date = dates[0]
        end_date = dates[-1]
        print(f'-- --from-failures: loaded {len(dates)} dates from {args.from_failures}')
    else:
        start_date = pendulum.parse(dates_cfg['start_date']).date()
        end_date = pendulum.parse(dates_cfg['end_date']).date()
        dates = list(date_range(start_date, end_date))

    n_tasks = int(params.get('n_tasks', 4))
    check_target = bool(params.get('check_target', True))
    max_retries = int(params.get('max_retries', 3))
    retry_initial_delay = int(params.get('retry_initial_delay', 30))
    retry_backoff = float(params.get('retry_backoff', 2.0))
    failures_file = Path(params['failures_file']) if 'failures_file' in params \
        else args.parameters_path.parent / 'failures.txt'

    print(f'-- CCI SST mirror: {len(dates)} days from {start_date} to {end_date}')
    print(f'-- bbox: lon[{bounds["min_lon"]},{bounds["max_lon"]}] lat[{bounds["min_lat"]},{bounds["max_lat"]}]')
    print(f'-- workers: {n_tasks}, check_target: {check_target}')
    print(f'-- retries: max={max_retries}, initial_delay={retry_initial_delay}s, backoff={retry_backoff}x')
    print(f'-- failures will be written to: {failures_file}')

    t1 = pendulum.now()
    counts = {'ok': 0, 'skip': 0, 'fail': 0}
    failures = []
    with tempfile.TemporaryDirectory(prefix='cci_sst_') as tmp:
        workdir = Path(tmp)
        config_path = workdir / 'rclone.config'
        create_rclone_config(remote_cfg, config_path)

        with concurrent.futures.ThreadPoolExecutor(max_workers=n_tasks) as executor:
            futures = {
                executor.submit(
                    process_one_day, d, bounds, remote_path, config_path, workdir, check_target,
                    max_retries=max_retries,
                    retry_initial_delay=retry_initial_delay,
                    retry_backoff=retry_backoff,
                ): d
                for d in dates
            }
            for fut in concurrent.futures.as_completed(futures):
                date, status, msg = fut.result()
                counts[status] += 1
                if status == 'ok':
                    print(f'[ok]   {date}')
                elif status == 'skip':
                    print(f'[skip] {date} (already on remote)')
                else:
                    print(f'[fail] {date}: {msg}')
                    failures.append((date, msg))

    elapsed = round((pendulum.now() - t1).total_seconds())
    print(f'-- Done in {elapsed}s. {counts["ok"]} uploaded, {counts["skip"]} skipped, {counts["fail"]} failed.')

    # Write failures.txt unconditionally — empty file if all succeeded, so the file
    # is always an authoritative record of the last run's state. MUST happen after
    # the `with tempfile.TemporaryDirectory(...)` block above closes; we'd lose the
    # file if the default location were inside workdir.
    failures_file.parent.mkdir(parents=True, exist_ok=True)
    with open(failures_file, 'w') as f:
        for date, _msg in failures:   # failures items are (date, msg) tuples
            f.write(f'{date.format("YYYY-MM-DD")}\n')
    print(f'-- failures recorded in {failures_file} (n={len(failures)})')

    if failures:
        sys.exit(1)


if __name__ == '__main__':
    main()
