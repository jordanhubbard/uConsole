from pathlib import Path
import json
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from record_forge_desktop import load_secret, pointer_events, text_keys, firmware_chord


class DesktopRecorderTests(unittest.TestCase):
    def test_firmware_chord_uses_scanners_and_releases_modifiers(self):
        commands = firmware_chord(['Control_L', 'Alt_L', 't'])
        self.assertEqual(commands[:2], (('key', 10, 1), ('run', 10)))
        self.assertIn(('matrix', 3, 4, 1), commands)
        self.assertEqual(commands[-2:], (('key', 10, 0), ('run', 10)))
        for invalid in ([], ['unknown'], ['a'] * 9, [True], 'abc'):
            with self.assertRaises(ValueError):
                firmware_chord(invalid)
    def test_pointer_is_relative_and_bounded(self):
        self.assertEqual(pointer_events(-4, 7), [
            {'type': 'rel', 'data': {'axis': 'x', 'value': -4}},
            {'type': 'rel', 'data': {'axis': 'y', 'value': 7}}])
        for invalid in (True, 1.5, '2', -1025, 1025, None):
            with self.assertRaises(ValueError):
                pointer_events(invalid, 0)

    def test_reused_password_requires_private_valid_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'secrets.json'
            path.write_text(json.dumps({'onboarding_password': 'a' * 24}))
            path.chmod(0o600)
            self.assertEqual(load_secret(path), 'a' * 24)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                load_secret(path)
            path.chmod(0o600)
            path.write_text(json.dumps({'onboarding_password': 'unexpected'}))
            with self.assertRaises(ValueError):
                load_secret(path)

    def test_text_mapping_is_bounded_and_layout_independent(self):
        self.assertEqual(text_keys('forge 42'), ['f', 'o', 'r', 'g', 'e', 'spc', '4', '2'])
        for invalid in ('', 'A', '\n', 'é', 'a' * 129, None, ['a']):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                text_keys(invalid)


if __name__ == '__main__':
    unittest.main()
