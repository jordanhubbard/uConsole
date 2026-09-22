#!/usr/bin/env python3
"""Capture a comparable, credential-free uConsole hardware inventory."""
import argparse
import json
from pathlib import Path
import shlex
import subprocess
import sys


PROBES = {
    'kernel': ['uname', '-a'],
    'model': ['sh', '-c', "tr -d '\\000' </proc/device-tree/model"],
    'cpu': ['lscpu'],
    'usb': ['lsusb', '-v'],
    'input': ['sh', '-c', 'cat /proc/bus/input/devices'],
    'network': ['ip', '-details', 'link'],
    'power_supply': ['sh', '-c',
        "for d in /sys/class/power_supply/*; do test -d \"$d\" || continue; echo ===$d; "
        "for f in type status capacity voltage_now current_now online; do test -r \"$d/$f\" && echo \"$f=$(cat \"$d/$f\")\"; done; done"],
    'gpio': ['gpioinfo'],
    'i2c': ['sh', '-c',
        "for b in /sys/class/i2c-adapter/i2c-*; do test -e \"$b\" || continue; "
        "n=${b##*-}; echo ===i2c-$n; i2cdetect -y \"$n\"; done"],
    'modules': ['lsmod'],
    'cmdline': ['sh', '-c', 'cat /proc/cmdline'],
}


def run_command(command, ssh):
    argv = ['ssh', '-o', 'BatchMode=yes', '--', ssh, shlex.join(command)] if ssh else command
    try:
        process = subprocess.run(argv, text=True, capture_output=True, timeout=45)
        return {'argv': command, 'exit_code': process.returncode,
                'stdout': process.stdout, 'stderr': process.stderr}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {'argv': command, 'exit_code': None, 'stdout': '', 'stderr': str(exc)}


def capture(ssh=None):
    return {'schema': 1, 'target': ssh or 'local',
            'probes': {name: run_command(argv, ssh) for name, argv in PROBES.items()}}


def compare(reference, candidate):
    differences = []
    for name in sorted(set(reference['probes']) | set(candidate['probes'])):
        left = reference['probes'].get(name, {})
        right = candidate['probes'].get(name, {})
        if left.get('stdout') != right.get('stdout') or left.get('exit_code') != right.get('exit_code'):
            differences.append({'probe': name,
                'reference_exit': left.get('exit_code'), 'candidate_exit': right.get('exit_code'),
                'reference_lines': len(left.get('stdout', '').splitlines()),
                'candidate_lines': len(right.get('stdout', '').splitlines())})
    return {'schema': 1, 'reference': reference.get('target'), 'candidate': candidate.get('target'),
            'matching': not differences, 'differences': differences}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    collect = sub.add_parser('capture')
    collect.add_argument('output', type=Path)
    collect.add_argument('--ssh', help='SSH target; authentication must already be configured')
    diff = sub.add_parser('compare')
    diff.add_argument('reference', type=Path)
    diff.add_argument('candidate', type=Path)
    args = parser.parse_args()
    try:
        if args.action == 'capture':
            if args.output.exists():
                raise FileExistsError(args.output)
            result = capture(args.ssh)
            args.output.write_text(json.dumps(result, indent=2) + '\n')
        else:
            result = compare(json.loads(args.reference.read_text()), json.loads(args.candidate.read_text()))
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({'error': str(exc)}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
