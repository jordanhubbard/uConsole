#!/usr/bin/env python3
"""Record interactive desktop acceptance through a real Workbench instance.

Run under a graphical session (or Xvfb). Send JSON lines on stdin: status,
capture, key, type, type-secret, move, click, firmware, firmware-keys,
serial, serial-secret, start, finish. The
record documents actions, not an automatic declaration that onboarding passed.
Only the Workbench-owned VM is controlled. EOF/failure force-stops that VM;
use only a disposable test workspace. Password fixtures stay in a private file.
"""
import argparse
import json
import os
from pathlib import Path
import queue
import re
import secrets
import sys
import threading
import time
import tkinter as tk
import uuid

from uconsole_workbench import Workbench
from forge_keyboard import commands_snapshot
from forge_keyboard_host import HostKeys, contacts
from uconsole_emulator import native_display


def firmware_chord(keys):
    if not isinstance(keys, list) or not 1 <= len(keys) <= 8 or any(
            not isinstance(key, str) or not contacts(key) for key in keys):
        raise ValueError('firmware-keys requires 1..8 supported host keysyms')
    mapper, commands = HostKeys(), []
    for index, key in enumerate(keys):
        commands.extend(mapper.change(index, key)[0])
    for index in reversed(range(len(keys))):
        commands.extend(mapper.change(index, keys[index], release=True)[0])
    return commands_snapshot(commands)


def text_keys(text):
    if not isinstance(text, str) or not re.fullmatch('[a-z0-9 ]{1,128}', text):
        raise ValueError('type accepts 1-128 lowercase letters, digits and spaces')
    return ['spc' if character == ' ' else character for character in text]


def pointer_events(dx, dy):
    if any(type(value) is not int or not -1024 <= value <= 1024 for value in (dx, dy)):
        raise ValueError('move requires integer dx/dy in -1024..1024')
    return [{'type': 'rel', 'data': {'axis': axis, 'value': value}}
            for axis, value in (('x', dx), ('y', dy))]


def load_secret(path):
    if path.is_symlink() or path.stat().st_mode & 0o077 or path.stat().st_size > 4096:
        raise ValueError('Reuse only a small private recorder secrets file, not a symlink')
    value = json.loads(path.read_text()).get('onboarding_password')
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{24}', value):
        raise ValueError('Invalid recorder password fixture')
    return value


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--workspace', type=Path, required=True)
    cli.add_argument('--secrets', type=Path, help='reuse a previous private recorder password fixture')
    cli.add_argument('--keyboard', choices=('generic', 'composite'), default='generic')
    options = cli.parse_args()
    workspace = options.workspace.resolve()
    directory = workspace / ('desktop-record-' + uuid.uuid4().hex)
    directory.mkdir(mode=0o700)
    secret = load_secret(options.secrets) if options.secrets else secrets.token_hex(12)
    with os.fdopen(os.open(directory / 'secrets.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                           0o600), 'w') as output:
        json.dump({'onboarding_password': secret}, output)
    root = tk.Tk()
    app = Workbench(root, workspace)
    events = []
    inbox = queue.Queue(maxsize=16)
    started = time.monotonic()
    typing = False
    finished = False

    def respond(event):
        event['elapsed_seconds'] = round(time.monotonic() - started, 3)
        events.append(event)
        print(json.dumps(event), flush=True)

    def read_commands():
        while True:
            line = sys.stdin.readline(4097)
            if not line:
                inbox.put({'action': 'eof'})
                return
            if len(line) > 4096 or not line.endswith('\n'):
                inbox.put({'action': 'invalid', 'reason': 'oversized or incomplete input'})
                return
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError('object required')
                inbox.put(request)
            except (ValueError, TypeError) as exc:
                inbox.put({'action': 'invalid', 'reason': str(exc)})

    def send_keys(keys):
        if not isinstance(keys, list) or not 1 <= len(keys) <= 8:
            raise ValueError('key requires a list of 1-8 QEMU qcodes')
        app.runtime.control('send-key', {'keys': [{'type': 'qcode', 'data': key} for key in keys],
                                         'hold-time': 60})

    def type_sequence(keys, action):
        nonlocal typing
        typing = True
        sequence = iter(keys)
        def next_key():
            nonlocal typing
            try:
                key = next(sequence)
                send_keys([key])
                root.after(130, next_key)
            except StopIteration:
                typing = False
                respond({'action': action, 'completed': True, 'characters': len(keys)})
            except Exception as exc:
                typing = False
                respond({'action': action, 'error': str(exc)})
        next_key()

    def dispatch(request):
        nonlocal finished, typing
        action = request.get('action')
        if typing and action not in ('status', 'capture'):
            raise ValueError('Wait for the current input sequence to finish')
        if options.keyboard == 'composite' and action in ('key', 'move', 'click', 'type', 'type-secret'):
            raise ValueError('Composite recording requires firmware/firmware-keys input; generic QEMU input is not evidence')
        if action == 'status':
            respond({'action': action, 'status': app.status.get(),
                     'running': app.process is not None and app.process.poll() is None,
                     'setup_running': app.display_setup is not None,
                     'console_tail': app.console.get('1.0', 'end')[-2400:]})
        elif action == 'capture':
            target = directory / ('screen-' + uuid.uuid4().hex + '.png')
            app.runtime.control('screendump', {'filename': str(target), 'format': 'png'})
            respond({'action': action, 'path': str(target)})
        elif action in ('firmware', 'firmware-keys'):
            if options.keyboard != 'composite':
                raise ValueError('Firmware actions require --keyboard composite')
            commands = (firmware_chord(request.get('keys')) if action == 'firmware-keys'
                        else commands_snapshot(request.get('commands')))
            submitted = app.job_controller().submit_keyboard('gui', commands)
            typing = True

            def poll_firmware():
                nonlocal typing
                result = app.controller.job(submitted['job_id'])
                if result['status'] not in ('completed', 'failed', 'cancelled'):
                    root.after(25, poll_firmware)
                    return
                typing = False
                respond({'action': action, 'job': result})

            root.after(25, poll_firmware)
        elif action == 'key':
            send_keys(request.get('keys'))
            respond({'action': action, 'keys': request['keys']})
        elif action == 'move':
            events = pointer_events(request.get('dx'), request.get('dy'))
            app.runtime.control('input-send-event', {'events': events})
            respond({'action': action, 'dx': request['dx'], 'dy': request['dy']})
        elif action == 'click':
            button = request.get('button', 'left')
            if button not in ('left', 'middle', 'right'):
                raise ValueError('click requires left, middle or right')
            app.runtime.control('input-send-event', {'events': [
                {'type': 'btn', 'data': {'button': button, 'down': True}}]})
            typing = True
            def release_button():
                nonlocal typing
                try:
                    app.runtime.control('input-send-event', {'events': [
                        {'type': 'btn', 'data': {'button': button, 'down': False}}]})
                    respond({'action': action, 'button': button})
                except Exception as exc:
                    respond({'action': action, 'error': str(exc)})
                finally:
                    typing = False
            root.after(80, release_button)
        elif action in ('type', 'type-secret'):
            text = secret if action == 'type-secret' else request.get('text')
            type_sequence(text_keys(text), action)
        elif action in ('serial', 'serial-secret'):
            text = secret if action == 'serial-secret' else request.get('text')
            if not isinstance(text, str) or len(text) > 2048 or '\n' in text or '\r' in text:
                raise ValueError('serial requires one line of at most 2048 characters')
            app.entry.delete(0, 'end')
            app.entry.insert(0, text)
            app.send()
            respond({'action': action, **({'text': text} if action == 'serial' else {})})
        elif action == 'start':
            app.start()
            respond({'action': action, 'status': app.status.get()})
        elif action == 'finish':
            if app.process is not None or app.display_setup is not None:
                raise ValueError('Shut down the guest and wait for Workbench to release it first')
            finished = True
            root.quit()
        elif action == 'eof':
            root.quit()
        else:
            raise ValueError('Unknown or invalid recorder action')

    def poll():
        try:
            request = inbox.get_nowait()
        except queue.Empty:
            pass
        else:
            try:
                dispatch(request)
            except Exception as exc:
                respond({'action': request.get('action'), 'error': str(exc)})
        root.after(50, poll)

    app.mode.set('desktop')
    app.display.set(native_display())
    app.keyboard.set(options.keyboard)
    threading.Thread(target=read_commands, daemon=True).start()
    root.after(50, poll)
    forced = False
    try:
        app.start()
        respond({'action': 'ready', 'evidence_directory': str(directory)})
        root.mainloop()
    finally:
        try:
            try:
                console = app.console.get('1.0', 'end')
            except tk.TclError:
                # A window-manager close can destroy widgets before finally.
                # Losing UI text must not bypass cleanup of the owned guest.
                console = '[Workbench widgets closed before console capture]\n'
            try:
                (directory / 'console.log').write_text(console)
            except OSError as exc:
                respond({'action': 'console-save', 'error': str(exc)})
            if app.runtime and app.runtime.process is not None and app.runtime.process.poll() is None:
                forced = True
                app.runtime.stop(force=True)
            if app.controller:
                for job_id in (app.boot_job, app.lifecycle):
                    if job_id:
                        app.controller.cancel(job_id)
                        forced = True
                app.controller.close()
        finally:
            record = {'status': 'recorded' if finished else 'interrupted',
                      'forced_cleanup': forced, 'events': events,
                      'automated_desktop_pass': False}
            (directory / 'record.json').write_text(json.dumps(record, indent=2) + '\n')
            try:
                root.destroy()
            except tk.TclError:
                pass


if __name__ == '__main__':
    main()
