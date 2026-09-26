"""Owner review and explicit foreground execution of prepared recovery jobs."""
import hashlib
import json
import os
from pathlib import Path
import stat
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


WARNINGS = {
    'backup-card': 'Read the offline card into a new private host backup. This does not authorize restoration.',
    'restore-root': 'Write the approved original backup to the offline root partition. Source filesystem errors require separate policy approval.',
    'deploy-root': 'Write the approved enhanced image root. The original backup remains the rollback source.',
    'reconcile-root': 'Fence and inspect the recorded attempt. This neither retries disk writes nor releases the recovery hold.',
    'retry-lease': 'Replay only the pending lease request with its original deadline. This does not adopt or extend a lease.',
}


def policy_pin(filename):
    fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('Select a regular owner policy file')
        data = stream.read(1024*1024+1)
    if len(data) > 1024*1024:
        raise ValueError('Recovery policy exceeds 1 MiB')
    return hashlib.sha256(data).hexdigest()


def summary(job):
    """Owner-only local review; never expose these paths through MCP listings."""
    return dict(name=job.name, operation=job.operation, effect=WARNINGS[job.operation],
                host=job.probe.host, port=job.probe.port, serial=job.probe.serial,
                kernel=job.probe.kernel, boot_id=job.boot_id, machine_id=job.machine_id,
                session=str(job.session), session_sha256=job.session_pin,
                key=str(job.probe.key), known_hosts=str(job.probe.known_hosts),
                arguments=job.arguments)


class RecoveryPanel:
    def __init__(self, parent, controller, workspace):
        self.controller, self.workspace = controller, workspace
        self.job = self.timer = self.pending = None
        self.window = tk.Toplevel(parent)
        self.window.title('Physical recovery — prepared jobs')
        self.selected = tk.StringVar()
        self.status = tk.StringVar(value='No action runs without confirmation. Prepared RAM-recovery session required.')
        ttk.Label(self.window, text='Backup → validated image change → deploy → reconcile → explicit boot release',
                  wraplength=760).pack(padx=12, pady=8)
        ttk.Label(self.window, text='Advanced prepared-job controls. Firmware staging, plan preparation and boot release are not automated here.',
                  wraplength=760).pack(padx=12, pady=4)
        controls = ttk.Frame(self.window)
        controls.pack(padx=12, pady=4)
        self.load_button = ttk.Button(controls, text='Review policy…', command=self.load_dialog)
        self.load_button.pack(side='left')
        self.approve_button = ttk.Button(controls, text='Approve reviewed policy…', command=self.approve)
        self.approve_button.pack(side='left', padx=4)
        self.approve_button.state(['disabled'])
        self.choice = ttk.Combobox(self.window, textvariable=self.selected, state='readonly', width=50)
        self.choice.pack(padx=12, pady=4)
        self.choice.bind('<<ComboboxSelected>>', lambda event: self.review())
        self.details = tk.Text(self.window, width=100, height=22, wrap='word', state='disabled')
        self.details.pack(padx=12, pady=4, fill='both', expand=True)
        actions = ttk.Frame(self.window)
        actions.pack(padx=12, pady=4)
        self.run_button = ttk.Button(actions, text='Run selected job…', command=self.start)
        self.run_button.pack(side='left')
        self.recheck_button = ttk.Button(actions, text='Recheck job status', command=self.poll)
        self.recheck_button.pack(side='left', padx=4)
        self.recheck_button.state(['disabled'])
        ttk.Label(self.window, textvariable=self.status, wraplength=760).pack(padx=12, pady=8)
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        self.refresh()

    def show(self, value):
        self.details.configure(state='normal')
        self.details.delete('1.0', 'end')
        self.details.insert('1.0', json.dumps(value, indent=2))
        self.details.configure(state='disabled')

    def refresh(self):
        listing = self.controller.call('recovery_jobs', {'workspace': self.workspace})
        names = [item['name'] for item in listing['jobs']]
        self.choice.configure(values=names, state='disabled' if self.job else 'readonly')
        if self.selected.get() not in names:
            self.selected.set(names[0] if names else '')
        self.run_button.state(['!disabled'] if names and listing['execution_granted'] and not self.job and not self.pending else ['disabled'])
        self.load_button.state(['disabled'] if self.job else ['!disabled'])
        self.approve_button.state(['!disabled'] if self.pending and not self.job else ['disabled'])
        self.recheck_button.state(['!disabled'] if self.job else ['disabled'])
        if not self.pending:
            self.review()

    def review(self):
        if self.pending:
            return
        try:
            self.show(summary(self.controller.recovery_jobs.get(self.selected.get(), self.workspace))
                      if self.selected.get() else {'configuration': 'Review an owner-prepared recovery policy, or start with --recovery-policy and --recovery-policy-sha256.'})
        except Exception as exc:
            self.status.set('Review unavailable: ' + str(exc))
            self.run_button.state(['disabled'])

    def load_dialog(self):
        if self.job:
            return
        filename = filedialog.askopenfilename(parent=self.window, title='Owner-prepared recovery policy',
                                              filetypes=[('JSON policy', '*.json')])
        if filename:
            try:
                self.load_policy(Path(filename))
            except Exception as exc:
                self.status.set('Policy review failed: ' + str(exc))

    def load_policy(self, filename):
        if self.job:
            raise ValueError('Wait for the recovery job before reviewing another policy')
        pin = policy_pin(filename)
        approved = self.controller.load_recovery_policy(filename, pin)
        self.pending = (Path(filename), pin)
        self.refresh()
        # Review all jobs, including other registered workspaces, before granting
        # the owner authority. Existing clients never gain a new grant implicitly.
        self.show(dict(policy_sha256=pin, jobs=[summary(item) for item in approved.jobs.values()]))
        self.status.set('Review every host, identity, operation, path and pin. Nothing has been approved or contacted.')

    def approve(self):
        if not self.pending or self.job:
            return
        filename, pin = self.pending
        if not messagebox.askyesno('Approve recovery policy',
                'Approve exactly the displayed policy?\n\nSHA-256: ' + pin +
                '\n\nThis enables later execution, but starts no action. Already authorized target-recovery clients can select its jobs.',
                parent=self.window):
            return
        try:
            self.controller.approve_recovery_policy(filename, pin)
            self.pending = None
            self.refresh()
            self.status.set('Policy approved. Select a job and review its effect before execution.')
        except Exception as exc:
            self.status.set('Approval failed; no action started: ' + str(exc))

    def start(self):
        if self.job or self.pending:
            return
        try:
            registry = self.controller.recovery_jobs
            job = registry.get(self.selected.get(), self.workspace)
            if not messagebox.askyesno('Confirm physical recovery action',
                    f'{job.operation} on {job.probe.host} (serial {job.probe.serial})?\n\n' + WARNINGS[job.operation] +
                    '\n\nRunning jobs cannot be cancelled. Failure can leave uncertain disk state. No automatic retry, rollback or reboot will occur.',
                    parent=self.window):
                return
            # A modal dialog processes GUI events. Reject a policy replacement
            # rather than execute a different definition under the same name.
            if self.controller.recovery_jobs is not registry:
                raise ValueError('Recovery policy changed during confirmation; review it again')
            submitted = self.controller.submit_recovery(self.workspace, job.name)
            self.job = submitted['job_id']
            self.refresh()
            self.status.set(f'{job.operation}: waiting for job {self.job}. Keep Workbench open.')
            self.timer = self.window.after(100, self.poll)
        except Exception as exc:
            self.status.set('Job not submitted: ' + str(exc))

    def poll(self):
        if self.timer:
            self.window.after_cancel(self.timer)
            self.timer = None
        if not self.job:
            return
        try:
            result = self.controller.job(self.job)
        except Exception as exc:
            self.status.set('Status unavailable; retain this job and recheck. Do not resubmit: ' + str(exc))
            return
        if result['status'] not in ('completed', 'failed', 'cancelled'):
            self.timer = self.window.after(250, self.poll)
            return
        self.job = None
        self.refresh()
        self.show(result)
        self.status.set(result['status'] + ': evidence retained. No rollback, hold release or reboot is implied. '
                        'Reconcile uncertain writes; never automatically retry a failed action.')

    def close(self):
        if self.job:
            self.status.set('Keep this panel open until the accepted job reaches a known terminal state.')
            return False
        if self.timer:
            self.window.after_cancel(self.timer)
        self.window.destroy()
        return True
