import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from forge_backup_source import inputs, prepare
from forge_recovery_restore_source import digest, validate
import test_recovery_archive_hash


class BackupSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = test_recovery_archive_hash.RecoveryArchiveHashTests()
        self.backup = self.fixture.fixture(self.root)
        self.reviewed = inputs(self.backup, digest(self.fixture.accepted))
        self.output = self.root/'prepared'

    def test_real_archive_to_verified_chunk_source_without_card_copy(self):
        result = prepare(self.output, self.reviewed)
        self.assertEqual(result['status'], 'prepared-backup-root-source')
        manifest = json.loads((self.output/'source/manifest.json').read_text())
        validate(manifest, result['manifest_sha256'])
        self.assertEqual(manifest['backup_plan_sha256'], self.reviewed['plan_sha256'])
        self.assertEqual(manifest['root'], result['root'])
        self.assertEqual(result['chunk_count'], len(manifest['chunks']))
        for key in ('target_contacted', 'target_written', 'lease_acquired',
                    'filesystem_consistency_qualified', 'restore_authorized',
                    'normal_boot_release_authorized'):
            self.assertIs(result[key], False)
        self.assertEqual(list(self.output.rglob('*.img')), [])
        for path in self.output.rglob('*'):
            self.assertEqual(path.stat().st_mode & 0o077, 0)
        with self.assertRaises(FileExistsError):
            prepare(self.output, self.reviewed)

    def test_wrong_pin_and_changed_review_rejected_without_output(self):
        with self.assertRaises(ValueError): inputs(self.backup, '0'*64)
        self.fixture.plan['unexpected_change'] = True
        self.fixture.save(self.backup/'plan.json', self.fixture.plan)
        with self.assertRaises(ValueError): prepare(self.output, self.reviewed)
        self.assertFalse(self.output.exists())

    def test_public_or_replaced_archive_rejected(self):
        (self.backup/'card.img.gz').chmod(0o644)
        with self.assertRaises(PermissionError): prepare(self.output, self.reviewed)
        self.assertFalse(self.output.exists())

    def test_output_overlap_rejected(self):
        for path in (self.backup, self.backup/'nested', self.root):
            with self.assertRaises(ValueError): prepare(path, self.reviewed)

    def test_corrupt_archive_retains_failure_and_no_outer_acceptance(self):
        archive = self.backup/'card.img.gz'
        payload = bytearray(archive.read_bytes())
        payload[-8] ^= 1
        archive.write_bytes(payload)
        self.reviewed = inputs(self.backup, digest(self.fixture.accepted))
        with self.assertRaises(OSError): prepare(self.output, self.reviewed)
        self.assertTrue((self.output/'failure.json').exists())
        self.assertTrue((self.output/'ranges/acceptance.json').exists())
        self.assertFalse((self.output/'acceptance.json').exists())

    def test_mutation_between_scans_is_not_accepted(self):
        from forge_backup_source import capture
        def changed(*args, **kwargs):
            result = capture(*args, **kwargs)
            self.fixture.plan['changed'] = True
            self.fixture.save(self.backup/'plan.json', self.fixture.plan)
            return result
        with patch('forge_backup_source.capture', side_effect=changed):
            with self.assertRaises(ValueError): prepare(self.output, self.reviewed)
        self.assertFalse((self.output/'acceptance.json').exists())
        self.assertTrue((self.output/'failure.json').exists())
