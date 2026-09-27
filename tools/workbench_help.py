"""Offline, searchable Tk user guide and keyboard-accessible contextual hints."""
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText


TOPICS = {
    'Getting started': """The Workbench is an image forge: edit and test a CM4 guest image, then take that image to real hardware. The bundled keyboard firmware is separate from the Linux guest image.

Before importing, see QEMU setup: the package contains the patched emulator builder, not a prebuilt emulator or a Linux guest image.

1. Use Image → Import guest image to select a supported CM4 .img or .img.bz2. Supply an independently verified SHA-256 for a custom image. The official compressed image uses the pinned checksum when this field is blank.
2. Preparation creates a private writable workspace; it never replaces an existing directory. Keep the entire workspace together, including its backing image. Allow at least 15–20 GiB, and more for exports and backups.
3. Choose maintenance, normal or desktop, then Start. Choose a display backend before Start if you want a graphical window; “none” deliberately runs without one.
4. Shut down the guest cleanly before exporting or changing its image.

Already have a workspace? Launch uconsole-workbench --workspace /absolute/path/to/workspace. It must contain machine.json and its referenced image and boot files. Do not create machine.json by hand or delete an existing workspace to silence an error.

Hover over controls for hints. Tab to a control and press F1 for its guide topic. This guide works offline; search matches both topic titles and their contents.""",
    'QEMU setup': """Emulation requires the Workbench's patched QEMU, not just a generic system QEMU installation. The package includes its builder and patch sources. The builder downloads the pinned QEMU release, checks its SHA-256, applies the patches, and compiles into your user-owned build directory. It does not replace the system QEMU. This operation requires a network connection and build tools; the guide itself is offline.

Linux prerequisites include a C/C++ compiler, Ninja, pkg-config, patch, GLib, Pixman and libslirp development packages. Install GTK development libraries for a GTK display window. On Ubuntu these include build-essential, ninja-build, pkg-config, patch, libglib2.0-dev, libpixman-1-dev, libslirp-dev and libgtk-3-dev. macOS requires the command-line developer tools and corresponding Homebrew dependencies. Python 3.12+ and Tk are required for Workbench. Image import/export is supported on Linux/macOS, not Windows.

From a source checkout, make deps installs platform prerequisites and make emulator-build builds QEMU. For an installed package, run the installation-specific command below in your host terminal after installing prerequisites. Review it before running; it may take several minutes. Build errors appear in that terminal. Keep the selected build and workspace backing files in place.

Obtain a supported CM4 Linux image separately. The built-in checksum is for the official compressed uConsole CM4 v3.1 64-bit image. For a raw or modified image, supply its independently verified expected SHA-256 during import. A checksum match establishes file identity, not trust in the image author.""",
    'Boot and display': """Start boots the selected workspace, not the keyboard firmware shown in the editor.

Maintenance is for controlled guest maintenance. Normal starts the regular system. Desktop prepares the overlay for the surrogate display when needed, then boots it. This preparation changes the writable overlay; checkpoint important work first.

Display “none” is headless. Select an available host backend for a QEMU display window (for example GTK/SDL on Linux, Cocoa on macOS). Host support depends on the packaged QEMU build. Changing the selector affects the next boot.

Pause suspends the VM; Resume continues it. Neither shuts down the guest. Power off asks the guest to shut down. Forcing QEMU to stop is equivalent to removing power and can corrupt guest files.

The display is a 1280×720 mailbox-framebuffer surrogate, not native DSI/VC4/GPU emulation. A successful desktop boot is not proof of real display-driver or hardware compatibility.""",
    'Editor and guest files': """The upper editor edits HOST files. Open file selects a host file; Save (Ctrl-S or Command-S) saves the editor contents on the host. Opening a file is not deployment to the guest.

Copy to guest transfers the saved file into the running guest. Guest files browses and transfers through the owned guest connection. Wait for transfers and guest commands to finish before starting another operation.

The lower pane is a serial transcript, not a full terminal emulator. Enter a line in the entry below it and press Return to send it. Paste command lets you review input. Copy selection, Copy boot log and Save transcript provide text evidence without screenshots. Transcripts can contain guest secrets: review before sharing.

The bundled keyboard project is copied to a user-owned location. Your edited project is preserved when bundled source changes. Building keyboard firmware and modifying a Linux guest image are different workflows.""",
    'Images and checkpoints': """Import creates a new workspace from a supported CM4 image after checksum verification. A custom image must retain the supported partition layout and boot filenames. Import does not flash a physical device.

Stop the emulator before image operations. Image → Create checkpoint records a named recovery point. Restore checkpoint changes the writable image back to that point; review its confirmation carefully. Recover interrupted restore reconciles a previously interrupted image transaction. Refresh boot files extracts updated boot assets after changes inside the guest.

Export image produces a new raw SD-card image suitable for subsequent hardware qualification. An emulator boot alone does not qualify that image on hardware. Keep the source image, final export and validation evidence.

Cancel image job requests cancellation, not rollback. Wait for cleanup and inspect the recorded outcome before retrying. Never move an overlay separately from its backing image.""",
    'Physical target and recovery': """Physical target and Recovery jobs operate on real hardware and require explicitly reviewed policies and their approved SHA-256 values at startup. Opening a panel does not grant permission to write a card or reboot.

The normal live-development workflow is SSH host/target with backup and restore of the existing card. A spare card or external reader is an optional deployment route, not a prerequisite.

Preserve a verified full backup before changing a device. Recovery preparation, approval, execution and reconciliation are distinct stages. Read each panel's current evidence and approval requirements. Never automatically retry an uncertain write or infer success merely because SSH returned.

Cancellation does not reverse completed writes. Retain failed journals, hashes and receipts. An independent reconciliation and normal-boot check are required before declaring a restore complete. Do not repair an original filesystem silently as part of backup/restore.""",
    'Power and device controls': """Next boot input selects generic USB input or the composite keyboard surrogate. Keyboard deck drives the owned emulated input bridge, not a physical keyboard.

Audio controls use USB audio surrogates: playback capture and synthetic input, not the carrier's native jack or a host microphone. Modem controls are a surrogate, not real cellular connectivity. Select these devices before boot.

ADC reference “missing” describes the guest supply reference, not converter power. These substitutes test guest behavior but do not establish electrical or native-device fidelity.

Load power profile captures an immutable initial power-state snapshot for the next boot. Clear profile restores defaults. Live power → Apply field changes one supported property; Read state queries it. Values are true/false or integers as indicated by the property. Key events can shut down the guest.

Run power schedule replays host-clock events. Cancel replay stops future events; completed events are not rolled back. Use checkpoints or disposable workspaces for fault injection.""",
    'Tasks and coding agents': """Tasks lists guest and host task definitions. A repository task file is not permission to execute arbitrary host commands. Host tasks require an owner-reviewed policy and its approved checksum. Guest tasks and transfers share the owned guest channel.

Copy agent context creates a text description you can give to an external coding CLI. Review it before sharing. The Workbench does not embed another assistant.

Agent attachment is opt-in via --agent-socket, --agent-allow and, when needed, --agent-files-root. Grants and approved policies constrain operations; an attachment never bypasses confirmation or transaction safety. Keep private sockets, policies, credentials and raw job data private.

Cancel host task, Cancel guest command, Cancel boot and Cancel transfer request cancellation of their respective jobs. Wait for cleanup. Cancellation is not proof that no effects occurred.""",
    'Errors and diagnostics': """Errors are recorded as timestamped JSON lines with severity, event, operation and workspace context. Help → Diagnostics shows the log path and lets you copy recent records. Error windows have selectable details and a Copy details button.

If Start reports missing machine.json, no guest workspace has been prepared at the selected path. Use Image → Import guest image, or restart with --workspace pointing at an existing prepared workspace. Installing keyboard firmware does not install a Linux guest image.

For boot failures, also inspect the workspace serial.log and workbench-qemu.log. Application errors, guest serial output and durable job history serve different purposes. Keep the job ID and original evidence when reporting a failure.

Logs and transcripts may include paths or error text from external commands. Review before sharing; do not post credentials, policies, disk images or full private job records. No diagnostic data is uploaded automatically.""",
}

# Text labels are shared by the actual controls; unknown controls retain default F1 help.
GROUPS = {
    'Boot and display': ('Start', 'Pause', 'Resume', 'Power off', 'Cancel boot'),
    'Editor and guest files': ('Open file', 'Save', 'Copy to guest', 'Guest files', 'Copy selection', 'Copy boot log', 'Paste command', 'Save transcript', 'Cancel transfer'),
    'Images and checkpoints': ('Image', 'Export image'),
    'Physical target and recovery': ('Physical target', 'Recovery jobs'),
    'Power and device controls': ('Keyboard deck', 'Audio controls', 'Modem controls', 'Load power profile', 'Clear profile', 'Apply field', 'Read state', 'Run power schedule', 'Cancel replay'),
    'Tasks and coding agents': ('Tasks', 'Copy agent context', 'Cancel guest command', 'Cancel host task'),
}
HINTS = {
    'Start': 'Boot a prepared Linux guest workspace. Import an image first; F1 for setup.',
    'Power off': 'Request guest shutdown. Forced stopping can corrupt the image.',
    'Save': 'Save the host source file; this does not deploy it to the guest.',
    'Copy to guest': 'Transfer the saved host file into the running guest.',
    'Export image': 'Export a stopped workspace to a new raw SD image.',
    'Physical target': 'Reviewed SSH host/target operations. Back up the existing card first.',
    'Recovery jobs': 'Prepare, review and reconcile real-device backup/restore transactions.',
}


class Tooltip:
    def __init__(self, widget, text):
        self.widget, self.text = widget, text
        self.timer = self.window = None
        widget.bind('<Enter>', self.schedule, add='+')
        widget.bind('<Leave>', self.hide, add='+')
        widget.bind('<FocusOut>', self.hide, add='+')
        widget.bind('<ButtonPress>', self.hide, add='+')
        widget.bind('<Destroy>', self.hide, add='+')
        widget.bind('<Escape>', self.hide, add='+')

    def schedule(self, event=None):
        self.hide()
        self.timer = self.widget.after(600, self.show)

    def show(self):
        self.timer = None
        if not self.widget.winfo_exists():
            return
        self.window = tk.Toplevel(self.widget)
        self.window.wm_overrideredirect(True)
        ttk.Label(self.window, text=self.text, padding=8, wraplength=360,
                  relief='solid').pack()
        self.window.update_idletasks()
        x = min(self.widget.winfo_rootx(), self.widget.winfo_screenwidth() - self.window.winfo_reqwidth())
        y = min(self.widget.winfo_rooty() + self.widget.winfo_height() + 4,
                self.widget.winfo_screenheight() - self.window.winfo_reqheight())
        self.window.geometry(f'+{max(0, x)}+{max(0, y)}')

    def hide(self, event=None):
        if self.timer is not None:
            self.widget.after_cancel(self.timer)
            self.timer = None
        if self.window is not None:
            self.window.destroy()
            self.window = None


class Guide:
    def __init__(self, parent, topic='Getting started', setup_command=None):
        self.content = dict(TOPICS)
        if setup_command:
            self.content['QEMU setup'] += '\n\nInstalled builder command:\n\n' + setup_command
        self.window = tk.Toplevel(parent)
        self.window.title('uConsole Workbench — User guide')
        self.window.geometry('900x650')
        self.window.minsize(620, 420)
        bar = ttk.Frame(self.window, padding=10)
        bar.pack(fill='x')
        ttk.Label(bar, text='Search guide:').pack(side='left')
        self.query = tk.StringVar()
        self.search = ttk.Entry(bar, textvariable=self.query)
        self.search.pack(side='left', fill='x', expand=True, padx=8)
        ttk.Button(bar, text='Clear', command=lambda: self.query.set('')).pack(side='left')
        ttk.Button(bar, text='Copy topic', command=self.copy_topic).pack(side='left', padx=6)
        panes = ttk.Panedwindow(self.window, orient='horizontal')
        panes.pack(fill='both', expand=True, padx=10, pady=(0, 10))
        self.topics = tk.Listbox(panes, exportselection=False, width=28)
        panes.add(self.topics, weight=1)
        self.body = ScrolledText(panes, wrap='word', padx=18, pady=16,
                                 font='TkDefaultFont', state='disabled')
        self.body.tag_configure('title', font=('TkDefaultFont', 18, 'bold'), spacing3=18)
        self.body.tag_configure('body', spacing1=4, spacing3=8)
        self.body.tag_configure('match', background='#fff2a8', foreground='#111111')
        panes.add(self.body, weight=4)
        self.topics.bind('<<ListboxSelect>>', self.select)
        self.query.trace_add('write', self.filter)
        self.window.bind('<Escape>', lambda event: self.window.destroy())
        self.filter()
        self.show(topic)

    def filter(self, *args):
        query = self.query.get().casefold().strip()
        self.matches = [title for title, body in self.content.items() if query in (title + '\n' + body).casefold()]
        self.topics.delete(0, 'end')
        for title in self.matches:
            self.topics.insert('end', title)
        self.show(self.matches[0] if self.matches else None)

    def show(self, topic):
        self.body.configure(state='normal')
        self.body.delete('1.0', 'end')
        self.body.insert('end', (topic or 'No matching topics') + '\n', 'title')
        self.body.insert('end', self.content.get(topic, 'Try a different search or choose Clear.'), 'body')
        query = self.query.get().strip()
        if query:
            start = '1.0'
            while True:
                start = self.body.search(query, start, stopindex='end', nocase=True)
                if not start:
                    break
                end = f'{start}+{len(query)}c'
                self.body.tag_add('match', start, end)
                start = end
        self.body.configure(state='disabled')
        self.topics.selection_clear(0, 'end')
        if topic in self.matches:
            index = self.matches.index(topic)
            self.topics.selection_set(index)
            self.topics.see(index)

    def select(self, event=None):
        selected = self.topics.curselection()
        if selected:
            self.show(self.matches[selected[0]])

    def copy_topic(self):
        self.window.clipboard_clear()
        self.window.clipboard_append(self.body.get('1.0', 'end-1c'))


def attach_hints(parent, show_help):
    hints = []
    for widget in parent.winfo_children():
        hints.extend(attach_hints(widget, show_help))
        label = str(widget.cget('text')) if 'text' in widget.keys() else ''
        for topic, labels in GROUPS.items():
            if label in labels:
                hints.append(Tooltip(widget, HINTS.get(label, f'{label} — see {topic}. Press F1 for instructions and safety notes.')))
                widget.bind('<F1>', lambda event, name=topic: (show_help(name), 'break')[1])
    return hints
