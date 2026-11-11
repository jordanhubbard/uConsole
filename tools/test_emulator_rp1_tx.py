#!/usr/bin/env python3
"""RP1 timed transmit FIFOs, frame boundaries, IRQ crossings and flow control."""
import argparse
from contextlib import ExitStack
from pathlib import Path
import tempfile
import time

from test_emulator_rp1 import Machine, configure, BAR1
from test_emulator_rp1_uart import VECTORS
from test_emulator_pmic import connect
from test_emulator_adc_migration import migrate
from uconsole_emulator import qmp

TX, CTS, RT = 0x20, 2, 0x40
BUSY, RXFE, TXFF, TXFE = 8, 0x10, 0x20, 0x80


class ObservedMachine(Machine):
    """Capture actual named RTS GPIO transitions from the qtest transport."""

    def __init__(self, *args, **kwargs):
        self.pin_events = []
        super().__init__(*args, **kwargs)

    def command(self, command):
        self.sock.sendall((command + '\n').encode())
        while True:
            reply = self.stream.readline().decode().strip()
            if reply.startswith('IRQ '):
                _, edge, number = reply.split()
                self.pin_events.append((edge, int(number)))
                continue
            assert reply.startswith('OK'), reply
            return reply


def step(m, ns):
    return int(m.command(f'clock_step {ns}').split()[1], 0)


def setup(m, n=0, selection=2, fifo=True, bits=8, parity=False,
          stops=1, integer=3, fractional=0, rx_selection=2):
    base = BAR1 + 0x30000 + n * 0x4000
    m.write(base + 0x38, 0)
    m.write(base + 0x30, 0x280)  # RXE/loopback, UARTEN/TXE clear.
    m.write(base + 0x2c, 0)
    m.write(base + 0x24, integer)
    m.write(base + 0x28, fractional)
    lcr = ((bits - 5) << 5) | (0x10 if fifo else 0)
    lcr |= 2 if parity else 0
    lcr |= 8 if stops == 2 else 0
    m.write(base + 0x2c, lcr)
    m.write(base + 0x34, (rx_selection << 3) | selection)
    m.write(base + 0x44, 0x7ff)
    m.write(base + 0x38, TX)
    m.cfg(VECTORS[n], 13)
    m.message(VECTORS[n], 0)
    assert not m.read(base + 0x3c) & TX, 'Empty UART raised an initial TX interrupt'
    return base


def receive(m, base, expected):
    assert not m.read(base + 0x18) & RXFE, ('RX missing', expected)
    assert m.read(base) & 0xff == expected


def empty_peer(peer):
    peer.setblocking(False)
    try:
        try:
            data = peer.recv(1)
        except BlockingIOError:
            return
        raise AssertionError(('External TX arrived before its frame deadline', data))
    finally:
        peer.settimeout(3)


def exercise(binary, root, stack):
    m = Machine(root, 'source', stack, binary)
    configure(m)
    qmp(m.monitor, 'cont')
    for n, vector in enumerate(VECTORS):
        for selection, threshold in enumerate((4, 8, 16, 24, 28)):
            base = setup(m, n, selection)
            for byte in range(32):
                m.write(base, byte)
            flags = m.read(base + 0x18)
            assert flags & BUSY and flags & TXFF and not flags & TXFE, \
                'Queued TX FIFO did not set BUSY/TXFF'
            assert flags & RXFE
            m.write(base + 0x30, 0x381)
            assert not m.read(base + 0x18) & TXFF
            frames = 31 - threshold
            step(m, frames * 10000 - 1)
            assert not m.read(base + 0x3c) & TX
            assert m.message(vector) == 0
            step(m, 1)
            assert m.read(base + 0x3c) & TX, (n, threshold)
            assert m.message(vector) == 0xfeed0000 + vector
            m.write(base + 0x44, TX)
            m.cfg(vector, 4, alias=0x800)
            m.message(vector, 0)
            # At31 frames the FIFO is empty but the last shift is still busy.
            step(m, (31 - frames) * 10000)
            flags = m.read(base + 0x18)
            assert flags & TXFE and flags & BUSY and not flags & TXFF
            step(m, 9999)
            for byte in range(31):
                receive(m, base, byte)
            assert m.read(base + 0x18) & RXFE
            step(m, 1)
            receive(m, base, 31)
            assert not m.read(base + 0x18) & BUSY
            assert not m.read(base + 0x3c) & TX
            assert m.message(vector) == 0
    # One holding register plus a separate shift register with FIFO disabled.
    base = setup(m, fifo=False)
    m.write(base, 0xa5)
    assert m.read(base + 0x18) & TXFF
    m.write(base + 0x30, 0x381)
    assert m.read(base + 0x18) & TXFE and m.read(base + 0x18) & BUSY
    assert m.read(base + 0x3c) & TX
    m.write(base, 0x5a)
    assert m.read(base + 0x18) & TXFF and not m.read(base + 0x3c) & TX
    step(m, 9999)
    assert m.read(base + 0x18) & RXFE
    step(m, 1)
    receive(m, base, 0xa5)
    step(m, 10000)
    receive(m, base, 0x5a)
    assert not m.read(base + 0x18) & BUSY
    # Both enables stop after the current frame, preserving queued characters.
    for disabled in (0x281, 0x380):
        base = setup(m)
        m.write(base, 0x41)
        m.write(base, 0x42)
        m.write(base + 0x30, 0x381)
        step(m, 5000)
        m.write(base + 0x30, disabled)
        step(m, 5000)
        receive(m, base, 0x41)
        assert m.read(base + 0x18) & BUSY and not m.read(base + 0x18) & TXFE
        step(m, 100000)
        assert m.read(base + 0x18) & RXFE
        m.write(base + 0x30, 0x381)
        step(m, 9999)
        assert m.read(base + 0x18) & RXFE
        step(m, 1)
        receive(m, base, 0x42)
    # All data widths, parity and stop-bit durations, with fractional rounding.
    for bits in range(5, 9):
        for parity in (False, True):
            for stops in (1, 2):
                base = setup(m, bits=bits, parity=parity, stops=stops, fractional=1)
                m.write(base, 0xff)
                m.write(base + 0x30, 0x381)
                frame_bits = 1 + bits + parity + stops
                ns = (frame_bits * 193 * 10**9 + 192000000 - 1) // 192000000
                step(m, ns - 1)
                assert m.read(base + 0x18) & RXFE and m.read(base + 0x18) & BUSY
                step(m, 1)
                receive(m, base, (1 << bits) - 1)
                assert not m.read(base + 0x18) & BUSY
    base = setup(m)
    m.write(base + 0x24, 6)  # Buffered; LCR_H has not committed this divisor.
    m.write(base, 0x61)
    m.write(base + 0x30, 0x381)
    step(m, 10000)
    receive(m, base, 0x61)
    m.write(base + 0x30, 0x280)
    m.write(base + 0x2c, 0x70)
    m.write(base, 0x62)
    m.write(base + 0x30, 0x381)
    step(m, 19999)
    assert m.read(base + 0x18) & RXFE
    step(m, 1)
    receive(m, base, 0x62)
    base = setup(m, integer=0)
    m.write(base, 0x63)
    m.write(base + 0x30, 0x381)
    step(m, 100000)
    assert m.read(base + 0x18) & RXFE and m.read(base + 0x18) & BUSY
    m.write(base + 0x24, 3)
    m.write(base + 0x2c, 0x70)
    step(m, 10000)
    receive(m, base, 0x63)
    # CTS is sampled between frames, and either modem edge latches until ICR.
    base = setup(m)
    m.write(base + 0x30, 0x8381)
    m.write(base, 0x71)
    m.write(base, 0x72)
    step(m, 50000)
    assert m.read(base + 0x18) & RXFE and not m.read(base + 0x18) & 1
    m.write(base + 0x30, 0x8b81)
    assert m.read(base + 0x18) & 1 and m.read(base + 0x3c) & CTS
    m.write(base + 0x44, CTS)
    assert not m.read(base + 0x3c) & CTS
    step(m, 5000)
    m.write(base + 0x30, 0x8381)
    assert m.read(base + 0x3c) & CTS
    step(m, 5000)
    receive(m, base, 0x71)
    step(m, 50000)
    assert m.read(base + 0x18) & RXFE and m.read(base + 0x18) & BUSY
    m.write(base + 0x30, 0x8b81)
    step(m, 10000)
    receive(m, base, 0x72)
    # Auto-RTS stops loopback at the RX watermark, resuming as software drains.
    base = setup(m, rx_selection=0)
    m.write(base + 0x30, 0xc381)
    assert m.read(base + 0x18) & 1
    for byte in range(8):
        m.write(base, byte)
    step(m, 40000)
    assert not m.read(base + 0x18) & 1 and m.read(base + 0x18) & BUSY
    for byte in range(8):
        receive(m, base, byte)
        if byte < 4:
            assert m.read(base + 0x18) & 1
            step(m, 10000)
    assert m.read(base + 0x18) & RXFE and not m.read(base + 0x18) & BUSY
    # TX and RX deadlines keep their original chronology across a coarse step.
    base = setup(m)
    m.write(base, 0x73)
    m.write(base + 0x30, 0x381)
    step(m, 41999)
    assert not m.read(base + 0x3c) & RT
    step(m, 1)
    assert m.read(base + 0x3c) & RT
    receive(m, base, 0x73)
    # Real VMState carries a partial shift and a wrapped32-entry pending stream.
    base = setup(m)
    for byte in range(32):
        m.write(base, byte)
    m.write(base + 0x30, 0x381)
    step(m, 200000)
    for byte in range(20):
        receive(m, base, byte)
    for byte in range(32, 52):
        m.write(base, byte)
    clock = step(m, 1234)
    target = Machine(root, 'target', stack, binary, incoming=True)
    target.command(f'clock_set {clock}')
    migrate(m, target, root)
    qmp(target.monitor, 'cont')
    step(target, 8765)
    assert target.read(base + 0x18) & RXFE
    step(target, 1)
    receive(target, base, 20)
    step(target, 310000)
    for byte in range(21, 52):
        receive(target, base, byte)
    assert not target.read(base + 0x18) & BUSY
    # A blocked FIFO migrates without an active timer and resumes on CTS.
    blocked = Machine(root, 'blocked', stack, binary)
    configure(blocked)
    qmp(blocked.monitor, 'cont')
    base = setup(blocked)
    blocked.write(base + 0x30, 0x8381)
    for byte in range(4):
        blocked.write(base, byte)
    clock = step(blocked, 1234)
    resumed = Machine(root, 'resumed', stack, binary, incoming=True)
    resumed.command(f'clock_set {clock}')
    migrate(blocked, resumed, root)
    qmp(resumed.monitor, 'cont')
    step(resumed, 50000)
    assert resumed.read(base + 0x18) & RXFE and resumed.read(base + 0x18) & BUSY
    resumed.write(base + 0x30, 0x8b81)
    step(resumed, 40000)
    for byte in range(4):
        receive(resumed, base, byte)
    # Reset cancels an in-flight frame and every queued character.
    base = setup(resumed, n=1)
    for byte in range(3):
        resumed.write(base, byte)
    resumed.write(base + 0x30, 0x381)
    step(resumed, 1234)
    qmp(resumed.monitor, 'system_reset')
    configure(resumed)
    step(resumed, 100000)
    for n in range(6):
        bank = BAR1 + 0x30000 + n * 0x4000
        flags = resumed.read(bank + 0x18)
        assert flags & RXFE and flags & TXFE and not flags & (BUSY | TXFF)
        assert not resumed.read(bank + 0x3c)
    # Actual external CTS input gates a real host byte stream at frame boundaries.
    sockets = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='rp1-tx-')))
    external = ObservedMachine(root, 'external', stack, binary,
                       endpoint_options=',uart0=serial0',
                       device_args=['-chardev',
                                    f'socket,id=serial0,path={sockets/"uart"},server=on,wait=off'])
    peer = stack.enter_context(connect(sockets/'uart', external.process))
    peer.settimeout(3)
    configure(external)
    qmp(external.monitor, 'cont')
    base = setup(external)
    external.command('irq_intercept_out /machine/peripheral/rp1/serial0 rts')
    external.write(base + 0x30, 0x8b01)
    assert external.pin_events[-1] == ('raise', 0)
    external.write(base + 0x30, 0x8301)
    assert external.pin_events[-1] == ('lower', 0)
    external.write(base + 0x34, 2)  # RX watermark4, TX watermark16.
    external.write(base + 0x30, 0xc301)
    assert external.pin_events[-1] == ('raise', 0)
    peer.sendall(bytes(range(4)))
    deadline = time.monotonic() + 3
    while not external.read(base + 0x3c) & 0x10:
        if time.monotonic() > deadline:
            raise TimeoutError('External RX watermark for RTS GPIO')
        time.sleep(0.001)
    assert external.pin_events[-1] == ('lower', 0)
    receive(external, base, 0)
    assert external.pin_events[-1] == ('raise', 0)
    for byte in range(1, 4):
        receive(external, base, byte)
    external.write(base + 0x30, 0x8301)
    assert external.pin_events[-1] == ('lower', 0)
    external.write(base + 0x44, 0x7ff)
    for byte in (0xa1, 0xa2):
        external.write(base, byte)
    step(external, 50000)
    empty_peer(peer)
    external.command('set_irq_in /machine/peripheral/rp1/serial0 cts 0 1')
    step(external, 5000)
    external.command('set_irq_in /machine/peripheral/rp1/serial0 cts 0 0')
    step(external, 4999)
    empty_peer(peer)
    step(external, 1)
    assert peer.recv(1) == b'\xa1'
    step(external, 50000)
    empty_peer(peer)
    external.command('set_irq_in /machine/peripheral/rp1/serial0 cts 0 1')
    step(external, 10000)
    assert peer.recv(1) == b'\xa2'
    qmp(external.monitor, 'system_reset')
    configure(external)
    assert external.read(base + 0x18) & 1  # The external pin survives UART reset.
    assert not external.read(base + 0x3c)
    print('PASS: all6 UARTs/all5 TX thresholds;32/1 FIFOs and separate shift; '
          'TXFE/TXFF/BUSY, exact frame/fractional/divisor and enable boundaries; '
          'CTS input/both modem edges and auto-RTS; coarse TX/RX deadlines; '
          'active/wrapped and blocked-FIFO migration; reset and external host TX')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--qemu', required=True)
    p.add_argument('--output', type=Path)
    args = p.parse_args()
    with ExitStack() as stack:
        root = args.output
        if root is None:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='rp1-tx-')))
        else:
            root.mkdir(mode=0o700)
        exercise(args.qemu, root.resolve(), stack)


if __name__ == '__main__':
    main()
