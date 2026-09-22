# Local CM4 emulator validation

Tested on 2026-09-21, Linux ARM64 (`sparky`), using QEMU 10.2.4 with the two
repository patches. These results establish a usable development environment,
**not complete uConsole hardware equivalence**. See [coverage](emulator.md).

## Inputs

* Official `uConsole_CM4_v3.1_64bit.img.bz2` SHA-256:
  `ef95242cdb0125e8ed08157400a26d4665acddd083481ec2314acd6e073b74ab`.
* Decompressed raw image SHA-256:
  `a7b0a2bfa86a45150af1ae70a94ed432718bfec90ffdef54952564bd34f0d788`.
* Shipped `kernel8.img` SHA-256:
  `4871c5bbb93f24aadfc574eb6af392464f11bbc4f0eb50e1b40eff3e78d67276`.
* Guest identifies itself as Debian GNU/Linux 13.2, kernel `6.12.62-v8+`,
  AArch64, Raspberry Pi Compute Module 4 device tree.

## Observations

* The shipped kernel mounts the real ext4 root filesystem from an 8 GiB virtual
  SD overlay. No replacement distribution or generic `virt` kernel is involved.
* The maintenance shell accepts commands over the emulated PL011 UART.
* After the RAM fix, `/proc/meminfo` reports `MemTotal: 1902268 kB`.
* Linux enumerates QEMU USB keyboard and relative mouse devices through DWC2.
  QMP keyboard injection delivered Linux SysRq sync, remount and poweroff
  requests in the normal guest. This does not validate STM32 keyboard firmware.
* Normal systemd boot completes the image's first-boot resizing/setup and its
  requested reboot. A subsequent boot reaches `clockworkpi login:` on ttyAMA1.
  It acquires `10.0.2.15` through the optional USB network substitute, and host
  port 2222 returns `SSH-2.0-OpenSSH_10.0p2 Debian-7`. Authenticated SSH was not
  established; no image account/password was changed.
* The EEPROM updater fails without real EEPROM hardware. Several physical
  GPIO/regulator devices remain deferred, and hardware-dependent services can
  delay boot/shutdown. A login prompt does not imply all services are healthy.
* A host Markdown file was copied into `/root/emulator-guide.md`, checked with
  SHA-256 in the guest, synced, and exported to a new raw SD image. Booting a
  new overlay of that exported image retained the same file checksum:
  `d391d7c52b7cc00b159137a8672a6316b78302926c5ad137fa1c4c41b5786e31`.
  This records the content at upload time; the host guide has since been edited.
* The exported image SHA-256 was
  `2e9d72879cd4b29fa1be863d2ff85c0e0a4e89401d44c1725f8059b53bcf13ba`.
  An attempted export while QEMU held the disk failed on its image lock.
* QMP status, pause and resume work. The workbench editor/copy test created
  `/root/gui-proof.txt` and read it back with SHA-256
  `a06f8425622ce728e62499f0cf8d72b0eb727b6556f2606b838814b8c7132a9b`.

## Checks and evidence

The repository suite runs 37 tests. All 37 passed with the system Python/Tk
under Xvfb. The default Python run skips GUI tests if Tk is unavailable.
ShellCheck passes. The QEMU qtest harness separately exercises watchdog arm,
countdown, reload, cancel, password rejection, expiry/reset and Linux poweroff.

Generated local evidence (ignored by Git):

| Path under `build/emulator/` | Evidence |
| --- | --- |
| `qemu-configure.log`, `qemu-build.log` | Baseline QEMU configuration/build |
| `qemu-watchdog-build.log`, `qemu-memory-build.log` | Device-model fixes built |
| `watchdog-test.log` | Real QEMU register/timer tests |
| `maintenance-evidence.log` | Guest OS, kernel, RAM, input devices |
| `normal-firstboot-patched.log` | First-boot setup and requested reboot |
| `workspace/serial.log` | Subsequent normal login, network, USB SysRq poweroff |
| `upload-evidence.log`, `export-evidence.log` | Guest file upload and raw export |
| `workbench-smoke.log`, `workbench.png` | Desktop workflow and screenshot |
| `export-smoke/serial.log`, `export-smoke/transfer.log` | Exported-image boot, GUI file copy/readback |
| `repo-tests.log`, `all-gui-tests.log` | Repository tests and Tk checks |
| `base-preservation.log` | Original raw image and backing-image hashes |

The redundant scratch raw image was removed after the preservation check;
the working workspace's backing image and exported SD image are retained.

macOS/Windows boot, graphical uConsole desktop, GDB interaction, physical CM4
comparison, keyboard flashing, modem, battery, charging, audio and DSI display
remain unvalidated. Host-tool CI is configured for three operating systems;
it has not been pushed/run remotely as part of this local work.
