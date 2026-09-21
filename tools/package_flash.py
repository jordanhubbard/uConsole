#!/usr/bin/env python3
"""Package freshly built firmware and the native reset helper for distribution."""
import argparse
from pathlib import Path
import struct
import tarfile

ROOT = Path(__file__).resolve().parents[1]


def package(build_dir):
    helper = build_dir / 'upload-reset.elf'
    firmware = build_dir / 'firmware/uconsole_keyboard.ino.bin'
    header = helper.read_bytes()[:20]
    if len(header) < 20 or header[:4] != b'\x7fELF' or header[5] != 1:
        raise ValueError('reset helper must be a little-endian Linux ELF binary')
    machine = struct.unpack_from('<H', header, 18)[0]
    architectures = {62: 'amd64', 183: 'aarch64', 40: 'armhf', 243: 'riscv64'}
    if machine not in architectures:
        raise ValueError(f'unsupported reset helper ELF machine: {machine}')
    architecture = architectures[machine]
    if firmware.stat().st_size == 0:
        raise ValueError('firmware is empty')
    bundled = ROOT / 'Bin/uconsole_keyboard_flash'
    files = [(bundled / name, name) for name in ('flash.sh', 'maple_upload', 'README.md')]
    files += [(firmware, 'uconsole_keyboard.ino.bin')]
    reset_path = 'upload-reset.elf' if architecture == 'amd64' else f'deb_packages/{architecture}/upload-reset.elf'
    files += [(helper, reset_path)]
    output = build_dir / f'uconsole_keyboard_flash-{architecture}.tar.gz'
    # Write atomically so an interrupted package step cannot replace a good bundle.
    temporary = output.with_suffix('.tmp')
    try:
        with tarfile.open(temporary, 'w:gz') as archive:
            for source, name in files:
                archive.add(source, arcname=f'uconsole_keyboard_flash/{name}')
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('build_dir', type=Path)
    args = parser.parse_args()
    print(package(args.build_dir))
