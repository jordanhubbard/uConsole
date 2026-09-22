#!/usr/bin/env python3
"""Exercise the uConsole AXP221 through the emulated BCM2835 I2C controller."""
from pathlib import Path
import socket
import subprocess
import tempfile
import time

from uconsole_emulator import executable


I2C = 0xFE205000
C, S, DLEN, A, FIFO = (0x00, 0x04, 0x08, 0x0C, 0x10)
I2CEN, ST, READ = (1 << 15), (1 << 7), 1
DONE, ERR = (1 << 1), (1 << 8)


def connect(path, process):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        sock = socket.socket(socket.AF_UNIX)
        try:
            sock.connect(str(path))
            sock.settimeout(5)
            return sock
        except OSError:
            sock.close()
            if process.poll() is not None:
                raise RuntimeError('QEMU exited before opening qtest socket')
            time.sleep(0.05)
    raise TimeoutError(path)


def main():
    with tempfile.TemporaryDirectory(prefix='uc-pmic-qtest-') as directory:
        directory = Path(directory)
        with (directory / 'qemu.log').open('wb') as log:
            process = subprocess.Popen([
                executable('qemu-system-aarch64'), '-M', 'raspi4b',
                '-accel', 'qtest', '-display', 'none', '-serial', 'none',
                '-monitor', 'none', '-qtest',
                f'unix:{directory}/test.sock,server=on,wait=off',
            ], stdout=log, stderr=log)
            try:
                with connect(directory / 'test.sock', process) as sock:
                    test = sock.makefile('rwb', buffering=0)

                    def qtest(command):
                        test.write((command + '\n').encode())
                        while True:
                            line = test.readline().decode().strip()
                            if line.startswith('IRQ '):
                                continue
                            assert line.startswith('OK'), line
                            return line

                    def write(offset, value):
                        qtest(f'writel 0x{I2C + offset:x} 0x{value:x}')

                    def read(offset):
                        return int(qtest(f'readl 0x{I2C + offset:x}').split()[1], 16)

                    # The AXP2xx model uses a write to select a register, followed
                    # by a read transaction. AXP221 silicon identifies as 0x06.
                    write(A, 0x34)
                    write(DLEN, 1)
                    write(C, I2CEN | ST)
                    write(FIFO, 0x03)
                    assert read(S) & DONE
                    write(S, DONE)

                    write(DLEN, 1)
                    write(C, I2CEN | ST | READ)
                    assert read(FIFO) == 0x06
                    assert read(S) & DONE
                    assert not read(S) & ERR

                    # An unused address must report a NACK through the controller.
                    write(S, DONE)
                    write(A, 0x35)
                    write(DLEN, 1)
                    write(C, I2CEN | ST | READ)
                    assert read(S) & ERR
                    print('PASS: AXP221 chip ID 0x06 at I2C address 0x34; unused address NACKs')
            except Exception:
                print((directory / 'qemu.log').read_text())
                raise
            finally:
                process.terminate()
                process.wait(timeout=10)


if __name__ == '__main__':
    main()
