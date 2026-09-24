"""Validate flashing failures without accessing a keyboard or USB device."""
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tarfile
import tempfile
import unittest

from tools.package_flash import package

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'Bin/uconsole_keyboard_flash'


class FlashTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.bundle = self.work / 'bundle with spaces'
        self.bundle.mkdir()
        self.bin = self.work / 'bin'
        self.bin.mkdir()
        self.log = self.work / 'calls.jsonl'
        for name in ('maple_upload', 'flash.sh'):
            shutil.copy2(SOURCE / name, self.bundle / name)
        self.firmware = self.bundle / 'firmware with spaces.bin'
        self.firmware.write_bytes(b'test firmware')
        self.env = dict(os.environ, PATH=f'{self.bin}:/usr/bin:/bin',
                        CALL_LOG=str(self.log), DFU_RESULT='0', RESET_RESULT='0')
        for name in ('dfu-util', 'reset'):
            self.stub(name, '''#!/usr/bin/python3
import json, os, sys
name = os.path.basename(sys.argv[0])
with open(os.environ['CALL_LOG'], 'a') as f:
    f.write(json.dumps([name, *sys.argv[1:]]) + '\\n')
sys.exit(int(os.environ['DFU_RESULT' if name == 'dfu-util' else 'RESET_RESULT']))
''')
        self.stub('sleep', '#!/bin/sh\nexit 0\n')
        self.stub('dpkg', '#!/bin/sh\necho "${TEST_ARCH:-arm64}"\n')
        self.env['DFU_UTIL'] = str(self.bin / 'dfu-util')
        self.env['UPLOAD_RESET'] = str(self.bin / 'reset')

    def stub(self, name, source):
        path = self.bin / name
        path.write_text(source)
        path.chmod(0o755)

    def calls(self):
        return [json.loads(s) for s in self.log.read_text().splitlines()] if self.log.exists() else []

    def run_upload(self, port='/dev/null', extra=()):
        return subprocess.run(['bash', str(self.bundle / 'maple_upload'), port,
                               '2', '1EAF:0003', str(self.firmware), *extra],
                              env=self.env, cwd=self.work, capture_output=True,
                              text=True, timeout=5)

    def test_success_preserves_arguments_and_path_spaces(self):
        result = self.run_upload(extra=('0x08002000:leave',))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Done:', result.stdout)
        self.assertEqual(self.calls(), [
            ['reset', '/dev/null', '750'], ['reset', '/dev/null', '750'],
            ['dfu-util', '-d', '1EAF:0003', '-a', '2', '-D', str(self.firmware),
             '--dfuse-address', '0x08002000:leave', '-R']])

    def test_dfu_failure_propagates(self):
        self.env['DFU_RESULT'] = '74'
        result = self.run_upload()
        self.assertEqual(result.returncode, 74)
        self.assertIn('DFU upload failed', result.stderr)
        self.assertNotIn('Done', result.stdout)
        self.assertNotIn('waiting', result.stdout)

    def test_already_in_bootloader_can_upload_after_reset_failure(self):
        self.env['RESET_RESULT'] = '1'
        result = self.run_upload()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('reset attempt 1 failed', result.stderr)
        self.assertEqual(self.calls()[-1][0], 'dfu-util')

    def test_reenumeration_timeout_is_not_success(self):
        result = self.run_upload(port='uconsole-test-missing-port')
        self.assertEqual(result.returncode, 1)
        self.assertIn('did not return', result.stderr)
        self.assertNotIn('Done', result.stdout)

    def test_preflight_errors_never_reset_keyboard(self):
        self.firmware.unlink()
        self.assertEqual(self.run_upload().returncode, 2)
        self.assertEqual(self.calls(), [])
        self.firmware.write_bytes(b'firmware')
        (self.bin / 'dfu-util').unlink()
        self.assertEqual(self.run_upload().returncode, 2)
        self.assertEqual(self.calls(), [])

    def test_unsupported_architecture_needs_explicit_helper(self):
        self.env.pop('UPLOAD_RESET')
        self.env['TEST_ARCH'] = 'unknown'
        self.assertEqual(self.run_upload().returncode, 2)
        self.assertEqual(self.calls(), [])

    def test_userspace_armhf_selects_armhf_helper(self):
        self.env.pop('UPLOAD_RESET')
        self.env['TEST_ARCH'] = 'armhf'
        helper = self.bundle / 'deb_packages/armhf/upload-reset.elf'
        helper.parent.mkdir(parents=True)
        helper.write_text('#!/bin/sh\nexit 0\n')
        helper.chmod(0o755)
        self.assertEqual(self.run_upload().returncode, 0)

    def test_flash_wrapper_works_outside_bundle(self):
        self.stub('sudo', '#!/bin/sh\nexec "$@"\n')
        self.firmware.rename(self.bundle / 'uconsole_keyboard.ino.bin')
        (self.bundle / 'maple_upload').write_text(
            '#!/bin/sh\n[ -s "$4" ] && [ "$1" = ttyACM0 ]\n')
        result = subprocess.run(['bash', str(self.bundle / 'flash.sh')],
                                cwd=self.work, env=self.env, timeout=5)
        self.assertEqual(result.returncode, 0)


class NativeResetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.binary = Path(cls.tmp.name) / 'upload-reset'
        subprocess.run(['cc', '-O2', '-Wall', '-Wextra', '-Werror',
                        str(SOURCE / 'upload-reset/upload-reset.c'),
                        '-o', str(cls.binary)], check=True)

    def test_bad_arguments_or_serial_device_fail(self):
        for args in ([], ['/dev/null'], ['/dev/null', '-1'], ['/dev/null', 'abc'],
                     ['/dev/null', '60001'], ['/dev/null', ''],
                     ['/dev/null', '999999999999999999999999'],
                     ['/dev/uconsole-test-missing-port', '0']):
            with self.subTest(args=args):
                result = subprocess.run([str(self.binary), *args],
                                        capture_output=True, text=True, timeout=2)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(result.stderr)


class PackageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.build = Path(self.tmp.name)
        (self.build / 'firmware').mkdir()
        (self.build / 'firmware/uconsole_keyboard.ino.bin').write_bytes(b'firmware')

    def write_helper(self, header):
        helper = self.build / 'upload-reset.elf'
        helper.write_bytes(header + b'native helper')
        helper.chmod(0o755)

    def test_linux_aarch64_release_bundle(self):
        header = bytearray(20)
        header[:6] = b'\x7fELF\x02\x01'
        struct.pack_into('<H', header, 18, 183)
        self.write_helper(header)
        output = package(self.build)
        self.assertEqual(output.name, 'uconsole_keyboard_flash-linux-aarch64.tar.gz')
        with tarfile.open(output) as archive:
            self.assertIn('uconsole_keyboard_flash/upload-reset', archive.getnames())

    def test_macos_arm64_release_bundle(self):
        header = bytearray(20)
        header[:4] = b'\xcf\xfa\xed\xfe'
        struct.pack_into('<I', header, 4, 0x0100000C)
        self.write_helper(header)
        output = package(self.build)
        self.assertEqual(output.name, 'uconsole_keyboard_flash-macos-arm64.tar.gz')


if __name__ == '__main__':
    unittest.main()
