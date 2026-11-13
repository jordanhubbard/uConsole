#!/usr/bin/env python3
"""GENET packet MIB registers, bank resets and actual migration continuation.

Uses isolated socketpair Ethernet and real descriptor DMA, without a host NIC.
Octet/histogram/error counters and physical link behavior need separate coverage.
"""
import argparse
from contextlib import ExitStack
from pathlib import Path
import tempfile
import time

from test_emulator_genet_network import Machine, PACKET, setup, wait_index
from test_emulator_genet_tx_staging import descriptor, migrate_staging, silence
from uconsole_emulator import executable, qmp

RX_PACKETS = 0xc28
TX_PACKETS = 0xca8
MIB_CONTROL = 0xd80


def counters(m, rx, tx):
    actual = m.read(RX_PACKETS), m.read(TX_PACKETS)
    assert actual == (rx, tx), f'packet counters {actual}, expected {(rx, tx)}'
    # Repeated reads must neither clear nor advance hardware counters.
    assert (m.read(RX_PACKETS), m.read(TX_PACKETS)) == actual


def transmit(m, tx):
    index = m.read(tx + 8) & 0xffff
    descriptor(m, index % 4, bytes(64) + PACKET, 0x6000)
    m.write(tx + 12, (index + 1) & 0xffff)
    assert m.receive_wire() == PACKET
    assert m.read(tx + 8) & 0xffff == (index + 1) & 0xffff


def receive(m, rx):
    index = m.read(rx + 8) & 0xffff
    m.send_wire(PACKET)
    wait_index(m, rx + 8, (index + 1) & 0xffff)
    assert m.inspect(0x120000000 + (index % 4) * 4096 + 66,
                     len(PACKET)) == PACKET
    m.write(rx + 12, (index + 1) & 0xffff)


def run(binary, root, case):
    with ExitStack() as stack:
        m = Machine(root, 'source', stack, binary)
        rx, tx = setup(m)
        if case == 'readonly':
            for offset in range(0xc00, 0xd80, 4):
                before = m.read(offset)
                m.write(offset, 0x12345678)
                assert m.read(offset) == before, f'MIB register {offset:#x} is writable'
            m.write(MIB_CONTROL, 0xfffffff8)
            assert m.read(MIB_CONTROL) == 0
            print('PASS: MIB counters/reserved words are read-only; control ignores reserved bits')
            return

        transmit(m, tx)
        receive(m, rx)
        counters(m, 1, 1)

        if case == 'packets':
            # Descriptor consumption is not packet completion: one split frame
            # advances the packet counter only once, at its valid EOP.
            descriptor(m, 1, bytes(64) + PACKET[:30], 0x2000)
            m.write(tx + 12, 2)
            silence(m)
            counters(m, 1, 1)
            descriptor(m, 2, PACKET[30:], 0x4000)
            m.write(tx + 12, 3)
            assert m.receive_wire() == PACKET
            counters(m, 1, 2)
            # Fill four RX slots. A fifth frame reaches the MAC but cannot
            # overwrite the owned ring; its descriptor discard is distinct.
            for index in range(2, 6):
                m.send_wire(PACKET)
                wait_index(m, rx + 8, index)
            m.send_wire(PACKET)
            deadline = time.monotonic() + 5
            while not m.read(rx + 8) >> 16:
                if time.monotonic() >= deadline:
                    raise TimeoutError('RX ring did not record the fifth-frame discard')
            assert m.read(rx + 8) & 0xffff == 5
            counters(m, 6, 2)
            m.write(RX_PACKETS, 0)
            m.write(TX_PACKETS, 0xffffffff)
            counters(m, 6, 2)
            print('PASS: exact frame DMA, split TX EOP, RX ring discard and non-destructive reads')
            return

        m.write(MIB_CONTROL, 1)
        counters(m, 0, 1)
        if case == 'reset':
            receive(m, rx)
            transmit(m, tx)
            counters(m, 0, 2)
            m.write(MIB_CONTROL, 0)
            receive(m, rx)
            counters(m, 1, 2)
            m.write(MIB_CONTROL, 2)
            counters(m, 1, 2)
            m.write(MIB_CONTROL, 4)
            counters(m, 1, 0)
            transmit(m, tx)
            counters(m, 1, 0)
            m.write(MIB_CONTROL, 0)
            transmit(m, tx)
            counters(m, 1, 1)
            qmp(m.monitor, 'system_reset')
            counters(m, 0, 0)
            assert m.read(MIB_CONTROL) == 0
            print('PASS: independent RX/TX/RUNT reset selection, held reset, release and cold reset')
            return

        target = Machine(root, 'target', stack, binary, incoming=True)
        migrate_staging(m, target, root)
        if qmp(target.monitor, 'query-status')['status'] != 'running':
            qmp(target.monitor, 'cont')
        counters(target, 0, 1)
        assert target.read(MIB_CONTROL) == 1
        receive(target, rx)
        counters(target, 0, 1)
        target.write(MIB_CONTROL, 0)
        receive(target, rx)
        transmit(target, tx)
        counters(target, 1, 2)
        print('PASS: actual migration preserves packet counts and held RX reset; DMA continues')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu-system')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--case', choices=('all', 'readonly', 'packets', 'reset', 'migration'),
                        default='all')
    args = parser.parse_args()
    binary = args.qemu_system or str(executable('qemu-system-aarch64'))

    def exercise(root):
        cases = ('readonly', 'packets', 'reset', 'migration') if args.case == 'all' else (args.case,)
        for case in cases:
            directory = root / case
            directory.mkdir()
            run(binary, directory, case)

    if args.output:
        args.output.mkdir(parents=True, exist_ok=False)
        exercise(args.output)
    else:
        with tempfile.TemporaryDirectory(prefix='uc-genet-mib-') as temporary:
            exercise(Path(temporary))


if __name__ == '__main__':
    main()
