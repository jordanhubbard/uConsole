#!/usr/bin/env python3
"""RP1 endpoint enumeration, actual PCI MSI-X DMA, local IACK and migration.

Runs the endpoint on generic ARM virt to isolate it from the unfinished Pi5
PCIe host. This is not whole-board/peripheral-engine qualification.
"""
import argparse
from contextlib import ExitStack
from pathlib import Path
import struct
import subprocess
import tempfile

from test_emulator_pcie_xhci import Machine as PCIMachine
from test_emulator_pmic import connect
from test_emulator_adc_migration import migrate
from uconsole_emulator import qmp

ECAM = 0x3f008000
BAR0, BAR1, BAR2 = 0x10000000, 0x10400000, 0x10800000
CFG = BAR1 + 0x108000
MESSAGE = 0x41000000


class Machine(PCIMachine):
    def __init__(self, root, name, stack, binary, incoming=False):
        sockets = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='rp1-')))
        self.monitor = sockets / 'qmp'
        test = sockets / 'qtest'
        log = stack.enter_context((root / (name + '.log')).open('wb'))
        self.process = subprocess.Popen([
            binary, '-nodefaults', '-M', 'virt,highmem=off', '-m', '128M',
            '-device', 'raspberrypi-rp1,id=rp1,addr=1.0',
            '-accel', 'qtest', '-S', '-display', 'none', '-serial', 'none',
            '-monitor', 'none', '-nic', 'none',
            '-qtest', f'unix:{test},server=on,wait=off',
            '-qmp', f'unix:{self.monitor},server=on,wait=off',
            *(['-incoming', 'defer'] if incoming else [])],
            stdout=log, stderr=log)
        stack.callback(self.close)
        try:
            self.sock = stack.enter_context(connect(test, self.process))
        except (RuntimeError, TimeoutError, OSError):
            raise RuntimeError(Path(log.name).read_text()[-4096:])
        self.stream = stack.enter_context(self.sock.makefile('rwb', buffering=0))

    def cfg(self, n, value=None, alias=0):
        address = CFG + alias + 8 + 4*n
        if value is None:
            return self.read(address)
        self.write(address, value)

    def pin(self, n, level):
        self.command(f'set_irq_in /machine/peripheral/rp1 unnamed-gpio-in {n} {level}')

    def message(self, n, value=None):
        if value is None:
            return self.read(MESSAGE + 4*n)
        self.write(MESSAGE + 4*n, value)

    def table(self, n, masked=False):
        address = MESSAGE + n*4
        self.write(BAR0 + n*16, address)
        self.write(BAR0 + n*16 + 4, 0)
        self.write(BAR0 + n*16 + 8, 0xfeed0000 + n)
        self.write(BAR0 + n*16 + 12, int(masked))

    def pending(self, n):
        return bool(self.read(BAR0 + 0x2000 + 4*(n//32)) & (1 << (n%32)))


def configure(m):
    assert m.read(ECAM) == 0x00011de4
    assert m.read(ECAM + 8) == 0x02000002
    for n, address, mask in [(0, BAR0, 0xffffc000),
                              (1, BAR1, 0xffc00000),
                              (2, BAR2, 0xffff0000)]:
        m.write(ECAM + 0x10 + 4*n, 0xffffffff)
        assert m.read(ECAM + 0x10 + 4*n) == mask
        m.write(ECAM + 0x10 + 4*n, address)
    m.write(ECAM + 4, 6, 'w')
    assert m.read(BAR1) == 0x20001927
    m.write(BAR1, 0)
    assert m.read(BAR1) == 0x20001927
    cap = m.read(ECAM + 0x34, 'b')
    seen = set()
    while cap:
        assert cap not in seen
        seen.add(cap)
        if m.read(ECAM + cap, 'b') == 0x11:
            break
        cap = m.read(ECAM + cap + 1, 'b')
    assert cap == 0xb0
    assert m.read(ECAM + cap + 2, 'w') & 0x7ff == 60
    assert m.read(ECAM + cap + 4) == 0
    assert m.read(ECAM + cap + 8) == 0x2000
    for n in range(61):
        m.table(n)
    m.write(ECAM + cap + 2, 0x8000, 'w')


def exercise(binary, root, stack):
    m = Machine(root, 'source', stack, binary)
    configure(m)
    # Each connected source uses its actual table entry and independent DMA.
    for n in range(61):
        m.cfg(n, 3)
        assert m.message(n) == 0xfeed0000 + n, n
        m.message(n, 0)
        m.cfg(n, 3)
        assert m.message(n) == 0, ('Repeated high generated an edge', n)
        m.cfg(n, 2, alias=0xc00)
        m.cfg(n, 2, alias=0x800)
        assert m.message(n) == 0xfeed0000 + n, n
        m.cfg(n, 0)
        m.message(n, 0)
    # Disabled raw inputs remain visible; enabling a held level creates an edge.
    m.pin(6, 1)
    assert m.read(CFG + 0x108) == 1 << 6
    m.write(CFG + 0x108, 0xffffffff)
    assert m.read(CFG + 0x108) == 1 << 6
    assert m.message(6) == 0
    m.cfg(6, 1)
    assert m.message(6) == 0xfeed0006
    m.pin(6, 0)
    m.cfg(6, 0)
    # TEST is ORed with the real input, not a pulse register.
    m.cfg(25, 3)
    m.message(25, 0)
    m.pin(25, 1)
    m.cfg(25, 2, alias=0xc00)
    assert m.message(25) == 0
    m.pin(25, 0)
    m.pin(25, 1)
    assert m.message(25) == 0xfeed0019
    m.pin(25, 0)
    m.cfg(25, 0)
    # MSI-X vector masks retain the event until the actual table is unmasked.
    n = 60
    m.table(n, masked=True)
    m.cfg(n, 3)
    assert m.message(n) == 0 and m.pending(n)
    assert m.read(CFG + 0x10c) == 1 << 28
    m.table(n)
    assert m.message(n) == 0xfeed003c and not m.pending(n)
    m.cfg(n, 0)
    m.message(n, 0)
    # Function mask is independent of both vector and local IACK masks.
    m.write(ECAM + 0xb2, 0xc000, 'w')
    m.cfg(31, 3)
    assert m.message(31) == 0 and m.pending(31)
    m.write(ECAM + 0xb2, 0x8000, 'w')
    assert m.message(31) == 0xfeed001f and not m.pending(31)
    m.cfg(31, 0)
    # IACK reissues a still-active source; no acknowledgement means no repeat.
    m.cfg(6, 9)
    m.message(6, 0)
    m.pin(6, 1)
    assert m.message(6) == 0xfeed0006
    m.message(6, 0)
    m.pin(6, 0)
    m.pin(6, 1)
    assert m.message(6) == 0
    m.cfg(6, 4, alias=0x800)
    assert m.message(6) == 0xfeed0006 and m.cfg(6) == 9
    m.message(6, 0)
    m.pin(6, 0)
    m.cfg(6, 4, alias=0x800)
    assert m.message(6) == 0
    m.pin(6, 1)
    assert m.message(6) == 0xfeed0006
    # Migrate both a locally blocked level and a separate masked PCI event.
    m.message(6, 0)
    m.table(42, masked=True)
    m.cfg(42, 3)
    assert m.pending(42)
    pattern = bytes(range(256))
    m.command(f'write {BAR2:#x} {len(pattern):#x} 0x{pattern.hex()}')
    target = Machine(root, 'target', stack, binary, incoming=True)
    migrate(m, target, root)
    assert target.cfg(6) == 9
    assert target.read(CFG + 0x108) & (1 << 6)
    assert target.message(6) == 0
    target.pin(6, 0)
    target.pin(6, 1)
    assert target.message(6) == 0, 'Migration lost the local acknowledgement mask'
    target.cfg(6, 4, alias=0x800)
    assert target.message(6) == 0xfeed0006
    assert target.pending(42)
    target.table(42)
    assert target.message(42) == 0xfeed002a and not target.pending(42)
    data = bytes.fromhex(target.command(f'read {BAR2:#x} 0x100').split()[1][2:])
    assert data == pattern, 'SRAM did not survive actual VMState migration'
    # PCI memory decode and reset are real PCI state, not an always-on aperture.
    target.write(ECAM + 4, 4, 'w')
    assert target.read(BAR1) != 0x20001927
    target.write(ECAM + 4, 6, 'w')
    assert target.read(BAR1) == 0x20001927
    qmp(target.monitor, 'system_reset')
    configure(target)
    for n in range(64):
        assert target.cfg(n) == 0
    assert target.read(CFG + 0x108) == target.read(CFG + 0x10c) == 0
    assert target.read(BAR0 + 0x2000) == target.read(BAR0 + 0x2004) == 0
    target.message(0, 0)
    target.cfg(0, 3)
    assert target.message(0) == 0xfeed0000
    print('PASS: RP1 PCI identity/BAR sizing/decode, all61 MSI-X DMA routes, raw '
          'levels/TEST/SET/CLR, vector/function masks/PBA, IACK race handling, '
          'SRAM and pending/blocked migration, reset and post-reset MSI')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    with ExitStack() as stack:
        root = args.output
        if root is None:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='rp1-test-')))
        else:
            root.mkdir(mode=0o700)
        exercise(args.qemu, root.resolve(), stack)


if __name__ == '__main__':
    main()
