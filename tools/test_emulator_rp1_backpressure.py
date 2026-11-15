#!/usr/bin/env python3
"""RP1 UART host backpressure, actual byte custody and migration.

Host socket queues are outside VMState. The source process is stopped after a
completed migration before its old socket is drained; this does not qualify
uncoordinated migration of an external serial service or physical timing.
"""
import argparse
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import socket
import tempfile
import time

from test_emulator_rp1 import Machine, configure, BAR1
from test_emulator_rp1_tx import setup, step, BUSY, TXFE, TXFF, RXFE
from test_emulator_pmic import connect
from test_emulator_adc_migration import migrate
from uconsole_emulator import qmp


def exact(peer, count):
    result = bytearray()
    while len(result) < count:
        block = peer.recv(count - len(result))
        assert block, ('Serial stream closed', len(result), count)
        result.extend(block)
    return bytes(result)


def quiet(peer):
    peer.settimeout(0.2)
    try:
        try:
            data = peer.recv(1)
        except socket.timeout:
            return
        raise AssertionError(('Unexpected serial output', data))
    finally:
        peer.settimeout(5)


def machine(root, name, stack, binary, n=0, incoming=False):
    sockets = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='rp1-bp-')))
    m = Machine(root, name, stack, binary, incoming=incoming,
                endpoint_options=f',uart{n}=serial0',
                device_args=['-chardev',
                             f'socket,id=serial0,path={sockets/"uart"},server=on,wait=off'])
    peer = stack.enter_context(connect(sockets/'uart', m.process))
    # Retain native socket watermarks. On Darwin, shrinking SO_RCVBUF below
    # the sender's SO_SNDLOWAT suppresses writable events even after drain.
    peer.settimeout(5)
    base = BAR1 + 0x30000 + n * 0x4000
    if not incoming:
        configure(m)
        qmp(m.monitor, 'cont')
        base = setup(m, n=n)
        m.write(base + 0x30, 0x301)  # Real host TX, no CTS gate or loopback.
    return m, peer, base, sockets/'uart'


def fill(m, base):
    pattern = bytearray()
    for i in range(16384):
        byte = (i * 17 + 3) & 255
        pattern.append(byte)
        m.write(base, byte)
        step(m, 10000)
        if m.read(base + 0x18) & BUSY:
            # Every frame deadline has passed. Its pending host output must
            # retain BUSY while qtest, QMP and unrelated timers remain usable.
            step(m, 1000000000)
            assert m.read(base + 0x18) & BUSY
            assert qmp(m.monitor, 'query-status')['running']
            return bytes(pattern)
    raise AssertionError('Host transport did not reach backpressure within16384 frames')


def migration(source, target, stack):
    directory = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='rp1-bpm-')))
    migrate(source, target, directory)


def exercise(binary, legacy, root, passed, observations):
    for n in range(6):
        with ExitStack() as stack:
            m, peer, base, _ = machine(root, f'uart{n}', stack, binary, n=n)
            prefix = fill(m, base)
            observations[f'uart{n}_accepted_before_stall'] = len(prefix) - 1
            queued = bytes(range(0xa0, 0xc0))
            for byte in queued:
                m.write(base, byte)
            assert m.read(base + 0x18) & TXFF
            m.write(base, 0xff)  # Refused full-FIFO write must not replace data.
            step(m, 1000000)
            assert m.read(base + 0x18) & TXFF
            assert not m.read(base + 0x3c) & 2, 'Host blockage invented a CTS edge'
            other = setup(m, n=(n + 1) % 6)
            m.write(other, 0x5a)
            m.write(other + 0x30, 0x381)
            step(m, 10000)
            assert m.read(other) & 255 == 0x5a, 'Other UART timer stopped'
            passed.append(f'uart{n}: blocked host retains shift/full FIFO; qtest/QMP/other UART progress')
            assert exact(peer, len(prefix)) == prefix
            # The next frame retains its full10us after host acceptance.
            quiet(peer)
            step(m, 9999)
            quiet(peer)
            step(m, 1)
            assert exact(peer, 1) == queued[:1]
            step(m, 310000)
            assert exact(peer, 31) == queued[1:]
            assert m.read(base + 0x18) & TXFE and not m.read(base + 0x18) & (BUSY | TXFF)
            quiet(peer)
            passed.append(f'uart{n}: drain preserves every accepted byte, full-FIFO refusal and frame deadlines')
    with ExitStack() as stack:
        m, peer, base, _ = machine(root, 'reset', stack, binary)
        prefix = fill(m, base)
        m.write(base, 0xa1)
        qmp(m.monitor, 'system_reset')
        configure(m)
        assert exact(peer, len(prefix) - 1) == prefix[:-1]
        step(m, 1000000000)
        assert m.read(base + 0x18) & TXFE and not m.read(base + 0x18) & BUSY
        assert m.read(base + 0x18) & RXFE
        quiet(peer)
        base = setup(m)
        m.write(base + 0x30, 0x301)
        m.write(base, 0xd1)
        step(m, 10000)
        assert exact(peer, 1) == b'\xd1'
        quiet(peer)
        passed.append('reset removes host watch and cancels only unaccepted shift/queued data; UART restarts')
    with ExitStack() as stack:
        m, peer, base, path = machine(root, 'disconnect', stack, binary)
        fill(m, base)
        m.write(base, 0xa2)
        peer.close()
        deadline = time.monotonic() + 5
        while m.read(base + 0x18) & BUSY:
            assert time.monotonic() < deadline, 'Disconnected UART did not retire its pending work'
            step(m, 10000)
        replacement = stack.enter_context(connect(path, m.process))
        replacement.settimeout(5)
        # Synchronize a QMP turn after accept, then transmit a fresh frame.
        qmp(m.monitor, 'query-status')
        m.write(base, 0xd2)
        step(m, 10000)
        assert exact(replacement, 1) == b'\xd2'
        quiet(replacement)
        passed.append('disconnect releases pending transport; reconnect receives only new accepted frames')
    with ExitStack() as stack:
        source, oldpeer, base, _ = machine(root, 'migration-source', stack, binary)
        prefix = fill(source, base)
        queued = bytes((0xe1, 0xe2, 0xe3))
        for byte in queued:
            source.write(base, byte)
        clock = step(source, 1234)
        target, peer, _, _ = machine(root, 'migration-target', stack, binary, incoming=True)
        target.command(f'clock_set {clock}')
        migration(source, target, stack)
        source.close()  # Quiesce old external transport before draining its socket.
        assert exact(oldpeer, len(prefix) - 1) == prefix[:-1]
        qmp(target.monitor, 'cont')
        assert exact(peer, 1) == prefix[-1:]
        step(target, 9999)
        quiet(peer)
        step(target, 1)
        assert exact(peer, 1) == queued[:1]
        step(target, 20000)
        assert exact(peer, 2) == queued[1:]
        assert not target.read(base + 0x18) & BUSY
        quiet(peer)
        passed.append('real VMState retains host-blocked shift and queued bytes; destination recreates local watch')
    if legacy:
        for direction, (a, b) in enumerate(((legacy, binary), (binary, legacy))):
            with ExitStack() as stack:
                source = Machine(root, f'compatible-source{direction}', stack, a)
                configure(source)
                qmp(source.monitor, 'cont')
                base = setup(source)
                for byte in (0x51, 0x52, 0x53):
                    source.write(base, byte)
                source.write(base + 0x30, 0x381)
                clock = step(source, 1234)
                target = Machine(root, f'compatible-target{direction}', stack, b, incoming=True)
                target.command(f'clock_set {clock}')
                migration(source, target, stack)
                qmp(target.monitor, 'cont')
                step(target, 8765)
                assert target.read(base + 0x18) & RXFE
                step(target, 1)
                assert target.read(base) & 255 == 0x51
                step(target, 20000)
                assert [target.read(base) & 255 for _ in range(2)] == [0x52, 0x53]
                assert not target.read(base + 0x18) & BUSY
                passed.append(f'ordinary partial TX wire migration direction{direction} retains old format')
        with ExitStack() as stack:
            source, _, base, _ = machine(root, 'refusal-source', stack, binary)
            fill(source, base)
            target, _, _, _ = machine(root, 'refusal-target', stack, legacy, incoming=True)
            sockets = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='rp1-bpr-')))
            uri = 'unix:' + str(sockets/'migration')
            qmp(target.monitor, 'migrate-incoming', {'uri': uri})
            qmp(source.monitor, 'migrate', {'uri': uri})
            assert target.process.wait(timeout=30) != 0, 'Old target accepted an unrepresentable host-pending shift'
            log = (root/'refusal-target.log').read_text(errors='replace')
            assert 'tx-backend-pending' in log, log[-2000:]
            passed.append('old destination explicitly rejects host-pending subsection instead of dropping its byte')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', required=True)
    parser.add_argument('--legacy-qemu')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(mode=0o700)
    binary = Path(args.qemu).resolve()
    identities = {str(binary): hashlib.sha256(binary.read_bytes()).hexdigest()}
    if args.legacy_qemu:
        old = Path(args.legacy_qemu).resolve()
        identities[str(old)] = hashlib.sha256(old.read_bytes()).hexdigest()
    result = {'ok': False, 'scope': __doc__, 'inputs': identities,
              'harness_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'passed': [], 'observations': {}}
    try:
        exercise(str(binary), args.legacy_qemu, args.output.resolve(),
                 result['passed'], result['observations'])
        result['ok'] = True
    except BaseException as error:
        result['error'] = repr(error)
        raise
    finally:
        result['inputs_unchanged'] = {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() == digest
                                      for name, digest in identities.items()}
        (args.output/'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print('PASS:', len(result['passed']), 'native RP1 host-backpressure controls')


if __name__ == '__main__':
    main()
