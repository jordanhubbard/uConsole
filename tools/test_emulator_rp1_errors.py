#!/usr/bin/env python3
"""RP1 decoded receive-break IRQs and sticky overrun status on real QEMU.

QMP supplies already detected break events; this is not serial-bit or break
duration emulation. Check independent FIFO/error/IRQ clears and real VMState.
"""
import argparse
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path

from test_emulator_rp1 import Machine, configure, BAR1
from test_emulator_rp1_uart import VECTORS, send
from test_emulator_adc_migration import migrate
from uconsole_emulator import qmp

BE, OE = 0x200, 0x400


def setup(m, n, loopback=False):
    base = BAR1 + 0x30000 + n * 0x4000
    m.write(base + 0x38, 0)
    m.write(base + 0x30, 0x300)
    m.write(base + 0x2c, 0x60)
    m.write(base + 0x24, 3)
    m.write(base + 0x28, 0)
    m.write(base + 0x2c, 0x70)
    m.write(base + 0x44, 0x7ff)
    m.write(base + 4, 0xff)
    m.write(base + 0x30, 0x381 if loopback else 0x301)
    m.cfg(VECTORS[n], 13)
    m.message(VECTORS[n], 0)
    return base


def machine(binary, root, name, stack, incoming=False):
    # Each UART has a separate character backend; QMP break is an actual
    # chardev frontend event, rather than an injected FIFO or register value.
    args = [arg for n in range(6)
            for arg in ('-chardev', f'ringbuf,id=rx{n},size=1024')]
    m = Machine(root, name, stack, binary, incoming=incoming,
                endpoint_options=''.join(f',uart{n}=rx{n}' for n in range(6)),
                device_args=args)
    if not incoming:
        configure(m)
        qmp(m.monitor, 'cont')
    return m


def receive_break(m, n):
    qmp(m.monitor, 'chardev-send-break', {'id': f'rx{n}'})


def exercise_break(binary, root, passed, observations):
    with ExitStack() as stack:
        m = machine(binary, root, 'breaks', stack)
        for n, vector in enumerate(VECTORS):
            base = setup(m, n)
            receive_break(m, n)
            ris, mis = m.read(base + 0x3c), m.read(base + 0x40)
            observations[f'break{n}'] = {'ris': ris, 'mis': mis}
            assert ris & BE, ('Detected break lacks raw error IRQ', n, ris)
            assert not mis & BE and m.message(vector) == 0
            assert m.read(base + 4) == 0, 'RSR changed before its data read'
            m.write(base + 0x38, BE)
            assert m.read(base + 0x40) & BE
            assert m.message(vector) == 0xfeed0000 + vector
            assert m.read(base) == 0x400
            assert m.read(base + 0x18) & 0x10, 'Break queued more than one zero'
            assert m.read(base + 4) == 4
            assert m.read(base + 0x3c) & BE, 'Data read cleared latched IRQ'
            m.write(base + 4, 0xa5)
            assert m.read(base + 4) == 0
            assert m.read(base + 0x40) & BE, 'ECR cleared the independent IRQ'
            m.message(vector, 0)
            m.cfg(vector, 4, alias=0x800)
            assert m.message(vector) == 0xfeed0000 + vector
            m.write(base + 0x44, BE)
            assert not m.read(base + 0x40) & BE
            m.message(vector, 0)
            m.cfg(vector, 4, alias=0x800)
            assert m.message(vector) == 0
            passed.append(f'UART{n}: masked/unmasked break, actual MSI-X, separate DR/RSR/ECR/ICR and RP1 IACK')

        for n in range(6):
            for control in (0x300, 0x101, 0x381):
                base = setup(m, n)
                m.write(base + 0x30, control)
                receive_break(m, n)
                assert m.read(base + 0x18) & 0x10
                assert not m.read(base + 0x3c) & BE
                m.write(base + 0x30, 0x301)
                receive_break(m, n)
                assert m.read(base + 0x3c) & BE
                assert m.read(base) == 0x400
            passed.append(f'UART{n}: UARTEN, RXE and external-loopback gates refuse break events; recovery receives again')

        # Preserve ordered clean/error/clean characters and a pending masked
        # interrupt through the real migration stream.
        base = setup(m, 0, loopback=True)
        send(m, base, 0x51)
        m.write(base + 0x30, 0x301)
        receive_break(m, 0)
        m.write(base + 0x30, 0x381)
        send(m, base, 0x52)
        target = machine(binary, root, 'break-target', stack, incoming=True)
        clock = int(m.command('clock_step 1').split()[1], 0)
        target.command(f'clock_set {clock}')
        migrate(m, target, root)
        qmp(target.monitor, 'cont')
        assert target.read(base + 0x3c) & BE
        assert not target.read(base + 0x40) & BE
        assert target.read(base + 4) == 0
        for data, status in ((0x51, 0), (0x400, 4), (0x52, 0)):
            assert target.read(base) == data
            assert target.read(base + 4) == status
        target.write(base + 0x38, BE)
        assert target.message(25) == 0xfeed0000 + 25
        passed.append('Real migration retains FIFO error ordering and masked break IRQ; destination unmasks actual MSI-X')
        qmp(target.monitor, 'system_reset')
        configure(target)
        assert target.read(base + 0x18) & 0x10
        assert target.read(base + 4) == 0
        assert not target.read(base + 0x3c) & (BE | OE)
        passed.append('Reset removes queued data, receiver error status and latched error interrupts')


def exercise_overrun(binary, root, passed, observations):
    with ExitStack() as stack:
        m = machine(binary, root, 'overruns', stack)
        for n, vector in enumerate(VECTORS):
            base = setup(m, n, loopback=True)
            m.write(base + 0x38, OE)
            for byte in range(32):
                send(m, base, byte)
            send(m, base, 0xa5)
            assert m.read(base + 4) == 8
            assert m.read(base + 0x40) & OE
            assert m.message(vector) == 0xfeed0000 + vector
            assert m.read(base) == 0
            status = m.read(base + 4)
            observations[f'overrun{n}_after_data'] = status
            assert status == 8, ('DR read cleared sticky RSR.OE before ECR', n, status)
            # ICR clears the interrupt without clearing sticky receive status.
            m.write(base + 0x44, OE)
            assert not m.read(base + 0x40) & OE
            assert m.read(base + 4) == 8
            for byte in range(1, 32):
                assert m.read(base) == byte, 'Full FIFO overwrote retained data'
                assert m.read(base + 4) == 8
            m.write(base + 4, 0xff)
            assert m.read(base + 4) == 0
            send(m, base, 0x5a)
            assert m.read(base) == 0x5a and m.read(base + 4) == 0
            passed.append(f'UART{n}: full FIFO preserves all data; RSR.OE survives DR/ICR until ECR; clean input recovers')

        base = setup(m, 0, loopback=True)
        for byte in range(32):
            send(m, base, byte)
        m.write(base + 0x30, 0x301)
        receive_break(m, 0)
        assert m.read(base + 0x3c) & (BE | OE) == BE | OE
        assert m.read(base + 4) == 8
        assert m.read(base) == 0 and m.read(base + 4) == 8
        target = machine(binary, root, 'overrun-target', stack, incoming=True)
        clock = int(m.command('clock_step 1').split()[1], 0)
        target.command(f'clock_set {clock}')
        migrate(m, target, root)
        qmp(target.monitor, 'cont')
        assert target.read(base + 4) == 8
        assert target.read(base + 0x3c) & (BE | OE) == BE | OE
        target.write(base + 4, 0)
        assert target.read(base + 4) == 0
        assert target.read(base + 0x3c) & (BE | OE) == BE | OE
        for byte in range(1, 32):
            assert target.read(base) == byte
            assert target.read(base + 4) == 0
        assert target.read(base + 0x18) & 0x10
        passed.append('Break at full FIFO latches BE/OE without overwrite; real migration retains sticky status and both IRQs')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--only', choices=('all', 'break', 'overrun'), default='all')
    args = parser.parse_args()
    args.output.mkdir(mode=0o700)
    binary = Path(args.qemu).resolve(strict=True)
    identity = hashlib.sha256(binary.read_bytes()).hexdigest()
    result = {'ok': False, 'scope': __doc__, 'qemu_sha256': identity,
              'harness_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'selected': args.only, 'passed': [], 'observations': {}}
    try:
        if args.only in ('all', 'break'):
            exercise_break(str(binary), args.output, result['passed'], result['observations'])
        if args.only in ('all', 'overrun'):
            exercise_overrun(str(binary), args.output, result['passed'], result['observations'])
        assert hashlib.sha256(binary.read_bytes()).hexdigest() == identity
        result['ok'] = True
    except BaseException as error:
        result['error'] = repr(error)
        raise
    finally:
        (args.output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
