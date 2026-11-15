#!/usr/bin/env python3
"""Native CQHCI register/card-command/IRQ/migration/refusal controls.

The ordinary SD v2/v3 backend does not advertise command queuing. Its queued
data operations must fail without a fake completion or storage write. Positive
queued-data DMA with a CQ-capable card remains a separate acceptance gate.
"""
import argparse
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import traceback

from test_emulator_pcie_xhci import Machine
from test_emulator_pi5_boot import ram_profile
from test_emulator_pmic import connect
from test_emulator_adc_migration import migrate
from uconsole_emulator import qmp

SD = 0x1000fff000
CQ = SD + 0x200
GIC = 0x107fff9000
TABLE = 0x100000


def digest(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


class Controller(Machine):
    def __init__(self, root, name, stack, binary, *, card=None, incoming=False, profile=None,
                 emmc=False, memory_gib=1):
        sockets = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='cq-')))
        self.monitor = sockets / 'qmp'
        test = sockets / 'qtest'
        log = stack.enter_context((root / (name + '.log')).open('wb'))
        machine = 'raspi5b'
        if profile is not None:
            machine += ',sd-cqhci=' + ('on' if profile else 'off')
        if emmc:
            machine += ',sd-emmc=on'
        machine, ram_args = ram_profile(machine, memory_gib)
        command = [str(binary), '-M', machine, '-m', str(memory_gib) + 'G', '-accel', 'qtest', '-S',
                   '-display', 'none', '-serial', 'none', '-monitor', 'none', '-nic', 'none',
                   '-qtest', f'unix:{test},server=on,wait=off',
                   '-qmp', f'unix:{self.monitor},server=on,wait=off']
        command += ram_args
        if card:
            command += ['-drive', f'if=sd,format=raw,file={card}' +
                        (',cache=writethrough' if emmc else '')]
        if incoming:
            command += ['-incoming', 'defer']
        self.process = subprocess.Popen(command, stdout=log, stderr=log)
        stack.callback(self.close)
        try:
            self.sock = stack.enter_context(connect(test, self.process))
        except Exception as exc:
            raise RuntimeError(Path(log.name).read_text(errors='replace')[-4096:]) from exc
        self.stream = stack.enter_context(self.sock.makefile('rwb', buffering=0))

    def step(self, ns=1000):
        return int(self.command(f'clock_step {ns}').split()[1], 0)

    def bytes(self, address, data):
        self.command(f'write {address:#x} {len(data):#x} 0x{data.hex()}')


def initialize_card(m):
    m.write(SD + 0x29, 0x0f, 'b')
    m.write(SD + 0x2c, 7, 'w')
    m.write(SD + 0x28, 0x12, 'b')
    m.write(SD + 0x34, 0x03ff7fff)
    def command(number, arg=0, response=0x1a):
        m.write(SD + 0x30, 0xffffffff)
        m.write(SD + 8, arg)
        m.write(SD + 0xe, (number << 8) | response, 'w')
        status = m.read(SD + 0x30)
        assert not status & 0xffff8000, (number, hex(status))
        return m.read(SD + 0x10)
    command(0, response=0)
    assert command(8, 0x1aa) & 0xfff == 0x1aa
    for _ in range(25):
        command(55)
        ocr = command(41, 0x40ff8000, 2)
        if ocr & 0x80000000:
            break
        m.step(10_000_000)
    assert ocr & 0x80000000, 'actual card never powered up'
    command(2, response=9)
    rca = command(3) >> 16
    assert rca
    command(7, rca << 16, 0x1b)
    status = command(13, rca << 16)
    assert status & 0x1e00 == 4 << 9, hex(status)
    return rca


def exercise(binary, legacy, root, check):
    with ExitStack() as stack:
        card = root / 'card.raw'
        with card.open('xb') as f:
            f.truncate(64 * 1024**2)
        card_sha = digest(card)
        m = Controller(root, 'source', stack, binary, card=card)
        check(m.read(CQ) == 0x510 and m.read(CQ + 4) == 0x1064, 'actual CQHCI version and virtual timer profile')
        m.write(CQ, 0xffffffff)
        m.write(CQ + 4, 0xffffffff)
        check(m.read(CQ) == 0x510 and m.read(CQ + 4) == 0x1064, 'read-only identities resist writes')
        qmp(m.monitor, 'cont')
        rca = initialize_card(m)
        check(True, 'real SD card initialization and CMD13 transfer state')
        # Capture, CQ signal, SDHCI signal and real GIC input are separate gates.
        m.write(GIC, 1)
        m.write(GIC + 0x931, 1, 'b')  # SPI273 / INTID305 => CPU0
        m.write(GIC + 0x124, 1 << 17)
        m.write(CQ + 0x14, 1)
        m.write(CQ + 0xc, 1)
        check(m.read(CQ + 0x10) == 1, 'HALT completion captured')
        check(not m.read(SD + 0x30) & 0x4000, 'CQ signal mask prevents SDHCI CQ source')
        m.write(CQ + 0x18, 1)
        check(bool(m.read(SD + 0x30) & 0x4000), 'unmasking CQ signal presents actual SDHCI status')
        check(not m.read(GIC + 0x224) & (1 << 17), 'SDHCI signal mask prevents GIC IRQ')
        m.write(SD + 0x38, 0x4000)
        check(bool(m.read(GIC + 0x224) & (1 << 17)), 'CQ source reaches declared GIC SPI')
        m.write(SD + 0x30, 0x4000)
        check(bool(m.read(SD + 0x30) & 0x4000), 'SDHCI W1C cannot clear an asserted CQ source')
        m.write(CQ + 0x10, 1)
        m.write(GIC + 0x2a4, 1 << 17)
        check(not m.read(SD + 0x30) & 0x4000 and not m.read(GIC + 0x224) & (1 << 17), 'CQ acknowledgement lowers source and GIC line')
        m.write(CQ + 0x14, 0)
        m.write(CQ + 0xc, 0)
        m.write(CQ + 0xc, 1)
        check(m.read(CQ + 0x10) == 0, 'disabled status capture does not latch HALT')
        m.write(CQ + 0x14, 0xf)
        m.write(CQ + 0x18, 0xf)
        m.write(CQ + 0x20, TABLE | 1023)
        check(m.read(CQ + 0x20) == TABLE, 'TDL alignment follows hardware interface')
        m.write(CQ + 0x24, 0)
        m.write(CQ + 0x44, rca)
        m.write(CQ + 8, 0x1001)
        m.write(CQ + 8, 0x1101)
        check(m.read(CQ + 8) == 0x1001, 'enabled configuration cannot change descriptor format')
        descriptor = 0x2f | (1 << 14) | (13 << 16) | (1 << 22) | (2 << 23) | ((rca << 16) << 32)
        m.bytes(TABLE + 31 * 16, struct.pack('<QQ', descriptor, 0))
        m.write(CQ + 0x28, 1 << 31)
        clock = m.step(10000)
        check(m.read(CQ + 0x28) == 1 << 31 and m.read(CQ + 0x48) == 0, 'HALT keeps real descriptor DMA pending')
        target_card = root / 'target-card.raw'
        shutil.copyfile(card, target_card)
        target = Controller(root, 'target', stack, binary, card=target_card, incoming=True)
        migrate(m, target, root)
        target.command(f'clock_set {clock}')
        qmp(target.monitor, 'cont')
        check(target.read(CQ + 0x28) == 1 << 31, 'queued doorbell migrates without fake completion')
        target.write(CQ + 0xc, 0)
        target.step(1000)
        check(target.read(CQ + 0x28) == 0 and target.read(CQ + 0x2c) == 1 << 31, 'migrated descriptor executes against actual card')
        response = target.read(CQ + 0x48)
        check(response & 0x1e00 == 4 << 9, 'DCMD returns actual SD status rather than fabricated data')
        check(target.read(CQ + 0x10) & 2, 'actual DCMD completion raises TCC')
        target.write(CQ + 0x2c, 1 << 31)
        check(target.read(CQ + 0x10) & 2, 'TCN and interrupt acknowledgements are independent')
        target.write(CQ + 0x10, 0x3f)
        # Current SD v2/v3 has no queue protocol: data requests must not succeed.
        target.bytes(TABLE + 5 * 16, struct.pack('<QQ', 0x2f | (1 << 12) | (1 << 16), (0x200000 << 32) | 0x31))
        target.bytes(0x200000, struct.pack('<II', (512 << 16) | 0x23, 0x300000))
        target.bytes(0x300000, b'Z' * 512)
        target.write(CQ + 0x28, 1 << 5)
        for _ in range(4):
            target.step(1000)
        check(bool(target.read(CQ + 0x10) & 4), 'unsupported card queue reports error')
        check(target.read(CQ + 0x54) & 0x9f3f == 0x852c, 'queue error identifies task5 and actual CMD44')
        check(not target.read(CQ + 0x2c) & (1 << 5), 'failed queue request is not completed successfully')
        check(target.read(0x300000) == 0x5a5a5a5a and digest(target_card) == card_sha, 'refused data queue changes neither DMA destination nor backing storage')
        target.write(CQ + 0xc, 1)
        target.write(CQ + 0x10, 0x3f)
        target.write(CQ + 0x28, 0xffffffff)
        check(target.read(CQ + 0x28) == 0xffffffff, 'all32 slots remain pending under HALT')
        target.write(CQ + 0x38, 1 << 7)
        check(target.read(CQ + 0x28) == 0xffffff7f and target.read(CQ + 0x38) == 0, 'individual task clear is self-clearing and preserves peers')
        target.write(CQ + 0xc, 0x101)
        check(target.read(CQ + 0x28) == 0 and target.read(CQ + 0x10) & 8, 'halted clear-all has real completion status')
        target.write(CQ + 0x28, 1)
        target.write(SD + 0x2f, 1, 'b')
        target.step(100000)
        check(target.read(CQ + 8) == 0 and target.read(CQ + 0x28) == 0 and target.read(CQ + 0x10) == 0, 'SDHCI software reset cancels CQ queues and timers')
        check(target.read(CQ) == 0x510 and target.read(CQ + 4) == 0x1064, 'CQ identities survive reset')
        check(digest(card) == card_sha and digest(target_card) == card_sha, 'synthetic source/target card bytes unchanged')
        target.write(CQ + 0x14, 0x3f)
        target.write(CQ + 0x20, TABLE)
        target.bytes(TABLE + 4 * 16, bytes(16))
        target.write(CQ + 8, 0x1001)
        target.write(CQ + 0x28, 1 << 4)
        target.step(1000)
        check(target.read(CQ + 0x10) & 4 and target.read(CQ + 0x3c) == 1 << 4,
              'invalid guest task descriptor reports response error and exact failed tag')
        check(target.read(CQ + 0x28) == 0 and target.read(CQ + 0x2c) == 0,
              'invalid descriptor retires without a successful completion')
        target.write(SD + 0x2f, 1, 'b')
        target.write(CQ + 0x14, 0x3f)
        target.write(CQ + 0x20, TABLE)
        target.bytes(TABLE + 31 * 24, struct.pack('<QQ', descriptor, 1))
        target.write(CQ + 8, 0x1101)
        target.write(CQ + 0x28, 1 << 31)
        target.step(1000)
        check(target.read(CQ + 0x10) & 0x24 == 0x24 and
              target.read(CQ + 0x3c) == 1 << 31 and target.read(CQ + 0x2c) == 0,
              'unadvertised crypto context fails without plaintext fallback')
        target.write(SD + 0x2f, 1, 'b')
        target.write(CQ + 0xc, 1)
        target.write(CQ + 8, 0x1001)
        target.write(CQ + 0x28, 0xffffffff)
        target.write(CQ + 8, 0x1000)
        check(target.read(CQ + 0x28) == 0 and target.read(CQ + 0x2c) == 0,
              'disabling CQE discards every pending doorbell without false completion')
        target.write(CQ + 8, 0x1001)
        target.write(CQ + 0xc, 0)
        target.step(10000)
        check(target.read(CQ + 0x28) == 0 and target.read(CQ + 0x2c) == 0,
              're-enabling CQE never resurrects discarded descriptors')
        target.write(CQ + 8, 0x1000)
        target.write(CQ + 0x20, TABLE)
        target.bytes(TABLE + 2 * 16, struct.pack('<QQ', 0x2f | (1 << 12) | (1 << 16),
                                               (0x200000 << 32) | 0x31))
        target.write(CQ + 8, 0x1001)
        target.write(CQ + 0x28, 1 << 2)
        target.step(1000)
        check(target.read(CQ + 0x28) == 1 << 2 and target.read(CQ + 0x58) == 0,
              'task descriptor fetched before the first card queue command')
        target.write(CQ + 8, 0x1000)
        target.write(CQ + 8, 0x1001)
        target.step(10000)
        check(target.read(CQ + 0x28) == 0 and target.read(CQ + 0x58) == 0 and
              target.read(CQ + 0x2c) == 0 and target.read(CQ + 0x34) == 0,
              'disable after descriptor fetch cancels its scheduled card command')
        check(digest(card) == card_sha and digest(target_card) == card_sha,
              'descriptor refusal and disable leave both backing cards unchanged')
        # Profile mismatch must never silently drop the new controller state.
        for index, (source_profile, target_profile) in enumerate(((True, False), (False, True))):
            source = Controller(root, f'ms{index}', stack, binary, profile=source_profile)
            receiver = Controller(root, f'mt{index}', stack, binary, incoming=True, profile=target_profile)
            endpoint = 'unix:' + str(source.monitor.parent / 'migration')
            qmp(receiver.monitor, 'migrate-incoming', {'uri': endpoint})
            qmp(source.monitor, 'migrate', {'uri': endpoint})
            check(receiver.process.wait(timeout=30) != 0, f'profile mismatch {source_profile}/{target_profile} refused')
        if legacy:
            for index, reverse in enumerate((False, True)):
                source = Controller(root, f'ls{index}', stack, binary if reverse else legacy,
                                    profile=False if reverse else None)
                receiver = Controller(root, f'lt{index}', stack, legacy if reverse else binary,
                                      incoming=True, profile=None if reverse else False)
                source.write(SD + 0x34, 0x1234)
                migrate(source, receiver, root)
                check(receiver.read(SD + 0x34) == 0x1234, f'actual legacy wire migration direction{index}')
        mutable = Controller(root, 'mutable', stack, binary)
        refused = False
        try:
            qmp(mutable.monitor, 'qom-set', {'path': '/machine', 'property': 'sd-cqhci', 'value': False})
        except ValueError as exc:
            assert 'before machine initialization' in str(exc), exc
            refused = True
        check(refused, 'hardware profile cannot change after initialization')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', type=Path, required=True)
    parser.add_argument('--legacy-qemu', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(mode=0o700, parents=True)
    binary = args.qemu.resolve()
    legacy = args.legacy_qemu.resolve() if args.legacy_qemu else None
    inputs = {str(p): digest(p) for p in (binary, legacy) if p}
    report = {'ok': False, 'scope': __doc__, 'inputs': inputs, 'passed': [],
              'harness_sha256': digest(Path(__file__))}
    def check(condition, label):
        assert condition, label
        report['passed'].append(label)
    try:
        exercise(binary, legacy, args.output, check)
        report['ok'] = True
    except BaseException:
        report['error'] = traceback.format_exc()
        raise
    finally:
        report['inputs_unchanged'] = {p: digest(Path(p)) == sha for p, sha in inputs.items()}
        if not all(report['inputs_unchanged'].values()):
            report['ok'] = False
        (args.output / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
    print('PASS CQHCI controls:', len(report['passed']))


if __name__ == '__main__':
    main()
