#!/usr/bin/env python3
"""CM4 development environment using QEMU's partial BCM2711 model."""
import argparse
import base64
import bz2
import hashlib
import json
import os
from pathlib import Path
import shutil
import shlex
import socket
import struct
import subprocess
import sys
import time
import uuid

from emulator_image import BootPartition
from emulator_dtb import patch_strings

ROOT = Path(__file__).resolve().parent.parent
DEFAULT = ROOT / 'build/emulator/workspace'
IMAGE_SHA256 = 'ef95242cdb0125e8ed08157400a26d4665acddd083481ec2314acd6e073b74ab'


def digest(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def executable(name):
    suffix = '.exe' if os.name == 'nt' else ''
    local = ROOT / 'build/emulator/qemu-build' / (name + suffix)
    return str(local) if local.is_file() else (shutil.which(name) or name)


def read_config(workspace):
    return json.loads((workspace / 'machine.json').read_text())


def prepare(args):
    source = args.image.resolve()
    workspace = args.workspace.resolve()
    if workspace.exists():
        raise ValueError('Workspace already exists; choose a new directory')
    actual = digest(source)
    expected = args.sha256 or (IMAGE_SHA256 if source.suffix == '.bz2' else None)
    if not expected:
        raise ValueError('For a raw/custom image, supply --sha256 with its expected hash')
    if actual.lower() != expected.lower():
        raise ValueError(f'Image checksum mismatch: {actual}')
    workspace.mkdir(parents=True)
    base = workspace / 'base.img'
    try:
        opener = bz2.open if source.suffix == '.bz2' else open
        with opener(source, 'rb') as inp, base.open('xb') as out:
            while chunk := inp.read(1024 * 1024):
                if chunk == b'\0' * len(chunk):
                    out.seek(len(chunk), 1)
                else:
                    out.write(chunk)
            out.truncate()
        with BootPartition(base) as boot:
            boot.extract('kernel8.img', workspace / 'kernel8.img')
            boot.extract('bcm2711-rpi-cm4.dtb', workspace / 'cm4-original.dtb')
        # Firmware config.txt is not executed by QEMU's direct kernel loader.
        # Activate the modeled DWC2 controller; do not apply the unmodeled panel/PMIC.
        dtb = patch_strings((workspace / 'cm4-original.dtb').read_bytes(), {
            ('/soc/usb@7e980000', 'status'): 'okay',
            ('/soc/usb@7e980000', 'compatible'): 'brcm,bcm2835-usb',
            ('/soc/usb@7e980000', 'dr_mode'): 'host',
            ('/soc/serial@7e201000/bluetooth', 'status'): 'disabled',
            ('/soc/serial@7e201000', 'skip-init'): None,
            ('/soc/serial@7e201000', 'uart-has-rtscts'): None,
        })
        (workspace / 'cm4-qemu.dtb').write_bytes(dtb)
        with base.open('rb') as stream:
            stream.seek(440)
            disk_id, = struct.unpack('<I', stream.read(4))
        # QEMU SD cards require a power-of-two size. Only the overlay grows.
        size = 1 << (base.stat().st_size - 1).bit_length()
        subprocess.run([args.qemu_img, 'create', '-f', 'qcow2', '-F', 'raw',
                        '-b', str(base), str(workspace / 'disk.qcow2'), str(size)], check=True)
        config = {'schema': 1, 'machine': 'raspi4b', 'coverage': 'partial-cm4',
                  'source': str(source), 'source_sha256': actual,
                  'base_sha256': digest(base), 'root': f'PARTUUID={disk_id:08x}-02',
                  'kernel_sha256': digest(workspace / 'kernel8.img'),
                  'dtb_sha256': digest(workspace / 'cm4-qemu.dtb')}
        (workspace / 'machine.json').write_text(json.dumps(config, indent=2) + '\n')
    except Exception:
        # Retain diagnostic files; never treat an incomplete preparation as usable.
        print(f'Preparation failed; partial files retained in {workspace}', file=sys.stderr)
        raise
    print(f'Prepared {workspace}\nCoverage: partial CM4; see docs/emulator.md')


def command(args):
    workspace = args.workspace.resolve()
    config = read_config(workspace)
    ports = [args.qmp_port, args.serial_port, args.gdb_port, args.ssh_port]
    if args.vnc_display is not None:
        ports.append(5900 + args.vnc_display)
    ports = [port for port in ports if port is not None]
    if any(not 1 <= port <= 65535 for port in ports) or len(ports) != len(set(ports)):
        raise ValueError('Listener ports must be distinct and between 1 and 65535')
    cmdline = (f'earlycon=pl011,mmio32,0xfe201000 console=ttyAMA1,115200 '
               f'root={config["root"]} rootfstype=ext4 rootwait rw')
    if args.mode == 'maintenance':
        cmdline += ' init=/bin/bash'
    else:
        cmdline += ' systemd.unit=multi-user.target'
    # raspi4b creates its SD card from the legacy IF_SD drive. Escape commas in
    # QEMU key/value syntax; subprocess receives paths without shell parsing.
    drive = str(workspace / 'disk.qcow2').replace(',', ',,')
    cmd = [args.qemu, '-machine', 'raspi4b', '-accel', 'tcg',
           '-kernel', str(workspace / 'kernel8.img'),
           '-dtb', str(workspace / 'cm4-qemu.dtb'),
           '-drive', f'if=sd,format=qcow2,file={drive}',
           '-append', cmdline, '-display', 'none', '-monitor', 'none',
           '-qmp', f'tcp:127.0.0.1:{args.qmp_port},server=on,wait=off',
           '-device', 'usb-kbd', '-device', 'usb-mouse']
    if args.serial_port:
        # Path-valued chardev options use QEMU's comma escaping.
        logfile = str(workspace / 'serial.log').replace(',', ',,')
        cmd += ['-chardev', f'socket,id=uart,host=127.0.0.1,port={args.serial_port},server=on,wait=off,logfile={logfile}',
                '-serial', 'chardev:uart']
    else:
        cmd += ['-serial', 'stdio']
    if args.gdb_port:
        cmd += ['-gdb', f'tcp:127.0.0.1:{args.gdb_port}']
    if args.pause:
        cmd += ['-S']
    if args.vnc_display is not None:
        cmd += ['-vnc', f'127.0.0.1:{args.vnc_display}']
    if args.ssh_port:
        cmd += ['-netdev', f'user,id=net,hostfwd=tcp:127.0.0.1:{args.ssh_port}-:22',
                '-device', 'usb-net,netdev=net']
    return cmd


def run(args):
    cmd = command(args)
    if args.dry_run:
        print(json.dumps(cmd, indent=2))
        return
    models = subprocess.check_output([args.qemu, '-machine', 'help'], text=True)
    if 'raspi4b ' not in models:
        raise ValueError('QEMU lacks raspi4b; run tools/build_emulator_qemu.py')
    (args.workspace / 'last-command.json').write_text(json.dumps(cmd, indent=2) + '\n')
    return subprocess.call(cmd)


def qmp(port, operation, arguments=None):
    with socket.create_connection(('127.0.0.1', port), timeout=5) as sock:
        with sock.makefile('rwb', buffering=0) as stream:
            greeting = json.loads(stream.readline())
            if 'QMP' not in greeting:
                raise ValueError('Endpoint is not a QMP monitor')
            for index, (name, params) in enumerate([('qmp_capabilities', {}), (operation, arguments or {})]):
                stream.write((json.dumps({'execute': name, 'arguments': params, 'id': index}) + '\n').encode())
                while True:
                    line = stream.readline()
                    if not line:
                        raise ConnectionError('QMP closed before replying')
                    response = json.loads(line)
                    if name == 'quit' and response.get('event') == 'SHUTDOWN':
                        return {}
                    if response.get('id') == index:
                        break
                if 'error' in response:
                    raise ValueError(response['error'])
            return response['return']


def export(args):
    workspace = args.workspace.resolve()
    read_config(workspace)
    # qemu-img refuses its exclusive lock if QEMU is using the overlay.
    # Convert to a temporary file in the destination directory, then publish
    # without overwriting an existing image (including our own backing file).
    target = args.output.resolve()
    if target.exists():
        raise ValueError('Export destination already exists')
    temporary = target.with_name(target.name + '.partial')
    with temporary.open('xb'):
        pass
    try:
        subprocess.run([args.qemu_img, 'convert', '-f', 'qcow2', '-O', 'raw',
                        str(workspace / 'disk.qcow2'), str(temporary)], check=True)
        os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    print(f'Exported {target}; sha256={digest(target)}')


def shell_command(sock, script, timeout=30):
    """Run a command on an already logged-in Linux shell; require its exit code.

    Base64 keeps quotes, newlines, and terminal control bytes out of the input
    line. Short commands only: Linux canonical TTY input has a bounded buffer.
    """
    marker = 'UC_' + uuid.uuid4().hex
    encoded = base64.b64encode(script.encode()).decode()
    line = f"printf '%s' '{encoded}' | base64 -d | /bin/bash; printf '\\n{marker}:%s\\n' $?\n"
    if len(line) > 3500:
        raise ValueError('Serial command exceeds canonical TTY line limit')
    sock.settimeout(timeout)
    sock.sendall(line.encode())
    end = time.monotonic() + timeout
    output = b''
    token = ('\n' + marker + ':').encode()
    while time.monotonic() < end:
        sock.settimeout(max(0.1, end - time.monotonic()))
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError('Guest serial console closed')
        output += chunk
        if token in output:
            result = output.split(token, 1)[1]
            if b'\n' in result:
                code = int(result.split(b'\n', 1)[0].strip())
                if code:
                    raise ValueError(f'Guest command failed ({code}): {output.decode(errors="replace")}')
                return output.decode(errors='replace')
    raise TimeoutError('Guest shell did not acknowledge command')


def put(args):
    payload = args.source.read_bytes()
    if len(payload) > 1024 * 1024:
        raise ValueError('Serial upload is limited to 1 MiB; use SSH/SCP for larger files')
    if not args.destination.startswith('/') or '\0' in args.destination:
        raise ValueError('Guest destination must be an absolute Linux path')
    temporary = '/tmp/uconsole-upload-' + uuid.uuid4().hex
    with socket.create_connection(('127.0.0.1', args.serial_port), timeout=5) as sock:
        # Prove a live shell before beginning a transaction.
        shell_command(sock, 'test "$(id -u)" = 0')
        try:
            shell_command(sock, f'umask 077; : > {temporary}')
            for pos in range(0, len(payload), 1024):
                encoded = base64.b64encode(payload[pos:pos + 1024]).decode()
                shell_command(sock, f"printf '%s' '{encoded}' | base64 -d >> {temporary}")
            expected = hashlib.sha256(payload).hexdigest()
            dest = shlex.quote(args.destination)
            output = shell_command(sock, f"test \"$(sha256sum {temporary} | cut -d' ' -f1)\" = {expected} && "
                                   f'install -m 644 {temporary} {dest} && sync && sha256sum {dest}')
            print(output)
        finally:
            shell_command(sock, f'rm -f {temporary}')


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--workspace', type=Path, default=DEFAULT)
    p.add_argument('--qemu', default=executable('qemu-system-aarch64'))
    p.add_argument('--qemu-img', default=executable('qemu-img'))
    sub = p.add_subparsers(dest='action', required=True)
    prep = sub.add_parser('prepare', help='Create an isolated writable workspace')
    prep.add_argument('image', type=Path)
    prep.add_argument('--sha256')
    prep.set_defaults(func=prepare)
    launch = sub.add_parser('run', help='Boot the CM4 image (partial hardware coverage)')
    launch.add_argument('--mode', choices=['normal', 'maintenance'], default='normal')
    launch.add_argument('--serial-port', type=int)
    launch.add_argument('--qmp-port', type=int, default=4444)
    launch.add_argument('--gdb-port', type=int)
    launch.add_argument('--ssh-port', type=int)
    launch.add_argument('--vnc-display', type=int)
    launch.add_argument('--pause', action='store_true')
    launch.add_argument('--dry-run', action='store_true')
    launch.set_defaults(func=run)
    control = sub.add_parser('control', help='Control an existing QEMU process')
    control.add_argument('operation', choices=['query-status', 'stop', 'cont', 'system_reset', 'quit'])
    control.add_argument('--qmp-port', type=int, default=4444)
    control.set_defaults(func=lambda args: print(json.dumps(qmp(args.qmp_port, args.operation))))
    output = sub.add_parser('export', help='Export a stopped, cleanly shut down overlay to raw SD image')
    output.add_argument('output', type=Path)
    output.set_defaults(func=export)
    upload = sub.add_parser('put', help='Copy a host file into a running maintenance shell (root)')
    upload.add_argument('source', type=Path)
    upload.add_argument('destination', help='Absolute guest path; overwrites that file')
    upload.add_argument('--serial-port', type=int, default=4445)
    upload.set_defaults(func=put)
    return p


def main():
    args = parser().parse_args()
    try:
        return args.func(args) or 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
