#!/usr/bin/env python3
"""Exercise actual Pi5 RAM maps, firmware revision and boot FDT at every size."""
import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile

from compose_pcie2_contract_dtb import decode, encode
from test_emulator_pi5_boot import Pi5Machine, MBOX

GIB = 1 << 30


def read_bytes(machine, address, size):
    return bytes.fromhex(machine.command(f'read {address:#x} {size:#x}').split()[1][2:])


def run(binary, output, profiles):
    output.mkdir(parents=True, exist_ok=False)
    results = []
    with tempfile.TemporaryDirectory(prefix='p5mem-', dir='/tmp') as temp:
        root = Path(temp)
        try:
            # A stopped synthetic AArch64 Image activates the real ARM boot
            # loader and board FDT fixup. No OS acceptance is inferred.
            header = struct.pack('<IIQQQQQQII', 0x14000000, 0, 0x200000,
                                 0x10000, 0, 0, 0, 0, 0x644d5241, 0)
            image = root/'Image'
            image.write_bytes(header + bytes(0x10000-len(header)))
            dtb = root/'input.dtb'
            dtb.write_bytes(encode({
                '/': {'#address-cells': struct.pack('>I', 2),
                      '#size-cells': struct.pack('>I', 1),
                      'compatible': b'brcm,bcm2712\0'},
                '/chosen': {},
                '/memory@0': {'device_type': b'memory\0',
                              'reg': struct.pack('>III', 0, 0, 0x40000000)}
            }, bytes(16), 0))
            for size in profiles:
                with ExitStack() as stack:
                    machine = Pi5Machine(root, f'ram-{size}', stack, binary,
                                         memory_gib=size,
                                         boot_args=('-kernel', str(image),
                                                    '-dtb', str(dtb)))
                    # The stopped ARM boot stub passes the FDT at 128 MiB.
                    raw = read_bytes(machine, 128 << 20, 8)
                    magic, length = struct.unpack('>II', raw)
                    assert magic == 0xd00dfeed and 40 <= length <= 65536, raw
                    raw = read_bytes(machine, 128 << 20, length)
                    (output/f'ram-{size}.dtb').write_bytes(raw)
                    nodes, _, _ = decode(raw)
                    memories = [v for v in nodes.values()
                                if v.get('device_type') == b'memory\0']
                    assert len(memories) == 1, memories
                    reg = memories[0]['reg']
                    assert len(reg) % 12 == 0
                    banks = [(hi << 32 | lo, n)
                             for hi, lo, n in struct.iter_unpack('>III', reg)]
                    assert banks[0] == (0, 0x3c000000), banks
                    assert sum(n for _, n in banks) == size*GIB-(64 << 20), banks
                    assert max(a+n for a, n in banks) == (
                        0x3c000000 if size == 1 else size*GIB), banks
                    assert all(0 < n < 1 << 32 and a+n <= size*GIB
                               for a, n in banks), banks
                    assert all(a+n <= b for (a, n), (b, _) in zip(banks, banks[1:]))
                    # Independent sentinels straddle each 32-bit address
                    # boundary and reach the last actual RAM word.
                    addresses = sorted({0x100000, size*GIB-4} |
                                       {a for a in (2*GIB, 4*GIB, 8*GIB)
                                        if a < size*GIB})
                    for index, address in enumerate(addresses):
                        machine.write(address, 0xa55a0000+index)
                    for index, address in enumerate(addresses):
                        assert machine.read(address) == 0xa55a0000+index
                    memory_code = size.bit_length()+1
                    expected = 0x804170 | memory_code << 20 | (size in (1, 16))
                    request = struct.pack('<8I', 32, 0, 0x10002, 4, 0, 0, 0, 0)
                    machine.command(f'write 0x180000 0x20 0x{request.hex()}')
                    # GPU DMA alias must work even when CPU RAM ends at 1 GiB.
                    machine.write(MBOX+0x20, 0x40180008)
                    assert machine.read(0x180004) == 0x80000000
                    assert machine.read(0x180014) == expected
                    assert machine.read(MBOX) == 0x40180008
                    results.append({'memory_gib': size, 'banks': banks,
                                    'board_revision': hex(expected),
                                    'ram_addresses': addresses, 'passed': True})
            for size in profiles:
                # A revision for a different capacity must be rejected.
                revision = 0xb04170 if size != 2 else 0xd04170
                command = [binary, '-M', f'raspi5b,board-revision={revision:#x}',
                           '-m', f'{size}G', '-display', 'none', '-serial', 'none',
                           '-monitor', 'none', '-nic', 'none']
                result = subprocess.run(command, capture_output=True, timeout=10)
                assert result.returncode != 0 and b'board-revision' in result.stderr
            family_controls = []
            for size in (1, 2, 4, 8, 16):
                revision = 0x804190 | (size.bit_length()+1) << 20
                if size in (8, 16):
                    with ExitStack() as stack:
                        machine = Pi5Machine(root, f'pi500-{size}', stack, binary,
                                             memory_gib=size, revision=revision)
                        request = struct.pack('<8I', 32, 0, 0x10002, 4, 0, 0, 0, 0)
                        machine.command(f'write 0x180000 0x20 0x{request.hex()}')
                        machine.write(MBOX+0x20, 0x40180008)
                        assert machine.read(0x180014) == revision
                    family_controls.append({'pi500_gib': size, 'accepted': True})
                else:
                    result = subprocess.run([binary, '-M',
                        f'raspi5b,board-revision={revision:#x}', '-m', f'{size}G',
                        '-display', 'none', '-serial', 'none', '-monitor', 'none',
                        '-nic', 'none'], capture_output=True, timeout=10)
                    assert result.returncode != 0 and b'board-revision' in result.stderr
                    family_controls.append({'pi500_gib': size, 'accepted': False})
            (output/'result.json').write_text(json.dumps({
                'passed': True, 'profiles': results, 'family_controls': family_controls,
                'scope': 'native QEMU qtest RAM/FDT/mailbox; no OS or hardware acceptance'
            }, indent=2)+'\n')
        finally:
            for log in root.glob('*.log'):
                shutil.copyfile(log, output/log.name)
    print('PASS Pi5 RAM/FDT/revision/mailbox profiles:', profiles)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--profiles', type=int, nargs='+', default=[1, 2, 4, 8, 16])
    args = parser.parse_args()
    run(str(args.qemu.resolve()), args.output.resolve(), args.profiles)
