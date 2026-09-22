#!/usr/bin/env python3
"""Desktop editor and emulator console; Python/Tk on Linux, macOS and Windows."""
import argparse
import codecs
import json
from pathlib import Path
import re
import socket
import subprocess
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from uconsole_emulator import DEFAULT, ROOT, parser, command, qmp
from uconsole_agent import context_markdown, inspect as agent_inspect, load_tasks

ANSI = re.compile(r'\x1b\][^\x07]*(?:\x07)|\x1b\[[0-?]*[ -/]*[@-~]')


class Workbench:
    def __init__(self, root, workspace, qmp_port=4444, serial_port=4445):
        self.root, self.workspace = root, workspace.resolve()
        self.qmp_port, self.serial_port = qmp_port, serial_port
        self.process = self.serial = self.log = self.errors = None
        self.filename = None
        self.transfer = self.transfer_log = None
        self.shutdown_requested = False
        self.shutdown_output = ''
        self.decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        root.title('uConsole CM4 Workbench — partial hardware emulation')
        root.geometry('1100x760')
        toolbar = ttk.Frame(root, padding=6)
        toolbar.pack(fill='x')
        self.mode = tk.StringVar(value='maintenance')
        ttk.Combobox(toolbar, textvariable=self.mode, values=['maintenance', 'normal'], state='readonly', width=14).pack(side='left')
        self.display = tk.StringVar(value='none')
        ttk.Combobox(toolbar, textvariable=self.display, values=['none', 'gtk', 'sdl'],
                     state='readonly', width=7).pack(side='left', padx=2)
        for label, action in [('Start', self.start), ('Pause', lambda: self.control('stop')),
                              ('Resume', lambda: self.control('cont')), ('Power off', self.poweroff),
                              ('Export image', self.export), ('Open file', self.open_file), ('Save', self.save_file),
                              ('Copy to guest', self.copy_to_guest)]:
            ttk.Button(toolbar, text=label, command=lambda fn=action: self.guard(fn)).pack(side='left', padx=2)
        agentbar = ttk.Frame(root, padding=(6, 0, 6, 6))
        agentbar.pack(fill='x')
        for label, action in [('Copy selection', self.copy_selection), ('Copy boot log', self.copy_console),
                              ('Copy agent context', self.copy_context), ('Paste command', self.paste_command),
                              ('Save transcript', self.save_transcript), ('Guest files', self.guest_files),
                              ('Tasks', self.tasks)]:
            ttk.Button(agentbar, text=label, command=lambda fn=action: self.guard(fn)).pack(side='left', padx=2)
        ttk.Label(root, text='BCM2711 / 2 GiB • AXP221 PMIC • USB input substitutes • no DSI/GPU or full modem model', padding=6).pack(fill='x')
        panes = ttk.Panedwindow(root, orient='vertical')
        panes.pack(fill='both', expand=True)
        edit_frame = ttk.Labelframe(panes, text='Host source editor')
        self.editor = tk.Text(edit_frame, undo=True, wrap='none', font='TkFixedFont', height=12)
        self.editor.pack(fill='both', expand=True)
        panes.add(edit_frame, weight=1)
        console_frame = ttk.Labelframe(panes, text='Guest serial console (line input; not a terminal emulator)')
        self.console = tk.Text(console_frame, wrap='word', state='disabled', font='TkFixedFont', height=16, background='#151a20', foreground='#d8e4ef')
        self.console.pack(fill='both', expand=True)
        self.console.bind('<Control-c>', self.copy_selection)
        self.console.bind('<Command-c>', self.copy_selection)
        self.console.bind('<Button-3>', self.console_menu)
        self.editor.bind('<Button-3>', self.editor_menu)
        panes.add(console_frame, weight=2)
        self.entry = ttk.Entry(root)
        self.entry.pack(fill='x', padx=6, pady=5)
        self.entry.bind('<Return>', lambda event: self.guard(self.send))
        self.status = tk.StringVar(value=f'Stopped • {self.workspace}')
        ttk.Label(root, textvariable=self.status, padding=6).pack(fill='x')
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(100, self.poll)

    def clipboard(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update_idletasks()
        self.status.set(f'Copied {len(text):,} characters to the system clipboard')

    def copy_selection(self, event=None):
        try:
            widget = self.root.selection_own_get()
        except tk.TclError:
            widget = self.root.focus_get()
        if widget not in (self.console, self.editor):
            widget = self.console
        try:
            text = widget.get('sel.first', 'sel.last')
        except tk.TclError:
            raise ValueError('Select text in the editor or console first')
        self.clipboard(text)
        return 'break'

    def copy_console(self):
        self.clipboard(self.console.get('1.0', 'end-1c'))

    def copy_context(self):
        self.clipboard(context_markdown(agent_inspect(self.workspace, self.qmp_port, 120)))

    def paste_command(self):
        text = self.root.clipboard_get()
        # A line-oriented serial input intentionally does not accept embedded newlines.
        self.entry.delete(0, 'end')
        self.entry.insert(0, ' '.join(text.splitlines()))
        self.entry.focus_set()

    def console_menu(self, event):
        menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(label='Copy selection', command=lambda: self.guard(self.copy_selection))
        menu.add_command(label='Copy all boot output', command=self.copy_console)
        menu.add_command(label='Copy agent context', command=self.copy_context)
        menu.tk_popup(event.x_root, event.y_root)

    def editor_menu(self, event):
        self.editor.focus_set()
        menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(label='Cut', command=lambda: self.editor.event_generate('<<Cut>>'))
        menu.add_command(label='Copy', command=lambda: self.editor.event_generate('<<Copy>>'))
        menu.add_command(label='Paste', command=lambda: self.editor.event_generate('<<Paste>>'))
        menu.tk_popup(event.x_root, event.y_root)

    def save_transcript(self):
        name = filedialog.asksaveasfilename(parent=self.root, defaultextension='.txt',
                                            title='Save clean serial transcript')
        if name:
            target = Path(name)
            if target.exists():
                raise ValueError('Choose a new transcript path')
            target.write_text(self.console.get('1.0', 'end-1c'))
            self.status.set(f'Saved transcript to {target}')

    def release_serial(self):
        if self.serial:
            self.serial.close()
            self.serial = None

    def agent_command(self, arguments, timeout=60):
        if self.process is None or self.active_mode != 'maintenance':
            raise ValueError('This operation requires a running maintenance shell')
        self.release_serial()
        result = subprocess.run([sys.executable, str(ROOT / 'tools/uconsole_agent.py'),
                                 '--workspace', str(self.workspace), '--serial-port', str(self.serial_port),
                                 *arguments], text=True, capture_output=True, timeout=timeout)
        if result.returncode:
            raise ValueError(result.stderr.strip() or result.stdout.strip())
        return result.stdout

    def guest_files(self):
        window = tk.Toplevel(self.root)
        window.title('Guest files — maintenance shell')
        window.geometry('700x460')
        path = tk.StringVar(value='/')
        top = ttk.Frame(window, padding=6)
        top.pack(fill='x')
        ttk.Entry(top, textvariable=path).pack(side='left', fill='x', expand=True)
        tree = ttk.Treeview(window, columns=('type', 'bytes'), show='tree headings')
        tree.heading('#0', text='Name')
        tree.heading('type', text='Type')
        tree.heading('bytes', text='Bytes')
        tree.column('type', width=80, anchor='center')
        tree.column('bytes', width=120, anchor='e')
        tree.pack(fill='both', expand=True, padx=6)

        def refresh():
            data = json.loads(self.agent_command(['ls', path.get()]))
            tree.delete(*tree.get_children())
            for entry in data['entries']:
                tree.insert('', 'end', text=entry['name'], values=(entry['type'], entry['bytes']))

        def enter(event=None):
            selection = tree.selection()
            if not selection:
                return
            item = tree.item(selection[0])
            name, kind = item['text'], item['values'][0]
            guest = str(Path(path.get()) / name)
            if kind == 'd':
                path.set(guest)
                refresh()
            else:
                target = filedialog.asksaveasfilename(parent=window, initialfile=name)
                if target:
                    self.agent_command(['get', guest, target], timeout=180)
                    self.status.set(f'Copied {guest} to {target}')

        ttk.Button(top, text='Refresh', command=lambda: self.guard(refresh)).pack(side='left', padx=4)
        ttk.Button(top, text='Up', command=lambda: (path.set(str(Path(path.get()).parent)), self.guard(refresh))).pack(side='left')
        ttk.Button(window, text='Open folder / Download selected file', command=lambda: self.guard(enter)).pack(pady=6)
        tree.bind('<Double-1>', lambda event: self.guard(enter))
        refresh()

    def tasks(self):
        tasks = load_tasks(ROOT / 'uconsole-tasks.json')
        window = tk.Toplevel(self.root)
        window.title('Agent tasks')
        window.geometry('680x420')
        names = tk.Listbox(window, exportselection=False, height=7)
        for name in tasks:
            names.insert('end', name)
        names.pack(fill='x', padx=6, pady=6)
        output = tk.Text(window, wrap='word', font='TkFixedFont')
        output.pack(fill='both', expand=True, padx=6)

        def run_selected():
            selection = names.curselection()
            if not selection:
                raise ValueError('Choose a task')
            name = names.get(selection[0])
            task = tasks[name]
            if task['kind'] == 'guest':
                result = self.agent_command(['task', name], timeout=int(task.get('timeout', 60)) + 10)
            else:
                process = subprocess.run([sys.executable, str(ROOT / 'tools/uconsole_agent.py'),
                                          'task', name], text=True, capture_output=True)
                result = process.stdout + process.stderr
            output.delete('1.0', 'end')
            output.insert('1.0', result)
            self.status.set(f'Task {name} finished')

        ttk.Button(window, text='Run selected task', command=lambda: self.guard(run_selected)).pack(pady=6)

    def guard(self, action):
        try:
            action()
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            messagebox.showerror('uConsole Workbench', str(exc), parent=self.root)

    def append(self, text):
        self.console.configure(state='normal')
        self.console.insert('end', ANSI.sub('', text).replace('\r', ''))
        if int(self.console.index('end-1c').split('.')[0]) > 3000:
            self.console.delete('1.0', '1000.0')
        self.console.see('end')
        self.console.configure(state='disabled')

    def start(self):
        if self.process is not None:
            raise ValueError('This workbench already owns a running emulator')
        args = parser().parse_args(['--workspace', str(self.workspace), 'run',
                                    '--mode', self.mode.get(), '--serial-port', str(self.serial_port),
                                    '--qmp-port', str(self.qmp_port), '--display', self.display.get()])
        cmd = command(args)
        # Do not accidentally attach controls to another VM on these ports.
        for port in (self.qmp_port, self.serial_port):
            with socket.socket() as check:
                check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                check.bind(('127.0.0.1', port))
        self.active_mode = self.mode.get()
        self.shutdown_requested = False
        self.shutdown_output = ''
        self.decoder.reset()
        (self.workspace / 'serial.log').write_bytes(b'')
        self.errors = (self.workspace / 'workbench-qemu.log').open('wb')
        self.process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=self.errors, stderr=self.errors)
        self.log = None
        self.status.set(f'Booting {self.active_mode} • serial 127.0.0.1:{self.serial_port}')

    def control(self, operation):
        if self.process is None:
            raise ValueError('Start an emulator in this workbench first')
        qmp(self.qmp_port, operation)
        self.status.set('Paused' if operation == 'stop' else 'Running')

    def send(self):
        if self.serial is None:
            raise ValueError('Serial console is not connected yet')
        self.serial.sendall((self.entry.get() + '\n').encode())
        self.entry.delete(0, 'end')

    def poweroff(self):
        if self.process is None:
            return
        if self.serial is None:
            raise ValueError('Wait for the serial console to connect')
        if self.active_mode == 'maintenance':
            script = ('mountpoint -q /proc || mount -t proc proc /proc; '
                      'mountpoint -q /sys || mount -t sysfs sys /sys; '
                      "rootmm=$(awk '$5 == \"/\" {print $3; exit}' /proc/self/mountinfo); "
                      "rootdev=/dev/$(sed -n 's/^DEVNAME=//p' \"/sys/dev/block/$rootmm/uevent\"); "
                      'test -b "$rootdev" && sync && mount -o remount,ro "$rootdev" / '
                      "&& printf '\\nUCONSOLE_ROOT_READONLY\\n' || printf '\\nUCONSOLE_SHUTDOWN_FAILED\\n'\n")
            self.serial.sendall(script.encode())
            self.shutdown_requested = True
            self.shutdown_output = ''
            self.status.set('Waiting for root filesystem to become read-only')
        else:
            messagebox.showinfo('Clean shutdown', 'Log in and run sudo poweroff in the console. QEMU exits when the guest shuts down.', parent=self.root)

    def export(self):
        if self.process is not None:
            raise ValueError('Shut down and close the emulator before exporting')
        target = filedialog.asksaveasfilename(parent=self.root, defaultextension='.img', title='Export SD image to a new file')
        if target:
            # Conversion can take minutes; an independent process keeps Tk responsive.
            log = self.workspace / 'export.log'
            with log.open('wb') as stream:
                subprocess.Popen([sys.executable, str(ROOT / 'tools/uconsole_emulator.py'),
                                  '--workspace', str(self.workspace), 'export', target], stdout=stream, stderr=stream)
            self.status.set(f'Export started; result and checksum will be in {log}')

    def open_file(self):
        if self.editor.edit_modified() and not messagebox.askyesno('Unsaved edits', 'Discard unsaved editor changes?', parent=self.root):
            return
        name = filedialog.askopenfilename(parent=self.root, initialdir=ROOT)
        if name:
            text = Path(name).read_text()
            self.editor.delete('1.0', 'end')
            self.editor.insert('1.0', text)
            self.filename = Path(name)
            self.editor.edit_modified(False)
            self.status.set(str(self.filename))

    def save_file(self):
        if self.filename is None:
            name = filedialog.asksaveasfilename(parent=self.root, initialdir=ROOT)
            if not name:
                return
            self.filename = Path(name)
        self.filename.write_text(self.editor.get('1.0', 'end-1c'))
        self.editor.edit_modified(False)
        self.status.set(f'Saved {self.filename}')

    def copy_to_guest(self):
        if self.process is None or self.active_mode != 'maintenance' or self.transfer:
            raise ValueError('Start maintenance mode and wait for any previous copy to finish')
        self.save_file()
        if self.filename is None:
            return
        destination = simpledialog.askstring('Copy file into image', 'Guest absolute path (replaces existing file):',
                                             initialvalue='/root/' + self.filename.name, parent=self.root)
        if destination:
            if self.serial:
                self.serial.close()
                self.serial = None
            self.transfer_log = (self.workspace / 'transfer.log').open('wb')
            self.transfer = subprocess.Popen([sys.executable, str(ROOT / 'tools/uconsole_emulator.py'),
                                             'put', str(self.filename), destination, '--serial-port', str(self.serial_port)],
                                            stdout=self.transfer_log, stderr=self.transfer_log)
            self.status.set('Copying file into the guest image')

    def poll(self):
        if self.transfer and self.transfer.poll() is not None:
            code = self.transfer.returncode
            self.transfer_log.close()
            self.transfer = self.transfer_log = None
            self.status.set('Copy complete; checksum in transfer.log' if code == 0 else 'Copy failed; see transfer.log')
        if self.process is not None:
            if self.log is None and (self.workspace / 'serial.log').exists():
                self.log = (self.workspace / 'serial.log').open('rb')
            if self.log:
                text = self.decoder.decode(self.log.read(65536))
                self.append(text)
                if self.shutdown_requested:
                    self.shutdown_output = (self.shutdown_output + text)[-8192:]
                    if '\nUCONSOLE_ROOT_READONLY\r\n' in self.shutdown_output:
                        self.guard(lambda: qmp(self.qmp_port, 'quit'))
                        self.shutdown_requested = False
                    elif '\nUCONSOLE_SHUTDOWN_FAILED\r\n' in self.shutdown_output:
                        self.shutdown_requested = False
                        self.status.set('Shutdown failed; inspect the guest console. QEMU is still running.')
            if self.serial is None and self.process.poll() is None and self.transfer is None:
                try:
                    self.serial = socket.create_connection(('127.0.0.1', self.serial_port), timeout=0.1)
                    self.serial.setblocking(False)
                    self.status.set(f'Running {self.active_mode} • serial connected')
                except OSError:
                    pass
            if self.serial:
                try:
                    # Output is displayed from the complete on-disk serial log.
                    self.serial.recv(65536)
                except BlockingIOError:
                    pass
                except OSError:
                    self.serial.close()
                    self.serial = None
            if self.process.poll() is not None:
                code = self.process.returncode
                self.release()
                self.status.set(f'Stopped (exit {code}); see workbench-qemu.log')
        self.root.after(100, self.poll)

    def release(self):
        for resource in (self.serial, self.log, self.errors):
            if resource:
                resource.close()
        self.process = self.serial = self.log = self.errors = None

    def close(self):
        if self.transfer is not None:
            messagebox.showinfo('Copy in progress', 'Wait for the file copy to finish before closing.', parent=self.root)
            return
        if self.editor.edit_modified() and not messagebox.askyesno('Unsaved edits', 'Close and discard unsaved editor changes?', parent=self.root):
            return
        if self.process is not None:
            if not messagebox.askyesno('Stop QEMU', 'Has the guest shut down or remounted its root filesystem read-only?\n\nStopping QEMU now removes power from the virtual machine.', parent=self.root):
                return
            try:
                qmp(self.qmp_port, 'quit')
                self.process.wait(timeout=5)
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                messagebox.showerror('Could not stop QEMU', str(exc), parent=self.root)
                return
            self.release()
        self.root.destroy()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--workspace', type=Path, default=DEFAULT)
    p.add_argument('--qmp-port', type=int, default=4444)
    p.add_argument('--serial-port', type=int, default=4445)
    args = p.parse_args()
    root = tk.Tk()
    Workbench(root, args.workspace, args.qmp_port, args.serial_port)
    root.mainloop()


if __name__ == '__main__':
    main()
