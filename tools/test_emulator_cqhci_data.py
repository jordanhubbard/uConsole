#!/usr/bin/env python3
"""Real block-backed virtual eMMC queue and CQHCI DMA controls.

The opt-in eMMC/64-bit host profile is a transport test fixture, not physical
Pi5 storage or a complete eMMC 5.1 feature qualification.
"""
import argparse
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import shutil
import struct
import traceback

from test_emulator_pi5_cqhci import Controller, CQ, SD, digest, migrate, qmp

RCA = 1
TABLE, SG, A, B = 0x100000, 0x200000, 0x300000, 0x340000


def read_bytes(m, address, length):
    return bytes.fromhex(m.command(f'read {address:#x} {length:#x}').split()[1][2:])


def command(m, number, arg=0, flags=0x1a, *, refusal=False):
    m.write(SD + 0x30, 0xffffffff)
    m.write(SD + 8, arg)
    m.write(SD + 0xe, (number << 8) | flags, 'w')
    status = m.read(SD + 0x30)
    if refusal:
        assert status & 0xffff8000, (number, hex(status), 'expected refusal')
    else:
        assert not status & 0xffff8000, (number, hex(status))
    return m.read(SD + 0x10)


def initialize(m, check):
    qmp(m.monitor, 'cont')
    m.write(SD + 0x29, 15, 'b')
    m.write(SD + 0x2c, 7, 'w')
    m.write(SD + 0x28, 0x12, 'b')
    m.write(SD + 0x34, 0x03ff7fff)
    command(m, 0, flags=0)
    for _ in range(25):
        ocr = command(m, 1, 0x40ff8080, 2)
        if ocr & 0x80000000:
            break
        m.step(10_000_000)
    assert ocr & 0x80000000, 'actual eMMC never powered up'
    command(m, 2, flags=9)
    command(m, 3, RCA << 16)
    command(m, 7, RCA << 16, 0x1b)
    m.write(SD + 4, (1 << 16) | 512)
    m.write(SD + 0xc, 0x12, 'w')
    command(m, 8, flags=0x3a)
    ext = b''.join(m.read(SD + 0x20).to_bytes(4, 'little') for _ in range(128))
    check(ext[192] == 8 and ext[307] == 31 and ext[308] & 1 and ext[15] == 0,
          'real EXT_CSD advertises queue support/depth with mode initially off')
    command(m, 44, (1 << 30) | (2 << 16) | 1, refusal=True)
    command(m, 13, RCA << 16)  # Observe/clear the refused command's R1 error.
    command(m, 6, (3 << 24) | (15 << 16) | (1 << 8), 0x1b)
    check(command(m, 13, (RCA << 16) | 0x8000) == 0,
          'queue mode is enabled through CMD6 and starts empty')


def pattern(lba):
    return bytes((i * 17 + lba * 29) & 255 for i in range(512))


def card_file(root, name):
    p = root / (name + '.raw')
    with p.open('xb') as f:
        f.truncate(64 * 1024**2)
        f.seek(8 * 512)
        f.write(b''.join(pattern(i) for i in range(8, 16)))
    return p


def submit_card(m, tag, blocks=1, lba=8, read=True):
    command(m, 44, (int(read) << 30) | (tag << 16) | blocks)
    command(m, 45, lba)


def ready(m):
    return command(m, 13, (RCA << 16) | 0x8000)


def configure(m, *, wide=False):
    m.write(CQ + 8, 0)
    m.write(SD + 0x28, 0x1a if wide else 0x12, 'b')
    m.write(CQ + 0x20, TABLE & 0xffffffff)
    m.write(CQ + 0x24, TABLE >> 32)
    m.write(CQ + 0x44, RCA)
    m.write(CQ + 0x40, 1)
    m.write(CQ + 0x14, 0x3f)
    m.write(CQ + 0x18, 0x3f)
    m.write(CQ + 0x10, 0x3f)
    m.write(CQ + 0x2c, 0xffffffff)
    m.write(CQ + 0xc, 0)
    m.write(CQ + 8, 0x1101 if wide else 0x1001)


def task(m, tag, segments, *, read=True, lba=8, wide=False, interrupt=True):
    total = sum(length for _, length in segments)
    assert total % 512 == 0
    descriptor = 0x2b | (int(interrupt) << 2) | (int(read) << 12) | ((total // 512) << 16) | (lba << 32)
    if wide:
        m.bytes(TABLE + tag * 32, struct.pack('<QQ', descriptor, 0) +
                struct.pack('<IQI', 0x31, SG, 0))
    else:
        m.bytes(TABLE + tag * 16, struct.pack('<QII', descriptor, 0x31, SG))
    for index, (address, length) in enumerate(segments):
        assert 0 < length <= 65536
        attr = ((length & 0xffff) << 16) | 0x21 | (2 if index == len(segments) - 1 else 0)
        m.bytes(SG + index * (16 if wide else 8),
                struct.pack('<IQI', attr, address, 0) if wide else
                struct.pack('<II', attr, address))
    m.write(CQ + 0x28, 1 << tag)


def finish(m, tag, *, interrupt=True, steps=100):
    for _ in range(steps):
        clock = m.step(1000)
        if m.read(CQ + 0x10) & 4:
            raise AssertionError(('actual CQ error', hex(m.read(CQ + 0x54))))
        if m.read(CQ + 0x2c) & (1 << tag):
            assert not m.read(CQ + 0x28) & (1 << tag)
            assert bool(m.read(CQ + 0x10) & 2) == interrupt
            m.write(CQ + 0x2c, 1 << tag)
            m.write(CQ + 0x10, 0x3f)
            return clock
    raise TimeoutError('Actual queued data task did not complete')


def adma_initialization(binary, root, check):
    # Linux initializes eMMC through ordinary SDHCI ADMA before enabling CQE.
    # SDMA is deliberately absent from this virtual host's capabilities.
    with ExitStack() as stack:
        card = card_file(root, 'adma-init')
        original_sha = digest(card)
        m = Controller(root, 'adma-init', stack, binary, card=card, emmc=True)
        initialize(m, check)
        command(m, 6, (3 << 24) | (15 << 16), 0x1b)
        check(not m.read(SD + 0x40) & (1 << 22) and
              m.read(SD + 0x40) & (1 << 19),
              'ordinary initialization host advertises ADMA2 without SDMA')
        for wide in (False, True):
            m.write(SD + 0x28, 0x1a if wide else 0x12, 'b')
            m.bytes(SG, struct.pack('<IQ', (512 << 16) | 0x23, A) if wide else
                    struct.pack('<II', (512 << 16) | 0x23, A))
            m.bytes(A, bytes([0xa5]) * 512)
            m.write(SD + 0x58, SG)
            m.write(SD + 0x5c, 0)
            m.write(SD + 4, (1 << 16) | 512)
            m.write(SD + 0xc, 0x13, 'w')
            check(m.read(SD + 0xc, 'w') & 1,
                  f'ADMA{64 if wide else 32} retains DMA enable without SDMA')
            command(m, 8, flags=0x3a)
            ext = read_bytes(m, A, 512)
            check(m.read(SD + 0x30) & 2 and ext[192] == 8 and
                  ext[307] == 31 and ext[308] & 1 and ext[15] == 0,
                  f'ordinary ADMA{64 if wide else 32} completes real EXT_CSD before CQE')
        check(digest(card) == original_sha,
              'ordinary ADMA initialization preserves backing bytes')


def mmio_dma_refusal(binary, root, check):
    with ExitStack() as stack:
        card = card_file(root, 'mmio-dma')
        original_sha = digest(card)
        m = Controller(root, 'mmio-dma', stack, binary, card=card, emmc=True)
        initialize(m, check)
        configure(m, wide=True)
        for tag, read in ((3, True), (5, False)):
            cfg = m.read(CQ + 8)
            # The read destination would disable CFG reentrantly. The write
            # source fits within real MMIO, then continues from ordinary RAM.
            segments = [(CQ + 8, 512)] if read else [(CQ, 16), (A, 496)]
            task(m, tag, segments, read=read, lba=40, wide=True)
            for _ in range(20):
                m.step(1000)
            cmd = 46 if read else 47
            check(m.read(CQ + 8) == cfg and m.read(CQ + 0x10) & 4 and
                  m.read(CQ + 0x54) == (1 << 31) | (tag << 24) | (cmd << 16),
                  f'MMIO DMA {"destination" if read else "source"} is refused without register mutation')
            check(m.read(CQ + 0x2c) == 0 and m.read(CQ + 0x28) == 0 and
                  m.read(CQ + 0x3c) & (1 << tag) and digest(card) == original_sha,
                  f'MMIO DMA CMD{cmd} has no false completion or backing write')
            command(m, 48, 1, 0x1b)
            configure(m, wide=True)
        task(m, 6, [(A, 512)], wide=True)
        finish(m, 6)
        check(read_bytes(m, A, 512) == pattern(8) and digest(card) == original_sha,
              'normal RAM data succeeds after explicit malformed-DMA recovery')


def exercise(binary, root, check):
    adma_initialization(binary, root, check)
    mmio_dma_refusal(binary, root, check)
    with ExitStack() as stack:
        card = card_file(root, 'source')
        baseline = card.read_bytes()
        original_sha = hashlib.sha256(baseline).hexdigest()
        m = Controller(root, 'source', stack, binary, card=card, emmc=True)
        initialize(m, check)
        # Independent card-protocol controls run before enabling the CQ host.
        submit_card(m, 2)
        check(ready(m) == 1 << 2, 'readiness identifies an accepted card task only')
        command(m, 44, (1 << 30) | (3 << 16) | 1)
        command(m, 13, RCA << 16)
        command(m, 45, 8, refusal=True)
        command(m, 13, RCA << 16)
        check(ready(m) == 1 << 2, 'intervening command invalidates CMD44/CMD45 pairing')
        command(m, 44, (1 << 30) | (2 << 16) | 1, refusal=True)
        command(m, 13, RCA << 16)
        check(ready(m) == 1 << 2, 'duplicate tag cannot replace an accepted task')
        command(m, 48, (2 << 16) | 2, 0x1b)
        check(ready(m) == 0, 'CMD48 discards the selected card task')
        submit_card(m, 4, blocks=3)
        m.write(SD + 4, (3 << 16) | 512)
        m.write(SD + 0xc, 0x32, 'w')
        command(m, 46, 4 << 16, 0x3a)
        prefix = b''.join(m.read(SD + 0x20).to_bytes(4, 'little') for _ in range(16))
        check(ready(m) == 0, 'active task is not falsely advertised as ready again')
        clock = m.step(1000)
        card2 = root / 'pio-target.raw'
        shutil.copyfile(card, card2)
        receiver = Controller(root, 'pio-target', stack, binary, card=card2,
                              emmc=True, incoming=True)
        migrate(m, receiver, root)
        receiver.command(f'clock_set {clock}')
        qmp(receiver.monitor, 'cont')
        remaining = b''.join(receiver.read(SD + 0x20).to_bytes(4, 'little')
                             for _ in range((3 * 512 - len(prefix)) // 4))
        check(prefix + remaining == b''.join(pattern(i) for i in range(8, 11)),
              'real card PIO data survives CMD13 and partial FIFO migration')
        check(ready(receiver) == 0 and digest(card2) == original_sha,
              'card task retires after actual data and read backing stays unchanged')
        # CQ host: split a block across SG entries, then migrate its partial buffer.
        m = receiver
        configure(m)
        segments = [(A, 200), (B, 700), (B + 0x1000, 124)]
        for address, length in segments:
            m.bytes(address, bytes([0xa5]) * length)
        task(m, 3, segments)
        for _ in range(20):
            clock = m.step(1000)
            if read_bytes(m, A, 200) == pattern(8)[:200]:
                break
        else:
            raise AssertionError(('No actual queued DMA data arrived',
                                  {hex(o): hex(m.read(CQ + o)) for o in
                                   (8, 12, 16, 32, 36, 40, 44, 48, 52, 60, 84, 88, 92)},
                                  read_bytes(m, A, 32).hex()))
        check(read_bytes(m, B, 700) == bytes([0xa5]) * 700 and
              m.read(CQ + 0x28) == 1 << 3 and m.read(CQ + 0x2c) == 0,
              'CQ task is pending between real partial-block SG transfers')
        card3 = root / 'dma-target.raw'
        shutil.copyfile(card2, card3)
        receiver = Controller(root, 'dma-target', stack, binary, card=card3,
                              emmc=True, incoming=True)
        migrate(m, receiver, root)
        receiver.command(f'clock_set {clock}')
        qmp(receiver.monitor, 'cont')
        finish(receiver, 3)
        check(b''.join(read_bytes(receiver, a, n) for a, n in segments) ==
              pattern(8) + pattern(9), 'queued SG read completes exactly across live migration')
        check(digest(card3) == original_sha, 'queued read changes no backing bytes')
        m = receiver
        m.bytes(A, bytes([0x77]) * 200)
        m.bytes(B, bytes([0x77]) * 824)
        task(m, 7, [(A, 200), (B, 824)], read=False, lba=40)
        for _ in range(20):
            m.step(1000)
            if m.read(CQ + 0x58) == 47 and m.read(CQ + 0x34) & (1 << 7):
                break
        else:
            raise AssertionError('Card did not accept queued write execution')
        m.step(1000)  # First 200 bytes reach the controller's partial block.
        m.write(CQ + 8, 0)
        m.step(10000)
        check(m.read(CQ + 0x28) == 0 and m.read(CQ + 0x2c) == 0 and
              digest(card3) == original_sha, 'disable cancels real partial write DMA without committing its block')
        response = command(m, 13, RCA << 16)
        check(response & 0x1e00 == 6 << 9,
              'host cancellation does not silently discard the active card write')
        command(m, 48, (7 << 16) | 2, 0x1b)
        check(command(m, 13, RCA << 16) & 0x1e00 == 4 << 9,
              'explicit card task management recovers the interrupted transfer')
        configure(m)
        task(m, 8, [(A, 200), (B, 824)], read=False, lba=40)
        for _ in range(20):
            m.step(1000)
            if m.read(CQ + 0x58) == 47 and m.read(CQ + 0x34) & (1 << 8):
                break
        else:
            raise AssertionError('Reset control never reached actual card write')
        m.step(1000)
        m.write(SD + 0x2f, 1, 'b')
        m.step(10000)
        check(m.read(CQ + 8) == 0 and m.read(CQ + 0x28) == 0 and
              m.read(CQ + 0x2c) == 0 and digest(card3) == original_sha,
              'SDHCI reset cancels actual partial CQ write and its delayed DMA')
        m.write(SD + 0x29, 15, 'b')
        m.write(SD + 0x2c, 7, 'w')
        m.write(SD + 0x34, 0x03ff7fff)
        check(command(m, 13, RCA << 16) & 0x1e00 == 6 << 9,
              'SDHCI reset does not invent a card task-management command')
        command(m, 48, (8 << 16) | 2, 0x1b)
        configure(m)
        m.write(CQ + 0x1c, 0x80018000 | (2 << 8))
        task(m, 1, [(A, 512)], interrupt=False)
        finish(m, 1, interrupt=False)
        check(bool(m.read(CQ + 0x1c) & (1 << 20)),
              'coalescing counter records an actual noninterrupting data completion')
        task(m, 1, [(A, 512)], interrupt=False)
        finish(m, 1)
        check(True, 'second real completion triggers the programmed coalescing threshold')
        task(m, 1, [(A, 512)], interrupt=False)
        finish(m, 1, interrupt=False)
        check(True, 'threshold interrupt remains disarmed until explicit counter reset')
        m.write(CQ + 0x1c, 0x80018081)
        task(m, 1, [(A, 512)], interrupt=False)
        clock = finish(m, 1, interrupt=False)
        card4 = root / 'timer-target.raw'
        shutil.copyfile(card3, card4)
        receiver = Controller(root, 'timer-target', stack, binary, card=card4,
                              emmc=True, incoming=True)
        migrate(m, receiver, root)
        receiver.command(f'clock_set {clock}')
        qmp(receiver.monitor, 'cont')
        m, card3 = receiver, card4
        m.step(1_023_999)
        check(not m.read(CQ + 0x10) & 2,
              'migrated coalescing timer does not fire before its virtual deadline')
        m.step(1)
        check(bool(m.read(CQ + 0x10) & 2),
              'migrated coalescing timer fires at the declared virtual deadline')
        m.write(CQ + 0x1c, 0x80010000)
        m.write(CQ + 0x10, 0x3f)
        check(not m.read(CQ + 0x1c) & (1 << 20),
              'explicit coalescing reset clears its real completion counter')
        configure(m)
        payload = bytes((i * 43 + 11) & 255 for i in range(1024))
        m.bytes(A, payload[:256])
        m.bytes(B, payload[256:])
        task(m, 5, [(A, 256), (B, 768)], read=False, lba=32)
        finish(m, 5)
        expected = bytearray(baseline)
        expected[32 * 512:34 * 512] = payload
        check(card3.read_bytes() == expected, 'queued SG write changes only its exact backing sectors')
        check(all(digest(p) == original_sha for p in (card, card2, root / 'dma-target.raw')),
              'source and intermediate migration backing files remain unchanged')
        qmp(m.monitor, 'quit')
        check(m.process.wait(timeout=15) == 0 and card3.read_bytes() == expected,
              'natural QEMU exit retains exact queued write readback')
    # A separate sparse 8GiB virtual host exercises addresses above 4GiB.
    global TABLE, SG
    TABLE, SG = 0x100100000, 0x100200000
    with ExitStack() as stack:
        card = card_file(root, 'wide')
        baseline = card.read_bytes()
        m = Controller(root, 'wide', stack, binary, card=card, emmc=True, memory_gib=8)
        initialize(m, check)
        check(m.read(SD + 0x40) & 0x10080000 == 0x10080000,
              'virtual eMMC host advertises ADMA2 with 64-bit addressing')
        configure(m, wide=True)
        high, low = 0x100500000, 0x500000
        m.bytes(low, bytes([0xa5]) * 1024)
        large_segments = [(high, 65536), (high + 0x20000, 4096)]
        for address, length in large_segments:
            m.bytes(address, bytes([0xa5]) * length)
        task(m, 6, large_segments, wide=True)
        finish(m, 6, steps=160)
        check(b''.join(read_bytes(m, a, n) for a, n in large_segments) == baseline[8 * 512:144 * 512] and
              read_bytes(m, low, 1024) == bytes([0xa5]) * 1024,
              '64-bit DMA preserves high addresses and splits a zero-encoded 64KiB segment')
        payload = bytes((i * 13 + 91) & 255 for i in range(1024))
        m.bytes(high, payload)
        task(m, 6, [(high, 1024)], wide=True, read=False, lba=40)
        finish(m, 6)
        expected = bytearray(baseline)
        expected[40 * 512:42 * 512] = payload
        check(card.read_bytes() == expected, '64-bit queued write reaches exact backing sectors')
        m.write(CQ + 8, 0)
        m.write(SD + 0x28, 0x12, 'b')
        m.write(CQ + 8, 0x1101)
        m.write(CQ + 0x28, 1 << 6)
        m.step(1000)
        check(m.read(CQ + 0x10) & 4 and m.read(CQ + 0x3c) == 1 << 6 and
              m.read(CQ + 0x2c) == 0 and card.read_bytes() == expected,
              '32-bit mode refuses a high descriptor address without storage mutation')
        refused = False
        try:
            qmp(m.monitor, 'qom-set', {'path': '/machine', 'property': 'sd-emmc', 'value': False})
        except ValueError as error:
            assert 'before machine initialization' in str(error)
            refused = True
        check(refused, 'virtual card selection cannot change after machine initialization')
        qmp(m.monitor, 'quit')
        check(m.process.wait(timeout=15) == 0 and card.read_bytes() == expected,
              '64-bit backing readback survives natural QEMU exit')
    # Negative protocol tests have their own card, independent of DMA state.
    with ExitStack() as stack:
        card = card_file(root, 'negative')
        original_sha = digest(card)
        m = Controller(root, 'negative', stack, binary, card=card, emmc=True)
        initialize(m, check)
        for tag in range(32):
            submit_card(m, tag)
        check(ready(m) == 0xffffffff, 'all32 card task slots have real accepted state')
        response = command(m, 6, (3 << 24) | (15 << 16), 0x1b)
        check(response & (1 << 22) and ready(m) == 0xffffffff,
              'nonempty queue mode disable reports illegal command and preserves tasks')
        command(m, 48, 1, 0x1b)
        check(ready(m) == 0, 'CMD48 discard-all clears the entire card queue')
        response = command(m, 6, (3 << 24) | (250 << 16), 0x1b)
        check(response & 0x80 and not command(m, 13, RCA << 16) & 0x80,
              'reported SWITCH_ERROR does not poison subsequent valid commands')
        check(digest(card) == original_sha, 'negative queue/control requests leave backing bytes unchanged')
        for index, emmc_source in enumerate((True, False)):
            source_card, target_card = card_file(root, f'ms{index}'), card_file(root, f'mt{index}')
            source = Controller(root, f'ms{index}', stack, binary, card=source_card,
                                emmc=emmc_source)
            target = Controller(root, f'mt{index}', stack, binary, card=target_card,
                                emmc=not emmc_source, incoming=True)
            endpoint = 'unix:' + str(source.monitor.parent / 'migration')
            qmp(target.monitor, 'migrate-incoming', {'uri': endpoint})
            qmp(source.monitor, 'migrate', {'uri': endpoint})
            check(target.process.wait(timeout=30) != 0,
                  f'card protocol migration mismatch direction{index} is refused')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, mode=0o700)
    binary = args.qemu.resolve()
    report = {'ok': False, 'scope': __doc__, 'binary': str(binary),
              'binary_sha256': digest(binary), 'harness_sha256': digest(Path(__file__)), 'passed': []}
    def check(condition, label):
        assert condition, label
        report['passed'].append(label)
    try:
        exercise(binary, args.output, check)
        report['ok'] = True
    except BaseException:
        report['error'] = traceback.format_exc()
        raise
    finally:
        report['binary_unchanged'] = digest(binary) == report['binary_sha256']
        report['synthetic_backing_files'] = {
            p.name: {'bytes': p.stat().st_size, 'sha256': digest(p)}
            for p in sorted(args.output.glob('*.raw')) if p.is_file()}
        if not report['binary_unchanged']:
            report['ok'] = False
        (args.output / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
    if not report['ok']:
        raise SystemExit(1)
    print('PASS actual queued eMMC/CQHCI data controls:', len(report['passed']))


if __name__ == '__main__':
    main()
