#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Download ESA CCI SST v3.0 L4 daily NetCDFs from CEDA, clip each to a regional
bbox with ncks, and upload to an rclone-configured S3 (or local) destination.

Intentionally lightweight: one file, argparse-based CLI, no Python package,
no tests. Sibling in spirit to `era5-download` but much smaller.

Source: https://dap.ceda.ac.uk/neodc/eocis/data/global_and_regional/sea_surface_temperature/CDR_v3/Analysis/L4/v3.0.1
License: CC-BY 4.0 (credit Merchant et al. 2019, doi:10.1038/s41597-019-0236-x)

External binaries required: curl, ncks (NCO), rclone.
"""
import argparse
import concurrent.futures
import shlex
import shutil
import subprocess
import sys
import tempfile
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

# year <= CDR_LAST_YEAR → CDR3.0, else ICDR3.0
CDR_LAST_YEAR = 2021

FILENAME_TMPL = (
    '{yyyymmdd}120000-ESACCI-L4_GHRSST-SSTdepth-OSTIA-GLOB_'
    '{variant}3.0-v02.0-fv01.0.nc'
)

CLIP_STR_FORMAT = (
    'ncks -O -4 -L 3 -d lat,{min_lat:.3f},{max_lat:.3f} '
    '-d lon,{min_lon:.3f},{max_lon:.3f} {src} {dst}'
)


######################################################
### Helpers

def check_external_binaries():
    for bin_ in ('curl', 'ncks', 'rclone'):
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
    return params


def date_range(start_date, end_date):
    curr = pendulum.date(start_date.year, start_date.month, start_date.day)
    end = pendulum.date(end_date.year, end_date.month, end_date.day)
    while curr <= end:
        yield curr
        curr = curr.add(days=1)


def build_url(date, variant):
    filename = FILENAME_TMPL.format(yyyymmdd=date.format('YYYYMMDD'), variant=variant)
    url = f'{CEDA_BASE}/{date.format("YYYY")}/{date.format("MM")}/{date.format("DD")}/{filename}'
    return url, filename


def create_rclone_config(config_dict, config_path):
    """Write an rclone config entry named 'sst' from a TOML dict."""
    type_ = config_dict['type']
    kv = ' '.join(f'{k}={v}' for k, v in config_dict.items() if k != 'type')
    cmd = f'rclone config create sst {type_} {kv} --config={config_path} --non-interactive'
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


def download_day(date, dl_dir):
    """Download the day's NetCDF, trying the year-appropriate variant first, then falling back."""
    primary, fallback = expected_variants_for_year(date.year)

    for variant in (primary, fallback):
        url, filename = build_url(date, variant)
        dst = dl_dir / filename
        cmd = ['curl', '-fSL', '-o', str(dst), url]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode == 0:
            return dst, filename
    return None, None


def clip_file(src, dst, min_lon, max_lon, min_lat, max_lat):
    cmd_str = CLIP_STR_FORMAT.format(
        min_lat=min_lat, max_lat=max_lat, min_lon=min_lon, max_lon=max_lon,
        src=str(src), dst=str(dst),
    )
    p = subprocess.run(shlex.split(cmd_str), capture_output=True, text=True)
    if p.returncode != 0:
        return p.stderr or p.stdout
    return None


def upload_file(clip_path, date, remote_path, config_path):
    """rclone copy preserves the filename; the YYYY dst dir gives layout."""
    dst_dir = f'sst:{remote_path.rstrip("/")}/{date.format("YYYY")}/'
    cmd = ['rclone', 'copy', str(clip_path), dst_dir, '--config', str(config_path)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        return p.stderr or p.stdout
    return None


def process_one_day(date, bounds, remote_path, config_path, workdir, check_target):
    """Download → clip → upload a single day. Returns (date, status, msg).

    status is one of 'ok', 'skip', 'fail'. If check_target is True and the
    remote already has a file for this date (either CDR or ICDR variant), the
    day is skipped. Set check_target=False to always re-download (e.g. after
    changing the bbox).
    """
    if check_target and remote_has_day(date, remote_path, config_path):
        return date, 'skip', None

    dl_dir = workdir / 'download'
    clip_dir = workdir / 'clipped'
    dl_dir.mkdir(parents=True, exist_ok=True)
    clip_dir.mkdir(parents=True, exist_ok=True)

    dl_path, filename = download_day(date, dl_dir)
    if dl_path is None:
        return date, 'fail', 'download failed (both CDR and ICDR returned non-200)'

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
        description='Download ESA CCI SST v3 daily NetCDFs from CEDA, clip, upload.'
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
    args = parser.parse_args()

    check_external_binaries()

    params = load_params(args.parameters_path)
    params = apply_overrides(params, args)

    dates_cfg = params.get('dates') or {}
    bounds = params.get('bounds') or {}
    remote_cfg = params.get('remote') or {}

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

    start_date = pendulum.parse(dates_cfg['start_date']).date()
    end_date = pendulum.parse(dates_cfg['end_date']).date()
    dates = list(date_range(start_date, end_date))

    n_tasks = int(params.get('n_tasks', 4))
    check_target = bool(params.get('check_target', True))

    print(f'-- CCI SST mirror: {len(dates)} days from {start_date} to {end_date}')
    print(f'-- bbox: lon[{bounds["min_lon"]},{bounds["max_lon"]}] lat[{bounds["min_lat"]},{bounds["max_lat"]}]')
    print(f'-- workers: {n_tasks}, check_target: {check_target}')

    t1 = pendulum.now()
    counts = {'ok': 0, 'skip': 0, 'fail': 0}
    failures = []
    with tempfile.TemporaryDirectory(prefix='cci_sst_') as tmp:
        workdir = Path(tmp)
        config_path = workdir / 'rclone.config'
        create_rclone_config(remote_cfg, config_path)

        with concurrent.futures.ThreadPoolExecutor(max_workers=n_tasks) as executor:
            futures = {
                executor.submit(process_one_day, d, bounds, remote_path, config_path, workdir, check_target): d
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

    if failures:
        sys.exit(1)


if __name__ == '__main__':
    main()
