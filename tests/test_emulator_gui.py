"""Desktop editor lifecycle checks; run under Xvfb on headless Linux."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
try:
    import tkinter as tk
except ImportError:
    tk = None


@unittest.skipUnless(tk and (os.environ.get('DISPLAY') or sys.platform in ('darwin', 'win32')), 'needs Tk and a desktop display')
class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        from uconsole_workbench import Workbench
        self.temporary = tempfile.TemporaryDirectory()
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = Workbench(self.root, Path(self.temporary.name))

    def tearDown(self):
        self.root.destroy()
        self.temporary.cleanup()

    def test_editor_saves_exact_content_and_tracks_unsaved_edits(self):
        path = Path(self.temporary.name) / 'example.txt'
        self.app.filename = path
        self.app.editor.insert('1.0', 'uConsole\nsecond line\n')
        self.assertTrue(self.app.editor.edit_modified())
        self.app.save_file()
        self.assertEqual(path.read_text(), 'uConsole\nsecond line\n')
        self.assertFalse(self.app.editor.edit_modified())

    def test_stopped_controls_do_not_connect_to_unowned_vm(self):
        with patch('uconsole_workbench.qmp') as monitor:
            with self.assertRaises(ValueError):
                self.app.control('stop')
            monitor.assert_not_called()


if __name__ == '__main__':
    unittest.main()
