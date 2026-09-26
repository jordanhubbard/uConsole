import gc
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import tkinter as tk
from forge_client import ClientSession
from forge_controller import Controller
from forge_ram_transport import RecoveryProbe
from forge_recovery_gui import RecoveryPanel, policy_pin
from forge_recovery_session import prepare


@unittest.skipUnless(os.environ.get('DISPLAY') or sys.platform == 'darwin', 'needs native Tk or Xvfb')
class RecoveryPanelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        for name in ('key', 'known_hosts'):
            (self.path/name).write_text('private credential canary ' + name)
            (self.path/name).chmod(0o600)
        probe = RecoveryProbe('fixture', self.path/'key', self.path/'known_hosts',
                              'a'*32, '6.12.62-v8+', '100000007b961d25')
        session = self.path/'session'
        record = prepare(session, probe, '11111111-2222-3333-4444-555555555555', 'b'*64)
        self.definition = dict(schema=1, jobs=dict(proof=dict(workspace='gui', machine_id='c'*32,
            session=str(session), session_sha256=record['binding_sha256'], key=str(probe.key),
            known_hosts=str(probe.known_hosts), operation='reconcile-root',
            arguments=dict(journal=str(self.path/'journal'), plan_sha256='d'*64))))
        self.policy = self.path/'policy.json'
        self.policy.write_text(json.dumps(self.definition))
        self.owner = Controller({'gui': self.path/'workspace'})
        self.root = tk.Tk()
        self.root.withdraw()
        self.panel = RecoveryPanel(self.root, self.owner, 'gui')

    def tearDown(self):
        self.owner.close()
        self.root.destroy()
        self.panel = self.root = self.owner = None
        gc.collect()
        self.temp.cleanup()

    def approve(self):
        self.panel.load_policy(self.policy)
        with patch('forge_recovery_gui.messagebox.askyesno', return_value=True):
            self.panel.approve()
        self.assertIsNone(self.panel.pending)

    def test_unconfigured_panel_does_not_enable_actions(self):
        self.assertIn('disabled', self.panel.run_button.state())
        self.assertIn('disabled', self.panel.approve_button.state())
        self.assertIn('--recovery-policy', self.panel.details.get('1.0', 'end'))
        self.assertEqual(self.owner.jobs, {})

    def test_review_is_local_and_never_displays_credential_contents(self):
        with patch.object(RecoveryProbe, '_observe', side_effect=AssertionError('unexpected SSH')):
            self.panel.load_policy(self.policy)
        self.assertNotIn('target-recovery', self.owner.grants)
        text = self.panel.details.get('1.0', 'end')
        self.assertIn('fixture', text)
        self.assertIn('d'*64, text)
        self.assertNotIn('private credential canary', text)
        self.assertIn('disabled', self.panel.run_button.state())
        self.assertNotIn('disabled', self.panel.approve_button.state())

    def test_approval_does_not_execute_or_expand_existing_client_grants(self):
        client = ClientSession(self.owner, ['gui'])
        self.approve()
        self.assertIn('target-recovery', self.owner.grants)
        self.assertFalse(client.call('recovery_jobs', {'workspace': 'gui'})['execution_granted'])
        self.assertEqual(self.owner.jobs, {})
        self.assertNotIn('disabled', self.panel.run_button.state())

    def test_changed_policy_bytes_refused_at_approval(self):
        self.panel.load_policy(self.policy)
        self.policy.write_text('{}')
        with patch('forge_recovery_gui.messagebox.askyesno', return_value=True):
            self.panel.approve()
        self.assertIn('Approval failed', self.panel.status.get())
        self.assertNotIn('target-recovery', self.owner.grants)
        self.assertIsNotNone(self.panel.pending)
        self.assertEqual(self.owner.jobs, {})

    def test_cancelled_approval_and_execution_have_no_effect(self):
        self.panel.load_policy(self.policy)
        with patch('forge_recovery_gui.messagebox.askyesno', return_value=False):
            self.panel.approve()
        self.assertNotIn('target-recovery', self.owner.grants)
        self.approve()
        with patch('forge_recovery_gui.messagebox.askyesno', return_value=False):
            self.panel.start()
        self.assertIsNone(self.panel.job)
        self.assertEqual(self.owner.jobs, {})

    def test_changed_policy_during_modal_confirmation_refused(self):
        self.approve()
        def change(*args, **kwargs):
            self.owner.approve_recovery_policy(self.policy, policy_pin(self.policy))
            return True
        with patch('forge_recovery_gui.messagebox.askyesno', side_effect=change):
            self.panel.start()
        self.assertIsNone(self.panel.job)
        self.assertEqual(self.owner.jobs, {})
        self.assertIn('changed during confirmation', self.panel.status.get())

    def test_active_job_blocks_close_reload_resubmit_and_survives_status_error(self):
        self.approve()
        entered, finish = threading.Event(), threading.Event()
        def execute(job):
            entered.set()
            if not finish.wait(5): raise TimeoutError('fixture not released')
            return dict(root_written=False, normal_boot_release_authorized=False)
        with patch('forge_recovery_jobs.execute', side_effect=execute) as worker, \
                patch('forge_recovery_gui.messagebox.askyesno', return_value=True):
            self.panel.start()
            job = self.panel.job
            try:
                self.assertTrue(entered.wait(2))
                self.assertFalse(self.panel.close())
                with self.assertRaises(ValueError): self.panel.load_policy(self.policy)
                self.panel.start()
                worker.assert_called_once()
                with patch.object(self.owner, 'job', side_effect=ConnectionError('status lost')):
                    self.panel.poll()
                self.assertEqual(self.panel.job, job)
                self.assertIn('Do not resubmit', self.panel.status.get())
                self.assertNotIn('disabled', self.panel.recheck_button.state())
                self.assertIn('disabled', self.panel.run_button.state())
            finally:
                finish.set()
                self.owner.jobs[job][2].result(timeout=5)
        self.panel.poll()
        self.assertIsNone(self.panel.job)
        self.assertIn('completed', self.panel.status.get())
        self.assertIn('No rollback', self.panel.status.get())
        self.assertIn('false', self.panel.details.get('1.0', 'end'))

    def test_failed_worker_retains_evidence_without_retry(self):
        self.approve()
        with patch('forge_recovery_jobs.execute', side_effect=RuntimeError('uncertain write')) as worker, \
                patch('forge_recovery_gui.messagebox.askyesno', return_value=True):
            self.panel.start()
            with self.assertRaises(RuntimeError):
                self.owner.jobs[self.panel.job][2].result(timeout=5)
            self.panel.poll()
            worker.assert_called_once()
        self.assertIn('uncertain write', self.panel.details.get('1.0', 'end'))
        self.assertIn('reconcile uncertain writes', self.panel.status.get())

    def test_nonregular_or_oversized_policy_refused(self):
        fifo = self.path/'fifo'
        os.mkfifo(fifo)
        with self.assertRaises(ValueError): policy_pin(fifo)
        self.policy.write_bytes(b' '*(1024*1024+1))
        with self.assertRaises(ValueError): policy_pin(self.policy)

    def test_normal_ssh_preparation_uses_shared_job_without_approving_staging(self):
        import test_recovery_stage_prepare
        fixture = test_recovery_stage_prepare.RecoveryStagePreparationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.panel.prepare_staging(fixture.publication, fixture.frozen['publication_sha256'],
                                   fixture.root/'firmware.json', fixture.frozen['firmware_sha256'], fixture.output)
        job = self.panel.job
        self.assertFalse(self.panel.close())
        self.owner.jobs[job][2].result(timeout=5)
        self.panel.poll()
        self.assertIsNone(self.panel.job)
        self.assertIsNone(self.panel.pending)
        self.assertIsNone(self.owner.targets)
        self.assertEqual(self.owner.grants, frozenset())
        self.assertIn('not approved, staged, or boot-qualified', self.panel.status.get())
        self.assertIn('prepared-not-approved', self.panel.details.get('1.0', 'end'))

    def test_fresh_session_enrollment_is_owner_job_and_never_approves_policy(self):
        import test_session_enrollment
        fixture = test_session_enrollment.SessionEnrollmentTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with fixture.observe():
            self.panel.enroll_session(fixture.value, fixture.output)
            job = self.panel.job
            self.assertFalse(self.panel.close())
            self.assertIn('disabled', self.panel.enroll_button.state())
            self.owner.jobs[job][2].result(timeout=5)
        self.panel.poll()
        self.assertIsNone(self.panel.job)
        self.assertIsNone(self.panel.pending)
        self.assertIsNone(self.owner.recovery_jobs)
        self.assertEqual(self.owner.grants, frozenset())
        self.assertIn('enrolled-not-leased', self.panel.details.get('1.0', 'end'))
        self.assertNotIn(fixture.value.owner, self.panel.details.get('1.0', 'end'))
        self.assertIn('separately reviewed job policy', self.panel.status.get())

    def test_pending_policy_blocks_session_enrollment(self):
        self.panel.load_policy(self.policy)
        self.assertIn('disabled', self.panel.enroll_button.state())
        with patch.object(self.owner, 'enroll_recovery_session') as submit:
            with self.assertRaises(ValueError): self.panel.enroll_session(None, self.path/'new')
            self.panel.enroll_dialog()
        submit.assert_not_called()

    def test_declined_enrollment_dialog_never_contacts_or_submits(self):
        import test_session_enrollment
        fixture = test_session_enrollment.SessionEnrollmentTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        value = fixture.value
        with patch('forge_recovery_gui.filedialog.askdirectory', side_effect=[str(fixture.stage), str(fixture.root)]), \
                patch('forge_recovery_gui.filedialog.askopenfilename', side_effect=[str(value.probe.key), str(value.probe.known_hosts)]), \
                patch('forge_recovery_gui.simpledialog.askstring', side_effect=[fixture.pin, value.probe.host,
                    value.probe.kernel, value.probe.serial, value.boot_id]), \
                patch('forge_recovery_gui.simpledialog.askinteger', return_value=1), \
                patch('forge_recovery_gui.messagebox.askyesno', return_value=False), \
                patch.object(RecoveryProbe, '_observe') as remote, \
                patch.object(self.owner, 'enroll_recovery_session') as submit:
            self.panel.enroll_button.invoke()
        remote.assert_not_called()
        submit.assert_not_called()
        self.assertEqual(self.owner.jobs, {})


if __name__ == '__main__':
    unittest.main()
