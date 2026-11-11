#!/usr/bin/env python3
"""RP1 six-UART BAR routing, RX FIFO boundaries, MSI-X and live migration.

Programmed receive thresholds and timed transmit frames use explicit virtual time.
"""
import argparse
from contextlib import ExitStack
from pathlib import Path
import tempfile
import time
from test_emulator_pmic import connect

from test_emulator_rp1 import Machine, configure, BAR1, CFG
from test_emulator_adc_migration import migrate
from uconsole_emulator import qmp

VECTORS = (25, 42, 43, 44, 45, 46)


def setup(m, n, fifo=True):
    base = BAR1 + 0x30000 + n*0x4000
    m.write(base + 0x30, 0x280)
    m.write(base + 0x24, 3)
    m.write(base + 0x28, 0)
    m.write(base + 0x2c, 0x70 if fifo else 0x60)
    m.write(base + 0x30, 0x381)  # UARTEN/TXE/RXE/internal loopback.
    m.write(base + 0x38, 0x410)  # RX and overrun interrupts.
    m.cfg(VECTORS[n], 9)
    return base


def send(m, base, byte):
    m.write(base, byte)
    m.command('clock_step 10000')  #48MHz, divisor3,8N1: one10us frame.


def exercise(binary, root, stack):
    m = Machine(root, 'source', stack, binary)
    configure(m)
    qmp(m.monitor, 'cont')
    for n, vector in enumerate(VECTORS):
        base = setup(m, n)
        assert [m.read(base + 0xfe0 + 4*i) for i in range(4)] == [0x11, 0x10, 0x34, 0]
        for byte in range(32):
            send(m, base, 32*n + byte)
        assert m.read(base + 0x18) & 0x40, ('RX FIFO is not full at32', n)
        assert m.message(vector) == 0xfeed0000 + vector
        status = m.read(CFG + (0x108 if vector < 32 else 0x10c))
        assert status & (1 << (vector % 32)), ('UART IRQ not connected', n)
        send(m, base, 0xff)  # Full FIFO must preserve its oldest32 bytes.
        assert m.read(base + 4) & 8 and m.read(base + 0x40) & 0x400
        for byte in range(32):
            assert m.read(base) & 0xff == 32*n + byte, ('FIFO overwrite/wrap', n, byte)
        assert m.read(base + 0x18) & 0x10, ('RX FIFO not empty', n)
        m.write(base + 0x44, 0x7ff)
        m.write(base + 4, 0)
        assert not m.read(base + 0x40)
        m.message(vector, 0)
        m.cfg(vector, 4, alias=0x800)
        assert m.message(vector) == 0, ('Cleared peripheral retriggered IACK', n)
    # Disabled FIFO has one character, not32; overflow also preserves oldest.
    base = setup(m, 0, fifo=False)
    send(m, base, 0xa5)
    assert m.read(base + 0x18) & 0x40
    send(m, base, 0x5a)
    assert m.read(base + 4) & 8
    assert m.read(base) & 0xff == 0xa5
    m.write(base + 0x44, 0x7ff)
    m.write(base + 4, 0)
    m.cfg(25, 4, alias=0x800)
    # All six FIFOs remain distinct, including wrapped high-half entries.
    for n, vector in enumerate(VECTORS):
        base = setup(m, n)
        for byte in range(24):
            send(m, base, byte)
        for byte in range(20):
            assert m.read(base) & 0xff == byte
        for byte in range(24, 52):
            send(m, base, byte)
        assert m.read(base + 0x18) & 0x40
    target = Machine(root, 'target', stack, binary, incoming=True)
    clock = int(m.command('clock_step 1').split()[1], 0)
    target.command(f'clock_set {clock}')
    migrate(m, target, root)
    qmp(target.monitor, 'cont')
    for n, vector in enumerate(VECTORS):
        base = BAR1 + 0x30000 + n*0x4000
        assert target.read(base + 0x18) & 0x40
        for byte in range(20, 52):
            assert target.read(base) & 0xff == byte, ('Lost migrated high FIFO half', n, byte)
        target.write(base + 0x44, 0x7ff)
        target.cfg(vector, 4, alias=0x800)
        target.message(vector, 0)
        send(target, base, 0xb0 + n)
        assert not target.read(base + 0x3c) & 0x10
        assert target.message(vector) == 0
        for _ in range(15):
            send(target, base, 0xb0 + n)
        assert target.message(vector) == 0xfeed0000 + vector
        for _ in range(16):
            assert target.read(base) & 0xff == 0xb0 + n
    qmp(target.monitor, 'system_reset')
    configure(target)
    for n in range(6):
        base = BAR1 + 0x30000 + n*0x4000
        assert target.read(base + 0x18) & 0x10
        assert target.read(base + 0x30) == 0x300
        assert target.read(base + 0x38) == 0
    # Actual external byte streams exercise every exposed chardev alias.
    sockets = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='rp1-serial-')))
    device_args = []
    options = ''
    for n in range(6):
        device_args.extend(['-chardev',
                           f'socket,id=serial{n},path={sockets/str(n)},server=on,wait=off'])
        options += f',uart{n}=serial{n}'
    external = Machine(root, 'external', stack, binary,
                       endpoint_options=options, device_args=device_args)
    configure(external)
    qmp(external.monitor, 'cont')
    for n in range(6):
        peer = stack.enter_context(connect(sockets/str(n), external.process))
        peer.settimeout(3)
        base = setup(external, n)
        external.write(base + 0x30, 0x301)  # External RX/TX; loopback disabled.
        pattern = bytes(range(n*32, n*32 + 32))
        peer.sendall(pattern)
        deadline = time.monotonic() + 3
        while not external.read(base + 0x18) & 0x40:
            if time.monotonic() > deadline:
                raise TimeoutError(('external RX FIFO', n))
            time.sleep(0.001)
        assert external.message(VECTORS[n]) == 0xfeed0000 + VECTORS[n]
        assert bytes(external.read(base) & 0xff for _ in range(32)) == pattern
        send(external, base, 0xa0 + n)
        assert peer.recv(1) == bytes([0xa0 + n]), ('Wrong host TX backend', n)
    print('PASS: all6 RP1 UART BAR/identity and real MSI-X paths;32-entry RX FIFO, '
          'one-byte mode, overrun/no-overwrite, independent wrapped FIFO migration '
          'and post-migration IRQ/reset; all6 external host RX/TX backends')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--qemu', required=True)
    p.add_argument('--output', type=Path)
    args = p.parse_args()
    with ExitStack() as stack:
        root = args.output
        if root is None:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='rp1-uart-')))
        else:
            root.mkdir(mode=0o700)
        exercise(args.qemu, root.resolve(), stack)


if __name__ == '__main__':
    main()
