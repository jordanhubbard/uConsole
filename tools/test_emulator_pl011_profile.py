#!/usr/bin/env python3
"""Keep legacy 16-entry PL011 migration and bind new 32-entry profiles."""
import argparse
from contextlib import ExitStack
from pathlib import Path
import tempfile

from test_emulator_pcie_xhci import Machine
from test_emulator_adc_migration import migrate
from uconsole_emulator import qmp

UART = 0x09000000


def configure(m):
    m.write(UART + 0x2c, 0x70)
    m.write(UART + 0x30, 0x381)
    for byte in range(16):
        m.write(UART, byte)
    assert m.read(UART + 0x18) & 0x40
    for byte in range(12):
        assert m.read(UART) & 0xff == byte
    for byte in range(16, 28):
        m.write(UART, byte)
    assert m.read(UART + 0x18) & 0x40


def exercise(binary, legacy):
    with tempfile.TemporaryDirectory(prefix='pl011-prof-') as tmp, ExitStack() as stack:
        root = Path(tmp)
        pairs = [(binary, binary)]
        if legacy:
            pairs.extend([(legacy, binary), (binary, legacy)])
        for index, (source_binary, target_binary) in enumerate(pairs):
            source = Machine(root, f's{index}', stack, source_binary, generic=True)
            target = Machine(root, f't{index}', stack, target_binary,
                             generic=True, incoming=True)
            configure(source)
            migrate(source, target, root)
            for byte in range(12, 28):
                assert target.read(UART) & 0xff == byte
            assert target.read(UART + 0x18) & 0x10
        for index, (source_depth, target_depth) in enumerate([(32, 16), (16, 32)]):
            source = Machine(root, f'ms{index}', stack, binary, generic=True,
                             device_args=('-global', f'pl011.fifo-depth={source_depth}'))
            target = Machine(root, f'mt{index}', stack, binary, generic=True,
                             incoming=True,
                             device_args=('-global', f'pl011.fifo-depth={target_depth}'))
            uri = 'unix:' + str(root/f'mismatch{index}')
            qmp(target.monitor, 'migrate-incoming', {'uri': uri})
            qmp(source.monitor, 'migrate', {'uri': uri})
            assert target.process.wait(timeout=30) != 0, 'FIFO-profile mismatch admitted'
            assert 'pl011' in (root/f'mt{index}.log').read_text()
        for index, (source_mode, target_mode) in enumerate([('on', 'off'), ('off', 'on')]):
            source = Machine(root, f'rs{index}', stack, binary, generic=True,
                             device_args=('-global', f'pl011.accurate-rx={source_mode}'))
            target = Machine(root, f'rt{index}', stack, binary, generic=True,
                             incoming=True,
                             device_args=('-global', f'pl011.accurate-rx={target_mode}'))
            uri = 'unix:' + str(root/f'rx-mismatch{index}')
            qmp(target.monitor, 'migrate-incoming', {'uri': uri})
            qmp(source.monitor, 'migrate', {'uri': uri})
            assert target.process.wait(timeout=30) != 0, 'RX policy mismatch admitted'
            assert 'pl011' in (root/f'rt{index}.log').read_text()
        for index, (source_mode, target_mode) in enumerate([('on', 'off'), ('off', 'on')]):
            source = Machine(root, f'ts{index}', stack, binary, generic=True,
                             device_args=('-global', f'pl011.accurate-tx={source_mode}'))
            target = Machine(root, f'tt{index}', stack, binary, generic=True,
                             incoming=True,
                             device_args=('-global', f'pl011.accurate-tx={target_mode}'))
            uri = 'unix:' + str(root/f'tx-mismatch{index}')
            qmp(target.monitor, 'migrate-incoming', {'uri': uri})
            qmp(source.monitor, 'migrate', {'uri': uri})
            assert target.process.wait(timeout=30) != 0, 'TX policy mismatch admitted'
            assert 'pl011' in (root/f'tt{index}.log').read_text()
        for depth in (0, 1, 17, 64):
            rejected = False
            try:
                Machine(root, f'invalid{depth}', stack, binary, generic=True,
                        device_args=('-global', f'pl011.fifo-depth={depth}'))
            except RuntimeError as error:
                rejected = 'fifo-depth must be 16 or 32' in str(error)
            assert rejected, ('Invalid FIFO size admitted', depth)
    print('PASS: PL01116 wrapped FIFO migration; both32/16 profile mismatch '
          'directions, RX/TX-policy mismatches and invalid depths rejected' +
          ('; old/new and new/old wire compatibility' if legacy else ''))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--qemu', required=True)
    p.add_argument('--legacy-qemu')
    args = p.parse_args()
    exercise(args.qemu, args.legacy_qemu)


if __name__ == '__main__':
    main()
