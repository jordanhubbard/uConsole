#!/usr/bin/env python3
"""Real BCM2711 endpoint MSI writes, controller decoding, GIC and migration.

The compact QEMU xHCI endpoint exposes ordinary 64-bit MSI, not MSI-X or VL805
identity. These software controls do not qualify physical PCIe timing.
"""
import argparse
import hashlib
import json
import shutil
import traceback
from contextlib import ExitStack
from pathlib import Path
import struct
import tempfile

from test_emulator_adc_migration import Machine as Disconnected, migrate
from test_emulator_pcie import firmware_sequence, irq
from test_emulator_pcie_xhci import Machine, configure, rings, noop, acknowledge, CMD, EVENT
from uconsole_emulator import executable, qmp


class MSIMachine(Machine):
    def irq(self):
        # Native MSI-bank SPI148, architectural interrupt180.
        return bool(self.read(0xff841214) & (1 << 20))


def capabilities(m):
    found = {}; seen = set(); cap = m.config(1, 0, 0, 0x34) & 255
    while cap:
        assert 0x40 <= cap <= 0xfc and cap % 4 == 0 and cap not in seen
        seen.add(cap); word = m.config(1, 0, 0, cap)
        found[word & 255] = cap; cap = (word >> 8) & 255
    assert 5 in found, 'Connected native xHCI endpoint has no ordinary MSI capability'
    assert 0x11 not in found, 'Compact aperture must not advertise an MSI-X table'
    return found


def message(m, cap, vector=0, address=0xffffffffc, data=None):
    header = m.config(1, 0, 0, cap)
    assert header & (1 << 23), 'Endpoint must support the native 64-bit MSI target'
    m.config(1, 0, 0, cap + 4, address & 0xffffffff)
    m.config(1, 0, 0, cap + 8, address >> 32)
    m.config(1, 0, 0, cap + 12, (0x6540 | vector) if data is None else data)
    m.config(1, 0, 0, cap, header | (1 << 16))


def target(m, address=0xffffffffc, enable=True):
    m.reg(0x4044, (address & 0xfffffffc) | int(enable))
    m.reg(0x4048, address >> 32)
    m.reg(0x404c, 0xffe06540)
    m.reg(0x4508, 0xffffffff)
    m.reg(0x4514, 0xffffffff)


def complete(m, number=0, expected_irq=True):
    command = CMD + number * 16
    m.memory(command, struct.pack('<QII', 0, 0, (23 << 10) | 1))
    m.xwrite(m.doorbell, 0)
    pointer, status, control = struct.unpack('<QII', m.inspect(EVENT + number * 16, 16))
    assert pointer == m.dma_base + command and status >> 24 == 1
    assert (control >> 10) & 63 == 33 and control & 1
    assert m.irq() == expected_irq, 'Actual xHCI MSI completion/GIC delivery mismatch'
    # MSI-enabled endpoint must not also assert the legacy INTx A input.
    assert not m.read(0xff841214) & (1 << 15)


def drain(m, next_event=1):
    intr = m.runtime + 0x20
    m.xwrite(intr, 3)
    m.xwrite(m.oper + 4, 8)
    pointer = m.dma_base + EVENT + next_event * 16
    m.xwrite(intr + 0x18, (pointer | 8) & 0xffffffff)
    m.xwrite(intr + 0x1c, pointer >> 32)
    m.reg(0x4508, 0xffffffff)
    m.reg(0x4060, 1)  # Original FreeBSD controller EOI sequence.
    assert m.reg(0x4500) == 0 and not m.irq()


def run(binary, root):
    with ExitStack() as stack:
        m = MSIMachine(root, 'source', stack, binary)
        configure(m); cap = capabilities(m)[5]; target(m)
        for vector in range(32):
            message(m, cap, vector); rings(m); complete(m)
            assert m.reg(0x4500) == 1 << vector
            drain(m)
        print('PASS: all32 real endpoint MSI vectors, high target, GIC and independent INTx')
        message(m, cap, 31); m.reg(0x4510, 1 << 31); rings(m)
        complete(m, expected_irq=False)
        assert m.reg(0x4500) == 1 << 31
        m.reg(0x4514, 1 << 31); assert m.irq(); drain(m)
        for address, data, enable in [(0xffffffffc, 0x7540, True),
                                      (0xffffffff8, 0x6540, True),
                                      (0xffffffffc, 0x6540, False)]:
            target(m, enable=enable); message(m, cap, address=address, data=data)
            rings(m); complete(m, expected_irq=False)
            assert m.reg(0x4500) == 0; drain(m)
        # The PCI MSI decoder wins over the RAM inbound window; CPU RAM remains
        # ordinary RAM and must not be corrupted by an interrupt message.
        address = 0x02000000
        m.memory(address, b'\xcd' * 4); target(m, address); message(m, cap, 17, address)
        rings(m); complete(m); assert m.reg(0x4500) == 1 << 17
        assert m.inspect(address, 4) == b'\xcd' * 4; drain(m)
        print('PASS: masked pending delivery, message/address/enable filtering and RAM isolation')
        target(m); message(m, cap, 7); rings(m); complete(m)
        n = MSIMachine(root, 'target', stack, binary, incoming=True)
        migrate(m, n, root)
        n.oper, n.runtime, n.doorbell = m.oper, m.runtime, m.doorbell
        assert n.irq() and n.reg(0x4500) == 1 << 7
        drain(n); complete(n, 1); drain(n, 2)
        qmp(n.monitor, 'system_reset')
        assert not n.irq() and n.reg(0x4500) == 0 and n.reg(0x4044) == 0
        configure(n); cap = capabilities(n)[5]; target(n); message(n, cap, 19)
        rings(n); complete(n); assert n.reg(0x4500) == 1 << 19; drain(n)
        print('PASS: pending MSI/endpoint migration continuation and complete reset/reprogramming')


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def preserve_logs(root, output):
    if output is None:
        return []
    output.mkdir(parents=True, exist_ok=True)
    logs = []
    for source in sorted(root.iterdir()):
        if source.is_file() and not source.is_symlink():
            target = output / source.name
            shutil.copy2(source, target)
            logs.append(dict(path=str(target), bytes=target.stat().st_size,
                             sha256=sha(target)))
    return logs


def require_original_endpoint(machine):
    """A new executable must not stand in for the optional old MSI-less binary."""
    cap = machine.config(1, 0, 0, 0x34) & 255
    seen = set()
    while cap:
        assert 0x40 <= cap <= 0xfc and cap % 4 == 0 and cap not in seen
        seen.add(cap)
        word = machine.config(1, 0, 0, cap)
        assert word & 255 not in (5, 0x11), 'Legacy executable already exposes MSI/MSI-X'
        cap = (word >> 8) & 255


def profiles(binary, legacy=None, output=None):
    """Real streams, retaining every selected case and explicit legacy omissions."""
    inventory = [
        ('old-to-new-disconnected', 'old', 'new', 'disconnected', False),
        ('new-to-old-disconnected', 'new', 'old', 'disconnected', False),
        ('old-to-new-legacy-INTx', 'old', 'new', 'legacy', False),
        ('new-to-old-legacy-INTx', 'new', 'old', 'legacy', False),
        ('new-MSI-to-new-legacy-refusal', 'new-msi', 'new', 'mismatch', True),
        ('new-legacy-to-new-MSI-refusal', 'new', 'new-msi', 'mismatch', True),
        ('new-MSI-to-old-refusal', 'new-msi', 'old', 'mismatch', True),
        ('old-to-new-MSI-refusal', 'old', 'new-msi', 'mismatch', True),
    ]
    selected = [case for case in inventory if legacy or 'old' not in case[1:3]]
    result = dict(schema='uconsole/pi4-msi-profile-controls@1',
                  candidate_sha256=sha(binary),
                  legacy_sha256=sha(legacy) if legacy else None,
                  case_inventory=[case[0] for case in selected],
                  unexecuted_legacy_cases=[case[0] for case in inventory if case not in selected],
                  cases=[], scope='Software model streams; no guest OS or physical acceptance.')

    def save():
        if output is not None:
            (output / 'profile-results.json').write_text(json.dumps(result, indent=2) + '\n')

    save()
    for name, src, dst, kind, reject in selected:
        row = dict(id=name, status='running', expected_rejection=reject)
        result['cases'].append(row)
        source = target_machine = None
        with tempfile.TemporaryDirectory(prefix='uc-pm-', dir='/tmp') as temporary:
            root = Path(temporary)
            try:
                with ExitStack() as stack:
                    def machine(which, label, incoming=False):
                        executable_path = legacy if which == 'old' else binary
                        if kind == 'disconnected':
                            return Disconnected(root, label, stack, incoming=incoming,
                                                base=0xfd500000, binary=executable_path)
                        flags = (() if which in ('old', 'new-msi') else
                                 ('-global', 'bcm2838-pcie-disconnected.endpoint-msi=off'))
                        return Machine(root, label, stack, executable_path,
                                       incoming=incoming, device_args=flags)

                    source = machine(src, 'source')
                    target_machine = machine(dst, 'target', True)
                    row.update(source_pid=source.process.pid, target_pid=target_machine.process.pid)
                    save()
                    if kind == 'disconnected':
                        firmware_sequence(source)
                        for bank in (0x4300, 0x4500):
                            source.write(bank + 4, 0x10)
                            source.write(bank + 0x14, 0x10)
                        assert irq(source, 0) and irq(source, 1)
                        migrate(source, target_machine, root)
                        assert irq(target_machine, 0) and irq(target_machine, 1)
                        firmware_sequence(target_machine)
                        target_machine.write(0x9210, 2)
                        assert target_machine.read(0x4500) == 0
                        assert not irq(target_machine, 0) and not irq(target_machine, 1)
                    else:
                        configure(source)
                        if src == 'old':
                            require_original_endpoint(source)
                        if dst == 'old':
                            # The incoming target is already realized; its PCI
                            # function can be inspected before loading a stream.
                            configure(target_machine)
                            require_original_endpoint(target_machine)
                        if reject:
                            endpoint = 'unix:' + str(root / 'migration')
                            qmp(target_machine.monitor, 'migrate-incoming', {'uri': endpoint})
                            qmp(source.monitor, 'migrate', {'uri': endpoint})
                            code = target_machine.process.wait(timeout=60)
                            assert code != 0, 'Incompatible connected MSI profile was accepted'
                            row['actual_refusal_target_exit'] = code
                            text = (root / 'target.log').read_text(errors='replace')
                            assert 'bcm2838-pcie-disconnected' in text
                            assert 'load' in text.lower() or 'subsection' in text.lower()
                        else:
                            rings(source)
                            noop(source, 0)
                            migrate(source, target_machine, root)
                            target_machine.oper = source.oper
                            target_machine.runtime = source.runtime
                            target_machine.doorbell = source.doorbell
                            assert target_machine.irq()
                            acknowledge(target_machine, 1)
                            noop(target_machine, 1)
                            acknowledge(target_machine, 2)
                    row['status'] = 'pass'
            except Exception:
                row.update(status='fail', error=traceback.format_exc())
            finally:
                row['source_teardown_exit'] = source.process.poll() if source else None
                row['target_teardown_exit'] = target_machine.process.poll() if target_machine else None
                row['closed_native_logs'] = preserve_logs(root, output / name if output else None)
                save()
                print(f"{row['status'].upper()}: {name}", flush=True)
    result['passed'] = sum(row['status'] == 'pass' for row in result['cases'])
    result['failed'] = sum(row['status'] == 'fail' for row in result['cases'])
    save()
    if result['unexecuted_legacy_cases']:
        print('NOT EXECUTED: six cross-version cases require --legacy-qemu', flush=True)
    assert not result['failed'], result
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--qemu', default='qemu-system-aarch64')
    ap.add_argument('--legacy-qemu', help='actual older MSI-less executable for six cross-version controls')
    ap.add_argument('--output', type=Path, help='fresh private directory retaining closed QEMU logs and results')
    args = ap.parse_args()
    binary = executable(args.qemu)
    legacy = executable(args.legacy_qemu) if args.legacy_qemu else None
    if args.output is not None:
        if args.output.exists():
            ap.error('--output already exists; preserve it and select a fresh directory')
        args.output.mkdir(parents=True, mode=0o700)
    # Unix sockets always use short temporary paths, independent of the evidence
    # directory's length. The original full32-vector control is unchanged.
    with tempfile.TemporaryDirectory(prefix='uc-pm-run-', dir='/tmp') as temporary:
        root = Path(temporary)
        try:
            run(binary, root)
        finally:
            preserve_logs(root, args.output / 'msi' if args.output else None)
    profiles(binary, legacy, args.output)


if __name__ == '__main__':
    main()
