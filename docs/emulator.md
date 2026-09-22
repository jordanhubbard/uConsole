# CM4 emulator and development workbench

This repository now has a **partial CM4 development environment**, based on
QEMU's `raspi4b` BCM2711 model. It boots the official CM4 kernel and filesystem,
provides a maintenance shell, an AXP221 PMIC, USB input substitutes, writable
disk overlays, image export, QMP controls, and a desktop source editor/serial
workbench with a machine-readable agent interface.
It is **not yet a complete virtual uConsole**. In particular, it does not validate
the DSI display, VideoCore GPU, battery/charging behavior, STM32 keyboard
firmware, or the complete modem.

## Research and adoption decision

Research performed 2026-09-21:

* [QEMU Raspberry Pi models](https://www.qemu.org/docs/master/system/arm/raspi.html)
  include a Pi 4B with four Cortex-A72 CPUs and 2 GiB of RAM. QEMU supplies the
  useful CPU, interrupt, storage, UART, USB and mailbox foundation. Its documented
  missing devices include PCIe, GENET Ethernet and PWM. The upstream development
  documentation is newer than the pinned build; source inspection and local
  tests establish the capabilities actually used here.
* [QEMU 10.2.4 source](https://github.com/qemu/qemu/tree/v10.2.4) is the pinned
  baseline. The host's preinstalled QEMU 8.2.2 does not have `raspi4b`.
* [void-uconsole](https://github.com/Ravenhammer-Research/void-uconsole) documents
  a Pi 3 QEMU launch for its image. It is an archived OS project, not a complete
  uConsole device model. Reuse upstream QEMU rather than adopting this wrapper.
* [Renode](https://github.com/renode/renode) is relevant to future MCU co-simulation,
  but this search did not identify a ready-made CM4/uConsole platform to adopt.
* A [ClockworkPi community request](https://forum.clockworkpi.com/t/emulate-the-cm4/15122)
  asks for the same capability; the thread does not supply a working full-device
  emulator. This is evidence of a gap, not proof that no other project exists.

Generic QEMU `virt` machines can eventually provide a faster application-testing
profile, but they do not exercise BCM2711 hardware or boot the CM4 kernel in the
same way. They are not presented here as CM4 emulation.

## Connections and fidelity

Serial is one development connection, not the whole device interface.
The repository's keyboard firmware initializes USB composite HID keyboard,
mouse/trackball, joystick, consumer controls, and a USB serial interface.
Firmware flashing uses USB reset/DFU. The core also has hardware UARTs. The
optional modem uses GPIO power sequencing and USB data/control/fastboot.

| Component | Current implementation | Remaining gap |
| --- | --- | --- |
| BCM2711 CPU, interrupts, clocks | QEMU Pi 4B, four Cortex-A72, TCG, 2 GiB | CM4 board identity, other RAM sizes, timing fidelity |
| SD and Linux | Official `kernel8.img`, CM4 DTB, actual ext4 root, qcow2 writes | Firmware/EEPROM boot chain; exact CM4 SD routing |
| UART | PL011 console, stdio or loopback TCP | Electrical/timing validation, UART Bluetooth assignment |
| Keyboard/trackball | QEMU USB keyboard and relative mouse over DWC2 | STM32F103 firmware, composite descriptors, Fn layers, joystick, LEDs, CDC and DFU |
| Watchdog/power reset | Repository QEMU patch adds countdown/reload/cancel/expiry | Broader power-management register coverage |
| Display | Optional local GTK/SDL or loopback VNC framebuffer | VC4/V3D, DSI, CWU50 panel and OCP8178 backlight; no panel-fidelity claim |
| PMIC/battery | QEMU AXP221 at I2C address 0x34; Linux driver probes it | Regulators, battery state, ADC, charging and interrupt behavior |
| Networking | Optional QEMU USB RNDIS adapter and user-mode NAT/SSH forwarding | Actual Wi-Fi/BT hardware and radio behavior; guest configuration still required |
| Optional 4G | Not modeled | SIM7600 USB/AT/QMI/fastboot, GPIO power/reset and fault scenarios |
| Audio/expansion | Not modeled | PWM/audio path and expansion hardware |

The real panel/PMIC wiring is in `clockworkpi-uconsole-overlay.dts` in the patched
kernel; the keyboard descriptors are in `Code/uconsole_keyboard/uconsole_keyboard.ino`.
Neither generic USB input devices nor a host console count as running that MCU firmware.

## Build and prepare

Python **3.12+** is recommended. On Ubuntu, source-build prerequisites are
`build-essential ninja-build pkg-config libglib2.0-dev libpixman-1-dev libslirp-dev`
and Python with venv support. Tk is needed only for the desktop workbench
(`python3-tk` on Ubuntu). QEMU's configure step reports any additional dependencies.

```sh
python3 tools/build_emulator_qemu.py
python3 tools/uconsole_emulator.py prepare /path/to/uConsole_CM4_v3.1_64bit.img.bz2
```

The bootstrap downloads QEMU 10.2.4 from the official HTTPS distribution site,
checks the repository-pinned SHA-256, applies the watchdog, upper-memory and
uConsole AXP221 patches, and builds in
`build/emulator/`. It does not replace system QEMU. The archive hash records the
artifact used here; it is not a signature-verification mechanism.

Preparation verifies the image against the recorded official-image SHA-256,
decompresses sparsely, reads FAT boot files without mounting anything, and creates
`build/emulator/workspace/`. Keep this directory together: its overlay references
the absolute path of its backing image. A custom `.img` or `.bz2` requires its
expected hash via `prepare IMAGE --sha256 HASH`; it must retain the supported
CM4 image layout and filenames. An existing workspace is never replaced.

Allow approximately 15–20 GiB for source/build/base images and subsequent image
exports, depending on the image and guest writes. Multiple workspaces consume
additional space. All generated material is ignored by Git.

## Run, edit, and export an image

```sh
# Root maintenance shell: boots the actual root filesystem without systemd.
python3 tools/uconsole_emulator.py run --mode maintenance

# Normal systemd boot, serial console and optional SSH port forwarding.
python3 tools/uconsole_emulator.py run --serial-port 4445 --ssh-port 2222

# Desktop source editor, serial console, agent context, tasks, files and export.
python3 tools/uconsole_workbench.py
```

On this Ubuntu host use `/usr/bin/python3 tools/uconsole_workbench.py`, since
that Python installation has Tk. Select text in either pane and press
**Copy selection**, Control-C, Command-C, or use the right-click menu. **Copy
boot log** copies the entire visible serial transcript. **Copy agent context**
places a clean Markdown bundle with workspace identity, runtime state, fidelity
limits and recent boot output on the system clipboard, ready to paste into a
chat. **Paste command** moves clipboard text into the line input without sending
it, so the command remains reviewable.

The **Guest files** browser lists and downloads files through the maintenance
console. **Tasks** loads the checked-in `uconsole-tasks.json`; tasks use argument
arrays or explicit guest scripts and produce structured output. The display
selector can open QEMU's GTK or SDL framebuffer in a second window. This still
does not emulate DSI/VC4. The workbench does not yet have language servers or an
integrated GDB frontend. Its console accepts lines; use SSH or another terminal
client for full-screen terminal programs. Start only one owner for ports
4444/4445 at a time.

The same operations are available without the GUI for coding agents and CI:

```sh
python3 tools/uconsole_agent.py inspect
python3 tools/uconsole_agent.py context --output /tmp/uconsole-context.md
python3 tools/uconsole_agent.py tasks
python3 tools/uconsole_agent.py task guest-summary
python3 tools/uconsole_agent.py exec -- uname -a
python3 tools/uconsole_agent.py ls /etc
python3 tools/uconsole_agent.py put ./config /etc/example.conf
python3 tools/uconsole_agent.py get /var/log/boot.log ./boot.log
```

Commands return JSON where automation needs state or transfer metadata. Guest
commands and file operations require a maintenance shell on the selected serial
port. Transfers are checksum-verified and limited to 8 MiB; use SSH/SCP for
larger artifacts. The context command strips ANSI escapes and its own command
wrappers so an agent receives useful boot evidence rather than terminal noise.

To modify an image from a host terminal:

```sh
python3 tools/uconsole_emulator.py run --mode maintenance --serial-port 4445
# In another terminal, once the root prompt appears:
python3 tools/uconsole_emulator.py put ./example.conf /etc/example.conf
```

The legacy emulator `put` command uses the existing root shell, uploads files up
to 1 MiB in bounded chunks,
verifies the SHA-256 in the guest, installs with mode 0644 and syncs. It replaces
the specified guest file. Only one client can own the serial connection; the
workbench releases its connection during its Copy to guest operation. For shell
inspection, mount `/proc` and `/sys` in maintenance mode as needed:

```sh
mount -t proc proc /proc
mount -t sysfs sys /sys
cat /etc/os-release
cat /proc/bus/input/devices
```

Before stopping a maintenance VM, mount `/proc` and `/sys` as above, then run
`sync && mount -o remount,ro /dev/mmcblk1p2 /` for the tested CM4 profile and wait
for success, then `python3 tools/uconsole_emulator.py control quit` on the host.
The workbench's Power off button performs that sequence and waits for the
guest acknowledgment, resolving the mounted root's device through procfs/sysfs.
An explicit source device avoids relying on udev-created UUID links, which do
not exist in the maintenance shell. In normal mode use `sudo poweroff` inside the guest.
`control quit` alone is a virtual power cut, not a clean guest shutdown.

```sh
python3 tools/uconsole_emulator.py export /path/to/new-uconsole.img
```

Export flattens a stopped overlay to a **new** raw image, prints its checksum,
and refuses existing destinations or an image currently held by QEMU. It does
not flash a physical drive. The exported disk contains your guest changes;
emulator-only DTB/command-line changes stay outside the SD image. Firmware
`config.txt` is not interpreted by QEMU's direct kernel loader, so changing it
inside the image does not establish that hardware boot or display settings work.
Kernel/DTB files extracted during preparation are likewise not automatically
refreshed after a guest kernel upgrade: export and prepare a new workspace.

## Debug/control interfaces and platform status

`run --gdb-port 1234 --pause` starts paused for a GDB remote connection.
`control query-status`, `control stop` and `control cont` use QMP.
`run --vnc-display 1` exposes QEMU's framebuffer at 127.0.0.1:5901; it does
**not** implement the uConsole DSI display. Network, QMP, GDB and serial listeners
bind only to loopback. `--ssh-port` creates a USB network substitute and forwards
to guest port 22; it does not enable SSH or provision credentials.

`run --display gtk` and `run --display sdl` open the same generic QEMU
framebuffer locally. `--keyboard-cdc-port 4550` adds a USB serial surrogate for
the STM32 CDC function, while QEMU's separate USB keyboard and mouse cover input.
`--modem-at-port 4551` adds one generic USB serial surrogate for AT-command
client development. These split devices deliberately do not claim the uConsole
STM32 composite descriptors, DFU behavior, or the SIM7600 USB/QMI/fastboot
contract.

## Compare with the physical CM4 uConsole

The inventory tool captures the same read-only probes from an emulator or real
machine without storing credentials. SSH must already be configured:

```sh
python3 tools/uconsole_hardware_probe.py capture emulator.json
python3 tools/uconsole_hardware_probe.py capture hardware.json --ssh user@uconsole.local
python3 tools/uconsole_hardware_probe.py compare emulator.json hardware.json
```

It records kernel, device-tree model, CPU, USB/input devices, network links,
power supplies, I2C, GPIO, modules and command line, preserving failures when an
optional command is absent. The comparison identifies changed probes without
pretending textual equality is hardware equivalence. A physical capture remains
pending until this machine has an authenticated SSH route to the booted unit.

| Host | Current status |
| --- | --- |
| Linux ARM64 (this machine) | QEMU built and CM4 maintenance boot exercised; Tk tested under Xvfb |
| Linux x86-64 | Same TCG design; not locally boot-tested |
| macOS | Portable Python/Tk tools; requires a QEMU build with the watchdog patch; not boot-tested |
| Windows | Portable Python/Tk tools and `.exe` selection; patched QEMU must be supplied; source bootstrap is POSIX-only; not boot-tested |

The CLI accepts global `--qemu`, `--qemu-img`, and `--workspace` arguments before
the subcommand. Host-tool CI runs on Linux/macOS/Windows; that is not a substitute
for a full boot/GUI qualification matrix, and no remote CI result is claimed yet.

## Emulator-only compatibility changes

The prepared DTB is a copy of the shipped **CM4** DTB, not a generic virtual-board
tree. It enables the DWC2 controller in host mode, disables the Bluetooth child
of UART0, and removes UART `skip-init` and hardware flow-control properties.
The shipped aliases name this PL011 console `ttyAMA1`. The direct kernel command
line selects it and identifies root by the image's MBR partition UUID.
The full uConsole carrier overlay is not applied because its DSI panel,
backlight and regulator graph still reference devices absent from QEMU. The
machine patch supplies the PMIC node directly. QEMU also removes several
unsupported BCM2711 nodes itself. Each difference limits hardware-validation
claims.

The [watchdog patch](../Code/patch/qemu/bcm2835-watchdog-timer.patch) fixes QEMU's
immediate-reset behavior when Linux arms its watchdog. The Linux driver uses
65536 ticks/second in a 20-bit register; a QEMU virtual-clock timer now implements
that countdown and reload/stop behavior, including Linux's poweroff encoding.
Migration state is version 2; old version-1 device states are not accepted.
This is a local patch, not an upstream-merged fix.

The [upper-memory patch](../Code/patch/qemu/raspi4-upper-memory.patch) also fixes
the decision to add the upper RAM region to the device tree. It must use the
board's total RAM, not the already-limited boot memory size. Without this change
Linux sees only the lower region (about 917 MiB usable); with it the tested
kernel reports `MemTotal: 1902268 kB`.

The [AXP221 patch](../Code/patch/qemu/uconsole-axp221-pmic.patch) attaches QEMU's
existing AXP221 model at the uConsole address, enables the CM4 I2C controller in
the runtime tree, and describes AC and battery child functions. Linux identifies
the device as AXP221. The current QEMU model exposes its register file but does
not yet simulate a configurable battery, charge cycle, ADC readings, regulator
wiring, or PMIC interrupts.

## Validation and remaining implementation

```sh
python3 -m unittest discover -s tests -p 'test_emulator.py' -v
python3 tools/test_emulator_watchdog.py
python3 tools/test_emulator_pmic.py
xvfb-run -a /usr/bin/python3 -m unittest discover -s tests -p 'test_emulator_gui.py' -v
```

Tests cover image checksums/preservation, FAT extraction and cyclic chains,
DTB changes, launch isolation, destination protection, agent task/context
contracts, clipboard behavior, and real QEMU watchdog and PMIC MMIO behavior.
Local build and boot evidence is in
`build/emulator/`; see `emulator-validation.md` for observed results.

Full-device implementation still needs the DSI/VC4 and panel/backlight models,
AXP221 battery/charging extensions, STM32/USB composite firmware path, SIM7600 model,
audio and wireless coverage. Add each model against documented register or USB
contracts with driver-level tests and comparison captures from the physical CM4.
Do not turn missing hardware into permanently successful fake responses.

For a complete IDE, keep QMP, GDB, serial and image operations behind this host
tool boundary; next add project/build tasks, a real terminal component, debug
UI, image browsing/diffing, device scenario controls, and packaged installers.
Release qualification must include actual boot/edit/export/debug workflows on
Linux, macOS and Windows, alongside physical CM4 validation.
