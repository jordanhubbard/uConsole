"""Image preservation, DTB edits, and host-independent launch contract."""
import importlib.util
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from emulator_image import BootPartition
from emulator_dtb import patch_strings
import uconsole_emulator as emulator
import uconsole_agent as agent


def sample_dtb():
    def word(*args):
        return struct.pack('>' + 'I' * len(args), *args)
    structure = word(1) + b'\0' * 4 + word(1) + b'soc\0'
    structure += word(3, 5, 0) + b'okay\0\0\0\0' + word(2, 2, 9)
    strings = b'status\0'
    return word(0xd00dfeed, 56 + len(structure) + len(strings), 56,
                56 + len(structure), 40, 17, 16, 0, len(strings), len(structure)) + b'\0' * 16 + structure + strings


class EmulatorTests(unittest.TestCase):
    def test_dtb_add_delete_and_change(self):
        original = sample_dtb()
        changed = patch_strings(original, {('/soc', 'status'): 'disabled', ('/soc', 'dr_mode'): 'host'})
        self.assertIn(b'disabled\0', changed)
        self.assertIn(b'host\0', changed)
        deleted = patch_strings(changed, {('/soc', 'status'): None})
        self.assertNotIn(b'disabled\0', deleted)
        self.assertEqual(struct.unpack_from('>I', deleted, 4)[0], len(deleted))
        self.assertEqual(patch_strings(original, {}), original)
        with self.assertRaises(ValueError):
            patch_strings(original, {('/nonexistent', 'status'): 'okay'})

    def test_fat16_extraction_and_cycle_rejection(self):
        data = bytearray(512 * 100)
        data[510:512] = b'\x55\xaa'
        data[450] = 6
        struct.pack_into('<II', data, 454, 1, 99)
        struct.pack_into('<H', data, 512 + 11, 512)
        data[512 + 13] = 1
        struct.pack_into('<H', data, 512 + 14, 1)
        data[512 + 16] = 1
        struct.pack_into('<H', data, 512 + 17, 16)
        struct.pack_into('<H', data, 512 + 22, 1)
        struct.pack_into('<H', data, 1024 + 4, 0xffff)
        data[1536:1547] = b'KERNEL8 IMG'
        struct.pack_into('<H', data, 1536 + 26, 2)
        struct.pack_into('<I', data, 1536 + 28, 5)
        data[2048:2053] = b'hello'
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / 'sd.img'
            target = Path(directory) / 'kernel.img'
            image.write_bytes(data)
            with BootPartition(image) as boot:
                boot.extract('kernel8.img', target)
                self.assertEqual(target.read_bytes(), b'hello')
                with self.assertRaises(FileExistsError):
                    boot.extract('kernel8.img', target)
                with self.assertRaises(FileNotFoundError):
                    boot.extract('missing.img', target)
            self.assertEqual(image.read_bytes(), data)
            struct.pack_into('<H', data, 1024 + 4, 2)
            image.write_bytes(data)
            with BootPartition(image) as boot:
                with self.assertRaisesRegex(ValueError, 'cyclic'):
                    list(boot.chain(2))

    def test_checksum_failure_creates_no_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / 'bad.bz2'
            image.write_bytes(b'not the expected image')
            workspace = Path(directory) / 'workspace'
            args = emulator.parser().parse_args(['--workspace', str(workspace), 'prepare', str(image)])
            with self.assertRaisesRegex(ValueError, 'checksum'):
                emulator.prepare(args)
            self.assertFalse(workspace.exists())

    def test_launch_uses_overlay_and_loopback_only(self):
        with tempfile.TemporaryDirectory(prefix='cm4, test ') as directory:
            workspace = Path(directory)
            (workspace / 'machine.json').write_text(json.dumps({'root': 'PARTUUID=12345678-02'}))
            args = emulator.parser().parse_args(['--workspace', directory, 'run', '--mode', 'maintenance',
                '--serial-port', '4445', '--gdb-port', '1234', '--ssh-port', '2222', '--vnc-display', '1',
                '--keyboard-cdc-port', '4550', '--modem-at-port', '4551'])
            cmd = emulator.command(args)
            self.assertIn('raspi4b', cmd)
            self.assertNotIn('virt', cmd)
            self.assertIn('init=/bin/bash', cmd[cmd.index('-append') + 1])
            drive = cmd[cmd.index('-drive') + 1]
            self.assertIn('disk.qcow2', drive)
            self.assertIn('cm4,, test', drive)
            self.assertNotIn('base.img', drive)
            for flag in ['-qmp', '-gdb', '-vnc']:
                self.assertIn('127.0.0.1', cmd[cmd.index(flag) + 1])
            self.assertTrue(any('id=keyboard-cdc' in item for item in cmd))
            self.assertTrue(any('id=modem-at' in item for item in cmd))
            self.assertEqual(cmd[cmd.index('-display') + 1], 'none')

    def test_local_display_and_vnc_are_mutually_exclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / 'machine.json').write_text(json.dumps({'root': 'PARTUUID=12345678-02'}))
            local = emulator.parser().parse_args(['--workspace', directory, 'run', '--display', 'gtk'])
            self.assertEqual(emulator.command(local)[emulator.command(local).index('-display') + 1], 'gtk')
            conflict = emulator.parser().parse_args([
                '--workspace', directory, 'run', '--display', 'sdl', '--vnc-display', '1'])
            with self.assertRaisesRegex(ValueError, 'cannot be combined'):
                emulator.command(conflict)

    def test_export_never_overwrites_existing_image(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / 'machine.json').write_text('{}')
            target = workspace / 'base.img'
            target.write_bytes(b'preserve')
            args = emulator.parser().parse_args(['--workspace', directory, 'export', str(target)])
            with self.assertRaisesRegex(ValueError, 'already exists'):
                emulator.export(args)
            self.assertEqual(target.read_bytes(), b'preserve')

    def test_agent_context_is_clean_and_explicit_about_limits(self):
        state = {'workspace': '/tmp/work', 'machine': {'machine': 'raspi4b',
                 'coverage': 'partial-cm4', 'root': 'PARTUUID=1234-02',
                 'source_sha256': 'abc'}, 'runtime': {'status': 'running'},
                 'serial_tail': ['\x1b[31mbooted\x1b[0m']}
        text = agent.context_markdown(state)
        self.assertIn('partial-cm4', text)
        self.assertIn('Known fidelity limits', text)
        self.assertNotIn('\x1b', agent.clean(text))

    def test_task_schema_and_host_argv_are_machine_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            task_file = Path(directory) / 'tasks.json'
            task_file.write_text(json.dumps({'schema': 1, 'tasks': {
                'answer': {'kind': 'host', 'argv': [sys.executable, '-c', 'print(42)']}}}))
            tasks = agent.load_tasks(task_file)
            result = agent.run_task('answer', tasks['answer'], 1)
            self.assertEqual(result['exit_code'], 0)
            self.assertEqual(result['stdout'], '42\n')

    def test_hardware_probe_comparison_reports_changed_probes(self):
        from uconsole_hardware_probe import compare, run_command
        first = {'target': 'emulator', 'probes': {'model': {'exit_code': 0, 'stdout': 'CM4'}}}
        second = {'target': 'hardware', 'probes': {'model': {'exit_code': 0, 'stdout': 'uConsole'}}}
        result = compare(first, second)
        self.assertFalse(result['matching'])
        self.assertEqual(result['differences'][0]['probe'], 'model')
        missing = run_command(['/definitely/missing/uconsole-probe'], None)
        self.assertIsNone(missing['exit_code'])
        self.assertTrue(missing['stderr'])


if __name__ == '__main__':
    unittest.main()
