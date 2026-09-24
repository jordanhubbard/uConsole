#!/usr/bin/env bash
# Cross-platform build/install/package driver for the uConsole Workbench.

set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
build_dir=${BUILD_DIR:-$root/build}
prefix=${PREFIX:-/usr/local}
destdir=${DESTDIR:-}

fail() {
    printf 'platform: %s\n' "$*" >&2
    exit 1
}

os=$(uname -s)
machine=$(uname -m)
case "$os:$machine" in
    Linux:x86_64|Linux:amd64) target=linux-x86_64 ;;
    Linux:aarch64|Linux:arm64) target=linux-aarch64 ;;
    Darwin:arm64) target=macos-arm64 ;;
    Darwin:x86_64) target=macos-x86_64 ;;
    *) fail "unsupported host: $os $machine" ;;
esac

stage=$build_dir/ide/$target/uconsole-workbench

install_deps() {
    if [[ $os == Linux ]]; then
        command -v apt-get >/dev/null 2>&1 || fail 'Linux dependency installation currently requires apt-get'
        sudo apt-get update
        sudo apt-get install -y \
            build-essential ca-certificates curl dfu-util libcap-ng-dev libglib2.0-dev \
            libgtk-3-dev libpixman-1-dev libslirp-dev ninja-build patch pkg-config \
            python3 python3-tk qemu-system-arm qemu-utils shellcheck tar unzip xvfb xz-utils
        if ! command -v arduino-cli >/dev/null 2>&1; then
            temp=$(mktemp -d)
            trap 'rm -rf -- "$temp"' EXIT
            curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh \
                | BINDIR="$temp" sh
            sudo install -m 0755 "$temp/arduino-cli" /usr/local/bin/arduino-cli
        fi
    else
        command -v brew >/dev/null 2>&1 || fail 'Homebrew is required; install it from https://brew.sh/'
        brew install arduino-cli dfu-util ninja pkg-config python-tk qemu shellcheck
    fi

    # The legacy STM32 core is needed for firmware builds. Some ARM hosts need
    # the manual toolchain procedure documented in Code/uconsole_keyboard/README.md.
    if ! arduino-cli core list 2>/dev/null | grep -q '^stm32duino:STM32F1[[:space:]]'; then
        arduino-cli config init --overwrite
        arduino-cli config add board_manager.additional_urls \
            https://dan.drown.org/stm32duino/package_STM32duino_index.json
        arduino-cli core update-index
        arduino-cli core install stm32duino:STM32F1@2021.2.22 || fail \
            'STM32F1 core install failed; follow the architecture-specific manual setup in Code/uconsole_keyboard/README.md'
    fi
    find_tk_python >/dev/null || fail 'dependencies installed, but no Python 3 interpreter can import tkinter'
}

find_tk_python() {
    local candidate resolved
    for candidate in "${PYTHON:-}" python3 /usr/bin/python3 /opt/homebrew/bin/python3; do
        [[ -n $candidate ]] || continue
        resolved=$(command -v "$candidate" 2>/dev/null || true)
        [[ -n $resolved ]] || continue
        if "$resolved" -c 'import tkinter' >/dev/null 2>&1; then
            printf '%s\n' "$resolved"
            return 0
        fi
    done
    return 1
}

write_launcher() {
    mkdir -p "$stage/bin"
    cat > "$stage/bin/uconsole-workbench" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
prefix=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
export UCONSOLE_ROOT="$prefix/libexec/uconsole-workbench"
export UCONSOLE_BUILD_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/uconsole-workbench"
for candidate in "${PYTHON:-}" python3 /usr/bin/python3 /opt/homebrew/bin/python3; do
    [[ -n $candidate ]] || continue
    resolved=$(command -v "$candidate" 2>/dev/null || true)
    if [[ -n $resolved ]] && "$resolved" -c 'import tkinter' >/dev/null 2>&1; then
        exec "$resolved" "$UCONSOLE_ROOT/tools/uconsole_workbench.py" "$@"
    fi
done
printf 'uconsole-workbench: no Python 3 interpreter with tkinter found; run make deps\n' >&2
exit 1
EOF
    chmod 0755 "$stage/bin/uconsole-workbench"
}

build_ide() {
    local -a compile
    command -v python3 >/dev/null 2>&1 || fail 'python3 is required; run make deps'
    command -v cc >/dev/null 2>&1 || fail 'a C compiler is required; run make deps'
    python3 -m compileall -q "$root/tools"
    rm -rf -- "$stage"
    mkdir -p "$stage/libexec/uconsole-workbench/tools" \
        "$stage/libexec/uconsole-workbench/Code/patch/qemu" \
        "$stage/share/doc/uconsole-workbench" \
        "$stage/share/uconsole-keyboard-flash"
    cp "$root"/tools/*.py "$stage/libexec/uconsole-workbench/tools/"
    cp "$root"/Code/patch/qemu/*.patch "$stage/libexec/uconsole-workbench/Code/patch/qemu/"
    cp "$root/uconsole-tasks.json" "$stage/libexec/uconsole-workbench/"
    cp "$root/docs/emulator.md" "$root/README.md" "$stage/share/doc/uconsole-workbench/"
    compile=(cc)
    if [[ -n ${CPPFLAGS:-} ]]; then
        read -r -a flags <<< "$CPPFLAGS"
        compile+=("${flags[@]}")
    fi
    read -r -a flags <<< "${CFLAGS:--O2 -Wall -Wextra}"
    compile+=("${flags[@]}" "$root/Bin/uconsole_keyboard_flash/upload-reset/upload-reset.c")
    if [[ -n ${LDFLAGS:-} ]]; then
        read -r -a flags <<< "$LDFLAGS"
        compile+=("${flags[@]}")
    fi
    if [[ -n ${LDLIBS:-} ]]; then
        read -r -a flags <<< "$LDLIBS"
        compile+=("${flags[@]}")
    fi
    compile+=(-o "$build_dir/upload-reset.elf")
    "${compile[@]}"
    write_launcher
    printf 'Built uConsole Workbench for %s in %s\n' "$target" "$stage"
}

package_ide() {
    build_ide
    firmware=$build_dir/firmware/uconsole_keyboard.ino.bin
    if [[ ! -s $firmware ]]; then
        make -C "$root" firmware BUILD_DIR="$build_dir"
    fi
    flash_archive=$(python3 "$root/tools/package_flash.py" "$build_dir" | tail -1)
    temp=$(mktemp -d)
    trap 'rm -rf -- "$temp"' EXIT
    tar -xzf "$flash_archive" -C "$temp"
    cp -R "$temp/uconsole_keyboard_flash/." "$stage/share/uconsole-keyboard-flash/"
    output=$build_dir/uconsole-workbench-$target.tar.gz
    tar -czf "$output" -C "$(dirname "$stage")" "$(basename "$stage")"
    printf '%s\n' "$output"
}

install_ide() {
    build_ide
    install -d "$destdir$prefix/bin" "$destdir$prefix/libexec" "$destdir$prefix/share"
    cp -R "$stage/libexec/uconsole-workbench" "$destdir$prefix/libexec/"
    cp -R "$stage/share/doc" "$destdir$prefix/share/"
    cp -R "$stage/share/uconsole-keyboard-flash" "$destdir$prefix/share/"
    install -m 0755 "$stage/bin/uconsole-workbench" "$destdir$prefix/bin/uconsole-workbench"
    printf 'Installed uConsole Workbench under %s%s\n' "$destdir" "$prefix"
}

case ${1:-} in
    target) printf '%s\n' "$target" ;;
    deps) install_deps ;;
    build) build_ide ;;
    run)
        python=$(find_tk_python) || fail 'no Python 3 interpreter with tkinter found; run make deps'
        export UCONSOLE_ROOT=$root
        export UCONSOLE_BUILD_DIR=$build_dir
        exec "$python" "$root/tools/uconsole_workbench.py" "${@:2}"
        ;;
    install) install_ide ;;
    package) package_ide ;;
    *) fail 'usage: scripts/platform.sh {target|deps|build|run|install|package}' ;;
esac
