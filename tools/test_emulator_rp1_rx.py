#!/usr/bin/env python3
"""RP1 programmed RX thresholds, baud-latched timeout and timed migration.

Controlled arrivals complete timed loopback frames before RX deadline checks.
The separate TX suite qualifies those frame boundaries; bit-level errors and
the RP1 clock tree remain separate.
"""
import argparse
from contextlib import ExitStack
from pathlib import Path
import tempfile

from test_emulator_rp1 import Machine, configure, BAR1
from test_emulator_rp1_uart import VECTORS
from test_emulator_adc_migration import migrate
from uconsole_emulator import qmp

RX, RT = 0x10, 0x40


def step(m, nanoseconds):
    return int(m.command(f'clock_step {nanoseconds}').split()[1], 0)


def setup(m, n, selection=2, integer=3, fractional=0, mask=RX | RT):
    base = BAR1 + 0x30000 + n*0x4000
    m.write(base + 0x38, 0)
    m.write(base + 0x30, 0x280)
    m.write(base + 0x2c, 0x60)
    m.write(base + 0x44, 0x7ff)
    m.write(base + 0x24, integer)
    m.write(base + 0x28, fractional)
    m.write(base + 0x2c, 0x70)
    m.write(base + 0x30, 0x381)
    m.write(base + 0x34, selection << 3)
    m.write(base + 0x38, mask)
    m.cfg(VECTORS[n], 13)  # ENABLE/IACK_EN plus self-clearing IACK.
    m.message(VECTORS[n], 0)
    assert not m.read(base + 0x3c) & (RX | RT)
    if not hasattr(m, 'uart_frame_ns'):
        m.uart_frame_ns = {}
    numerator = 10 * (64 * integer + fractional) * 10**9
    m.uart_frame_ns[base] = (numerator + 192000000 - 1) // 192000000
    return base


def arrive(m, base, byte):
    m.write(base, byte)
    step(m, m.uart_frame_ns[base])


def exercise(binary, root, stack):
    m = Machine(root, 'source', stack, binary)
    configure(m)
    # Qtest advances virtual time explicitly, but -S disables timer dispatch.
    # Enable the virtual clock; the qtest accelerator executes no guest code.
    qmp(m.monitor, 'cont')
    for n, vector in enumerate(VECTORS):
        for selection, threshold in enumerate((4, 8, 16, 24, 28)):
            base = setup(m, n, selection)
            for count in range(threshold - 1):
                arrive(m, base, count)
                assert not m.read(base + 0x3c) & RX, 'Below-threshold UART raised RX interrupt'
                assert m.message(vector) == 0
            arrive(m, base, threshold - 1)
            assert m.read(base + 0x3c) & RX, (n, threshold)
            assert m.message(vector) == 0xfeed0000 + vector
            assert m.read(base) & 0xff == 0
            assert not m.read(base + 0x3c) & RX
            for count in range(1, threshold):
                assert m.read(base) & 0xff == count
            assert m.read(base + 0x18) & 0x10
        base = setup(m, n)
        arrive(m, base, 0x51)
        step(m, 31999)
        assert not m.read(base + 0x3c) & (RX | RT)
        assert m.message(vector) == 0
        step(m, 1)
        assert m.read(base + 0x3c) & RT
        assert m.message(vector) == 0xfeed0000 + vector
        # RP1 IACK reissues an uncleared UART timeout level.
        m.message(vector, 0)
        m.cfg(vector, 4, alias=0x800)
        assert m.message(vector) == 0xfeed0000 + vector
        m.write(base + 0x44, RT)
        m.message(vector, 0)
        m.cfg(vector, 4, alias=0x800)
        # Start the10us frame early so the new character still arrives at
        #31999ns after ICR, exactly as in the original controlled-arrival test.
        step(m, 21999)
        assert not m.read(base + 0x3c) & RT
        # A new character restarts the deadline, not the old elapsed remainder.
        arrive(m, base, 0x52)
        assert not m.read(base + 0x3c) & RT
        step(m, 1)
        assert not m.read(base + 0x3c) & RT
        step(m, 31998)
        assert not m.read(base + 0x3c) & RT
        step(m, 1)
        assert m.read(base + 0x3c) & RT
        assert m.read(base) & 0xff == 0x51
        assert m.read(base + 0x3c) & RT, 'Partial drain cleared the latched timeout'
        assert m.read(base) & 0xff == 0x52
        assert not m.read(base + 0x3c) & RT
        m.message(vector, 0)
        m.cfg(vector, 4, alias=0x800)
        step(m, 64000)
        assert not m.read(base + 0x3c) & RT and m.message(vector) == 0
    # UARTEN and RXE independently gate the receiver, including loopback.
    for control in (0x181, 0x380):
        base = setup(m, 0)
        m.write(base + 0x30, control)
        arrive(m, base, 0x70)
        step(m, 32000)
        assert m.read(base + 0x18) & 0x10
        assert not m.read(base + 0x3c) & (RX | RT)
        assert m.message(25) == 0
        # A globally disabled UART queues TX; discard that pending character
        # before enabling reception. TX queue retention has its own controls.
        m.write(base + 0x2c, 0x60)
        m.write(base + 0x2c, 0x70)
        m.write(base + 0x30, 0x381)
        arrive(m, base, 0x71)
        step(m, 32000)
        assert m.read(base + 0x3c) & RT
        assert m.read(base) & 0xff == 0x71
    # Fractional divisor: ceil(8 * (64*3+1) * 1e9 / 48e6) = 32167ns.
    base = setup(m, 0, fractional=1)
    arrive(m, base, 0x61)
    step(m, 32166)
    assert not m.read(base + 0x3c) & RT
    step(m, 1)
    assert m.read(base + 0x3c) & RT
    # IBRD/FBRD are buffered until a write of LCR_H commits the baud divisor.
    base = setup(m, 0)
    arrive(m, base, 0x62)
    m.write(base + 0x24, 6)
    step(m, 31999)
    assert not m.read(base + 0x3c) & RT
    step(m, 1)
    assert m.read(base + 0x3c) & RT
    assert m.read(base) & 0xff == 0x62
    m.write(base + 0x2c, 0x70)
    m.cfg(25, 4, alias=0x800)
    m.message(25, 0)
    m.uart_frame_ns[base] = 20000
    arrive(m, base, 0x63)
    step(m, 63999)
    assert not m.read(base + 0x3c) & RT
    step(m, 1)
    assert m.read(base + 0x3c) & RT and m.message(25) == 0xfeed0019
    # Masked timeout latches raw status, then unmasking routes a real MSI-X.
    base = setup(m, 0, mask=0)
    arrive(m, base, 0x64)
    step(m, 32000)
    assert m.read(base + 0x3c) & RT and not m.read(base + 0x40)
    assert m.message(25) == 0
    m.write(base + 0x38, RT)
    assert m.message(25) == 0xfeed0019
    # A partially elapsed deadline survives actual migration without restarting.
    base = setup(m, 0)
    arrive(m, base, 0x65)
    clock = step(m, 12345)
    target = Machine(root, 'target', stack, binary, incoming=True)
    target.command(f'clock_set {clock}')
    migrate(m, target, root)
    # The incoming process also has -S; explicitly enable timer dispatch.
    qmp(target.monitor, 'cont')
    step(target, 19654)
    assert not target.read(base + 0x3c) & RT
    step(target, 1)
    assert target.read(base + 0x3c) & RT
    assert target.message(25) == 0xfeed0019
    assert target.read(base) & 0xff == 0x65
    # Reset cancels a live timer; it cannot inject an interrupt into the next boot.
    base = setup(target, 0)
    arrive(target, base, 0x66)
    step(target, 123)
    qmp(target.monitor, 'system_reset')
    configure(target)
    step(target, 64000)
    for n in range(6):
        bank = BAR1 + 0x30000 + n*0x4000
        assert not target.read(bank + 0x3c) & (RX | RT)
        assert target.read(bank + 0x18) & 0x10
    print('PASS: all6 UARTs/all5 RX thresholds, exact32-bit timeout boundary, '
          'fractional ceil and LCR_H divisor latch, RX enable/restart/partial drain, '
          'raw/masked/IACK MSI-X, remaining-deadline migration and reset cancellation')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--qemu', required=True)
    p.add_argument('--output', type=Path)
    args = p.parse_args()
    with ExitStack() as stack:
        root = args.output
        if root is None:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='rp1-rx-')))
        else:
            root.mkdir(mode=0o700)
        exercise(args.qemu, root.resolve(), stack)


if __name__ == '__main__':
    main()
