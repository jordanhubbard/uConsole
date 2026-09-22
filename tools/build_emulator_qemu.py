#!/usr/bin/env python3
"""Build an isolated, pinned QEMU on a POSIX host; never replace system QEMU."""
import argparse
import hashlib
from pathlib import Path
import subprocess
import tarfile
import urllib.request

VERSION = '10.2.4'
SHA256 = '821b545b92f165e57dddccac5077d76d4d436a226595b8813ad59306bbfd0746'
ROOT = Path(__file__).resolve().parent.parent / 'build/emulator'


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--jobs', type=int, default=4)
    args = p.parse_args()
    if args.jobs < 1:
        p.error('--jobs must be positive')
    ROOT.mkdir(parents=True, exist_ok=True)
    archive = ROOT / f'qemu-{VERSION}.tar.xz'
    if not archive.exists():
        temporary = archive.with_suffix('.partial')
        urllib.request.urlretrieve(f'https://download.qemu.org/{archive.name}', temporary)
        temporary.rename(archive)
    with archive.open('rb') as stream:
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
    if actual != SHA256:
        raise SystemExit(f'QEMU archive checksum mismatch: {actual}')
    source = ROOT / f'qemu-{VERSION}'
    if not source.exists():
        with tarfile.open(archive) as tar:
            tar.extractall(ROOT, filter='data')
    for name in ['bcm2835-watchdog-timer.patch', 'raspi4-upper-memory.patch',
                 'uconsole-axp221-pmic.patch']:
        patch = ROOT.parent.parent / 'Code/patch/qemu' / name
        check = subprocess.run(['patch', '--dry-run', '--forward', '-p1', '-i', str(patch)],
                               cwd=source, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if check.returncode == 0:
            subprocess.run(['patch', '--forward', '-p1', '-i', str(patch)], cwd=source, check=True)
        else:
            # Only accept a source tree which already contains this exact patch.
            subprocess.run(['patch', '--dry-run', '--reverse', '-p1', '-i', str(patch)], cwd=source, check=True)
    build = ROOT / 'qemu-build'
    build.mkdir(exist_ok=True)
    if not (build / 'build.ninja').exists():
        subprocess.run([str(source / 'configure'), '--target-list=aarch64-softmmu',
                        '--disable-docs', '--disable-werror', '--disable-guest-agent',
                        '--enable-slirp', '--enable-vnc', '--disable-debug-info'],
                       cwd=build, check=True)
    subprocess.run(['ninja', f'-j{args.jobs}', 'qemu-system-aarch64', 'qemu-img'], cwd=build, check=True)
    subprocess.run([str(build / 'qemu-system-aarch64'), '--version'], check=True)


if __name__ == '__main__':
    main()
