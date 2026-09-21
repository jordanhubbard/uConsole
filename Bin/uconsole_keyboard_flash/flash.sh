#!/bin/bash
set -euo pipefail
DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if (( $# > 2 )); then
    echo "Usage: $0 [firmware.bin] [serial_port]" >&2
    exit 2
fi
firmware=${1:-$DIR/uconsole_keyboard.ino.bin}
serial_port=${2:-ttyACM0}
if (( EUID == 0 )); then
    exec "$DIR/maple_upload" "$serial_port" 2 1EAF:0003 "$firmware"
else
    exec sudo "$DIR/maple_upload" "$serial_port" 2 1EAF:0003 "$firmware"
fi
