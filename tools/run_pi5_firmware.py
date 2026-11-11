#!/usr/bin/env python3
"""Exercise a Pi5 SD image through its actual U-Boot and FreeBSD EFI loader.

The first-stage transport supplies an AArch64 firmware-equivalent entry with
x0 pointing to the enrolled FDT. It preserves every U-Boot byte. This does not
emulate VideoCore ROM/EEPROM execution. Unlike direct FreeBSD kernel boot, all
subsequent SD reads, boot scripts, EFI calls and kernel loading run in the guest.
Only regular files are accepted; writes go to a new COW overlay.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import stat
import struct
import subprocess
import time
import uuid
import zlib

from compose_pcie2_contract_dtb import decode
from uconsole_emulator import qmp


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def regular(path):
    path = path.resolve(strict=True)
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError('regular file required: ' + str(path))
    return path


def partition_start(image, label=None):
    with image.open('rb') as stream:
        stream.seek(512)
        header = bytearray(stream.read(512))
        if header[:8] != b'EFI PART':
            raise ValueError('GPT SD image required')
        length, expected = struct.unpack_from('<II', header, 12)
        if not 92 <= length <= 512:
            raise ValueError('invalid GPT header size')
        struct.pack_into('<I', header, 16, 0)
        if zlib.crc32(header[:length]) != expected:
            raise ValueError('invalid GPT header checksum')
        table, count, size, expected = struct.unpack_from('<QIII', header, 72)
        if not 1 <= count <= 4096 or not 128 <= size <= 4096:
            raise ValueError('unreasonable GPT entry table')
        if table * 512 + count * size > image.stat().st_size:
            raise ValueError('GPT table outside image')
        stream.seek(table * 512)
        entries = stream.read(count * size)
        if zlib.crc32(entries) != expected:
            raise ValueError('invalid GPT table checksum')
        for index in range(count):
            entry = entries[index * size:(index + 1) * size]
            # EFI System Partition, as stored in GPT's mixed-endian UUID.
            if label is None:
                if entry[:16] != uuid.UUID('c12a7328-f81f-11d2-ba4b-00a0c93ec93b').bytes_le:
                    continue
            elif entry[56:128].decode('utf-16le').rstrip('\0') != label:
                continue
            first, last = struct.unpack_from('<QQ', entry, 32)
            if first < 34 or last < first or (last + 1) * 512 > image.stat().st_size:
                raise ValueError('ESP outside image')
            return first
    raise ValueError('image has no requested partition: ' + (label or 'ESP'))


def fat_read(image, first, name):
    return subprocess.check_output(['mtype', '-i', f'{image}@@{first * 512}', '::/' + name])


def firmware_transport(uboot):
    if not 4096 <= len(uboot) <= 16 * 1024 * 1024:
        raise ValueError('unreasonable U-Boot binary size')
    # AArch64 B instruction at the raw image entry, not an ELF/PE executable.
    if struct.unpack_from('<I', uboot)[0] & 0xfc000000 != 0x14000000:
        raise ValueError('expected an AArch64 U-Boot raw entry branch')
    header = bytearray(4096)
    # Place the unchanged, position-independent firmware at physical 0x80000.
    struct.pack_into('<IIQQQQQQII', header, 0, 0x14000400, 0,
                     0x7f000, len(header) + len(uboot), 0, 0, 0, 0, 0x644d5241, 0)
    return header + uboot


def clean_serial(data):
    return re.sub(rb'\x1b\[[0-?]*[ -/]*[@-~]', b'', data).decode(errors='replace')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', type=Path, required=True)
    parser.add_argument('--image', type=Path, required=True)
    parser.add_argument('--dtb', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--memory-gib', type=int, choices=[1, 2, 4, 8, 16], default=8)
    parser.add_argument('--timeout', type=int, default=240)
    parser.add_argument('--card-size-gib', type=int, choices=[1, 2, 4, 8, 16, 32, 64],
                        default=32, help='virtual SD capacity; 32 GiB exercises SDHC addressing')
    args = parser.parse_args()
    if not 10 <= args.timeout <= 1800:
        parser.error('timeout must be between 10 and 1800 seconds')
    binary, image, dtb = map(regular, [args.qemu, args.image, args.dtb])
    qemu_img = regular(binary.with_name('qemu-img'))
    first = partition_start(image)
    uboot = fat_read(image, first, 'u-boot.bin')
    nodes, _, _ = decode(dtb.read_bytes())
    compatible = nodes['/'].get('compatible', b'').split(b'\0')
    if b'brcm,bcm2712' not in compatible:
        raise ValueError('BCM2712 FDT required')
    if b'raspberrypi,500' in compatible:
        board_type = 0x19
        dtb_name = 'bcm2712-rpi-500.dtb'
    elif b'raspberrypi,5-model-b' in compatible:
        board_type = 0x17
        dtb_name = ('bcm2712-d-rpi-5-b.dtb' if b'brcm,bcm2712-d0' in compatible
                    else 'bcm2712-rpi-5-b.dtb')
    else:
        raise ValueError('enrolled Pi5 Model B or Pi500 board FDT required')
    if board_type == 0x19 and args.memory_gib not in (8, 16):
        raise ValueError('Pi500/500+ supports 8 or 16 GiB RAM')
    if fat_read(image, first, dtb_name) != dtb.read_bytes():
        raise ValueError('supplied FDT does not match the image boot partition')
    pins = {str(path): digest(path) for path in [binary, qemu_img, image, dtb]}
    capacity = args.card_size_gib * 1024**3
    if capacity < image.stat().st_size:
        raise ValueError('selected card capacity is smaller than the image')
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    transport = out / 'U-Boot-Image'
    transport.write_bytes(firmware_transport(uboot))
    assert transport.read_bytes()[4096:] == uboot
    env = {key: value for key, value in os.environ.items()
           if key not in ('LD_LIBRARY_PATH', 'DYLD_LIBRARY_PATH')}
    overlay = out / 'sd.qcow2'
    subprocess.run([str(qemu_img), 'create', '-f', 'qcow2', '-F', 'raw', '-b',
                    str(image), str(overlay)], check=True, env=env, capture_output=True)
    subprocess.run([str(qemu_img), 'resize', str(overlay), str(capacity)],
                   check=True, env=env, capture_output=True)
    memory_code = {1: 2, 2: 3, 4: 4, 8: 5, 16: 6}[args.memory_gib]
    revision = 0x804000 | (board_type << 4) | (memory_code << 20)
    if board_type == 0x17 and args.memory_gib in (1, 16):revision |= 1
    monitor = Path('/tmp') / ('p5fw-' + uuid.uuid4().hex[:16] + '.qmp')
    command = [str(binary), '-M', f'raspi5b,board-revision={revision:#x}',
               '-m', f'{args.memory_gib}G', '-kernel', str(transport), '-dtb', str(dtb),
               '-drive', f'if=sd,format=qcow2,file={overlay}', '-display', 'none',
               '-serial', 'stdio', '-monitor', 'none', '-nic', 'none', '-no-reboot',
               '-qmp', f'unix:{monitor},server=on,wait=off']
    milestones = {'uboot': 'U-Boot 20', 'efi_loader': 'FreeBSD/arm64 EFI loader',
                  'queued_experiment': 'Hardware trace: booting experiment',
                  'kernel': 'FreeBSD 16.', 'sd_root': 'Trying to mount root from ufs:/dev/gpt/HWTROOT',
                  'init': 'Daemonarchy hardware trace: init reached',
                  'dumps_saved': 'Saved to DATRACE DUMPS/'}
    observed = {}
    screenshot_errors = {}
    screenshots = {}
    screenshot_pool = ThreadPoolExecutor(max_workers=1)
    data = b''
    failure = None
    started = time.monotonic()
    with (out / 'serial.log').open('wb') as log, (out / 'qemu.log').open('wb') as err:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=err, env=env)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        panic_at = None
        try:
            while selector.get_map():
                for key, _ in selector.select(.25):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    data += chunk
                    log.write(chunk)
                    log.flush()
                text = clean_serial(data)
                for name, marker in milestones.items():
                    if name not in observed and marker in text:
                        observed[name] = round(time.monotonic() - started, 3)
                        print(name, observed[name], flush=True)
                        if name in ('efi_loader', 'init'):
                            # Keep draining serial while QMP captures the video
                            # console: a full stdout pipe must not block QMP.
                            screenshots[name] = screenshot_pool.submit(
                                qmp, monitor, 'screendump',
                                {'filename': str(out / (name + '.png')), 'format': 'png'})
                if 'panic:' in text or 'Synchronous Abort' in text or 'Please RESET' in text:
                    panic_at = panic_at or time.monotonic()
                    if time.monotonic() - panic_at > 2:
                        raise RuntimeError('guest panic/abort; inspect serial-clean.txt')
                if time.monotonic() - started > args.timeout:
                    raise TimeoutError('firmware boot deadline')
            process.wait(timeout=10)
        except Exception as error:
            failure = repr(error)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
            selector.close()
            monitor.unlink(missing_ok=True)
    screenshot_pool.shutdown(wait=True)
    for name, future in screenshots.items():
        try:
            future.result()
        except Exception as error:
            screenshot_errors[name] = repr(error)
    (out / 'serial-clean.txt').write_text(clean_serial(data))
    inputs_unchanged = all(digest(Path(name)) == value for name, value in pins.items())
    readback = out / 'readback.img'
    subprocess.run([str(qemu_img), 'convert', '-O', 'raw', str(overlay), str(readback)],
                   check=True, env=env)
    # As with physical readback, retain the image-sized prefix containing the
    # GPT/ESP/root/DATRACE, rather than the card's unallocated trailing space.
    with readback.open('r+b') as stream:
        stream.truncate(image.stat().st_size)
    try:
        checkpoint = fat_read(readback, first, 'UBOOT.LOG').decode(errors='replace').strip()
    except subprocess.CalledProcessError:
        checkpoint = None
    dump_files = {}
    probe_status = {}
    state = ''
    try:
        datrace = partition_start(readback, 'DATRACE')
        state = fat_read(readback, datrace, 'STATE.TXT').rstrip(b'\0 \r\n').decode()
        pending = re.search(r'^pending=([A-Za-z0-9_.-]{1,64})$', state, re.M)
        if pending:
            for name in ('done.txt', 'capture.log', 'dmesg.txt', 'sysctl.txt', 'kenv.txt'):
                content = fat_read(readback, datrace, f'DUMPS/{pending[1]}/{name}')
                (out / ('dump-' + name)).write_bytes(content)
                dump_files[name] = hashlib.sha256(content).hexdigest()
            for name, status in re.findall(r'^finish (\S+) status=(\d+)$',
                                          (out / 'dump-capture.log').read_text(), re.M):
                probe_status[name] = int(status)
    except (ValueError, subprocess.CalledProcessError) as error:
        failure = failure or repr(error)
    checks = {name: name in observed for name in milestones}
    checks['durable_dump_complete'] = 'done.txt' in dump_files
    checks['all_probe_completion_records'] = set(probe_status) == {
        'dmesg.txt', 'kenv.txt', 'sysctl.txt', 'kldstat.txt',
        'pciconf.txt', 'devinfo.txt', 'usbconfig.txt'}
    checks['efi_framebuffer_captured'] = (out / 'efi_loader.png').exists()
    checks['init_framebuffer_captured'] = (out / 'init.png').exists()
    checks.update(inputs_unchanged=inputs_unchanged, natural_exit=process.returncode == 0,
                  no_guest_failure=failure is None,
                  checkpoint_before_efi=checkpoint == 'PI500-HANDOFF-04-ABOUT-TO-ENTER-EFI')
    result = {'ok': all(checks.values()), 'checks': checks, 'milestones': observed,
              'failure': failure, 'returncode': process.returncode, 'command': command,
              'input_sha256': pins, 'uboot_sha256': hashlib.sha256(uboot).hexdigest(),
              'transport_sha256': digest(transport), 'readback_sha256': digest(readback),
              'checkpoint': checkpoint, 'board_revision': hex(revision),
              'virtual_card_bytes': capacity, 'readback_prefix_bytes': image.stat().st_size,
              'datrace_state': state, 'durable_dump_sha256': dump_files,
              'probe_exit_status': probe_status, 'screenshot_errors': screenshot_errors,
              'scope': 'firmware-equivalent x0/EL2 entry, actual U-Boot/EFI/SD/kernel; no EEPROM/RP1 qualification'}
    (out / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'ok': result['ok'], 'checks': checks, 'failure': failure}), flush=True)
    raise SystemExit(0 if result['ok'] else 1)


if __name__ == '__main__':
    main()
