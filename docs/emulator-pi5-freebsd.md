# Pi5 / BCM2712 FreeBSD multi-user SD model

The separate `raspi5b` machine boots the FreeBSD-based Daemonarchy kernel to
multi-user, runs the standard `/etc/rc`, and provides a real serial getty/login.
It has four Cortex-A76 CPUs with BCM2712 affinities `0`, `0x100`, `0x200`,
`0x300`, PSCI, GIC-400, the SoC debug PL011, RNG200 and the native BCM2712 SD
controller at their Pi5 physical addresses. It does not inherit the BCM2711
machine or use a Pi4 device tree. The initial enrolled board tree was the captured BCM2712 D0 Pi500 tree.
The current Model B D0 input and its separate acceptance are recorded below;
other silicon/pinctrl variants are not qualified by these results.

This is a digital SD/EFI bootstrap model with serial and firmware framebuffer
output. VideoCore ROM/EEPROM execution, RP1 USB/Ethernet/input, GPU acceleration,
physical HDMI behavior, PCIe electrical behavior, pad voltages and analog
clock/timing behavior are not implemented or qualified here. Use the existing
selected PCIe2 contract separately for its host/MSI/DMA tests. It is not a
replacement for physical boot acceptance or an installed desktop distribution.

## Inputs and boot path

The boot path is QEMU's AArch64 Linux boot ABI into FreeBSD's existing
`LINUX_BOOT_ABI` support. `prepare_pi5_boot.py` converts the ELF load segments
into an Image, adding the AArch64 Image header in the unused ELF header area.
The kernel's executable bytes are preserved. A native FreeBSD EFI loader and
Pi5 U-Boot/firmware handoff are exercised by the separate firmware lane below;
they are not part of this direct boot.

The derived FDT preserves the enrolled Pi5 CPU, SoC bus translation, GIC, SD,
UART, clock, pin and regulator bindings. It explicitly disables unimplemented
devices, omits unused fixed-clock placeholders, sizes RAM and selects the debug
UART. Its manifest records the input/output SHA256 identities and every changed
property/removed clock node. Unsupported clock nodes are omitted because the
FreeBSD fixed-clock probe also attempts disabled placeholders with no frequency.

The machine accepts 1, 2, 4, 8 and 16 GiB, with 8 GiB default. All RAM banks appear as
`reg` tuples in one memory node, preserving the enrolled root's one or two
size cells: the FreeBSD direct-boot FDT parser reads that
node rather than collecting separate root memory nodes. The firmware profile
excludes a 64 MiB low-memory VideoCore/framebuffer window from ARM-usable RAM.
The 1 GiB profile has no bank above 1 GiB; the 16 GiB profile keeps
64-bit bank addresses without truncation. The 1/16 GiB defaults report the
documented Model B revision 1.1 memory identities, while explicit board revision
overrides must still match the selected capacity. See the
[official revision encoding and model table](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#new-style-revision-codes).
The [memory verification record](pi5-memory-verification-20261111.json) binds the
native QEMU binary and exact model bytes. Run
`tools/test_emulator_pi5_memory.py --qemu <binary> --output <new-directory>`
to exercise all five Model B capacities, documented Pi500/500+ capacities,
invalid family/capacity combinations, actual RAM sentinels, mailbox revision
responses and the stopped synthetic boot FDT. This does not claim a guest OS
boot or full peripheral fidelity.
The boot registers
have explicit MMIO/reset/migration state; SD card-detect reflects the attached
QEMU SD card rather than a constant insertion flag. Polled GPIO is the modeled
subset; general GPIO interrupt delivery is not claimed.

A fresh ARM64 GENERIC kernel with the BCM2712 drivers is required. Record
its build host and compiler scope separately: a FreeBSD amd64-hosted ARM64
cross-kernel is a valid simulator input, not native ARM64 qualification. The BCM2712 SD card-detect consumer
must pass its actual FDT node to the OFW GPIO API. Earlier D520 kernels pass zero
and panic on the real Pi500 card-detect property. The correction is in
[daemonarchy-src PR77](https://github.com/jordanhubbard/daemonarchy-src/pull/77),
source `26afadf8a344b572515919baef9d9081defb37e2`, stacked on the earlier mapping
and PL011 fixes. The qualified development kernel's SHA256 is
`92ad4a467541c86d57673cbfb4a334995c8e51ec897d315e06a5df25442b47a0`.
The native compiler/bootstrap provenance remains recorded separately; an
installed compiler is not claimed as a source-qualified compiler product.

## Reproduce

Build isolated QEMU with `python3 tools/build_emulator_qemu.py --jobs 4`.
On Linux hosts with both Homebrew and system GLib tools installed, use a build
PATH whose `pkg-config`, GLib headers/libraries and `gdbus-codegen` agree. The
recorded Linux build used `/usr/bin:/bin:/usr/local/bin` first and no inherited
`LD_LIBRARY_PATH`/`DYLD_LIBRARY_PATH`.

On an allocated FreeBSD worker, assemble the lab disk from an aarch64 ISO
and the selected ARM64 kernel. The kernel copied into the disk must match the
prepared boot input exactly. Keep all staging and outputs outside source and
the live root; the lab's retained userland is not a final CURRENT distribution
closure:

```sh
sudo sh tools/build_pi5_freebsd_root.sh \
  -i /path/to/matching-aarch64.iso -s ISO_SHA256 \
  -k /path/to/native-GENERIC-kernel -K KERNEL_SHA256 \
  -o /path/to/new-pi5-root
```

This writes a new regular UFS/GPT image; it refuses existing output directories.
It retains the actual base `/etc/rc`, init, getty/login and libraries. The private
VM has an empty root console password, no SSH service and no emulated NIC in
the test command. It is not a published general-purpose image.

Copy/decompress `pi5-multiuser.img.xz` to the simulator host. The current Model B
D0 lane uses the unchanged official
[`bcm2712d0-rpi-5-b.dtb`](https://github.com/raspberrypi/firmware/blob/016253866bbd6c77fcb2a744a97d81e79ad4884f/boot/bcm2712d0-rpi-5-b.dtb),
SHA256 `d01e2104a8b747e99e310bfb22aeac8100e3b98df29951b296b5cdc2e95afb53`.
Do not substitute the similarly named `bcm2712-d-rpi-5-b.dtb`: its mixed main
pinctrl bindings fail the current D0 driver's default pinctrl admission. That
failed boot remains a negative observation; it is not acceptance for that variant.
The historical Pi500 input `400b2da08abb8fd0ffd65085d1e0e1e6c6d01cadd27385403ea3a3fd5a1537f4`
belongs to the earlier runs, not these new Model B inputs.

The current kernel is built from reviewed source
`87e0a125850c5c791515fb7ae31ebcb6e8cfce72`; its SHA256 is
`3604ee5b36b898bec6c069cb1a3d58b13f3e282bf386eeecd86544febc413f54`.
Its build uses original FreeBSD Makefiles on FreeBSD 16 amd64, targeting ARM64
GENERIC. Native ARM64, SDK/lifecycle and final distribution qualification remain
separate. See [the current OS verification record](pi5-os-verification-20261111.json).

Prepare the selected capacity and enroll its board identity:

```sh
python3 tools/prepare_pi5_boot.py \
  --dtb /path/to/bcm2712d0-rpi-5-b.dtb --kernel /path/to/kernel \
  --memory-gib 1 --output /path/to/new-boot-inputs
python3 tools/run_pi5_freebsd.py \
  --qemu build/emulator/qemu-build/qemu-system-aarch64 \
  --boot-inputs /path/to/new-boot-inputs \
  --disk /path/to/pi5-multiuser.img --memory-gib 1 \
  --output /path/to/new-run
```

The runner hashes its regular-file inputs, creates a fresh COW overlay, drains
the serial console continuously and bounds command frames. It logs in through
getty/login, checks four CPUs and full modeled RAM, init/services, native UFS,
the on-disk kernel hash and storage I/O, then requests reboot. It queries the
actual page size and requires the boot's real-memory count to equal every modeled
bank, minus the 64 MiB firmware reservation and FreeBSD's excluded first page.
The separate available-memory check accounts for kernel/bootstrap allocations;
a percentage threshold alone cannot establish that all hardware banks were read.
Source image invariance, disk synchronization and QEMU natural exit are checked separately.
Logs, commands, input hashes and individual guards are saved in `result.json`.
No physical disk is accessed.

For an interactive console using a separately created writable COW disk,
select the board revision from the prepared manifest and the matching capacity.
This example uses Model B at 1 GiB:

```sh
build/emulator/qemu-build/qemu-system-aarch64 \
  -M raspi5b,board-revision=0xa04171 -m 1G -kernel /path/to/boot-inputs/FreeBSD-Image \
  -dtb /path/to/boot-inputs/pi5-sd.dtb \
  -drive if=sd,format=qcow2,file=/path/to/disposable-overlay.qcow2 \
  -display none -serial stdio -monitor none -nic none
```

## Current Model B D0 OS acceptance

All five capacities (1, 2, 4, 8 and 16 GiB) pass the actual FreeBSD multi-user
runner using the current unchanged official D0 Model B tree, the current kernel
and a fresh matching native-assembled lab disk. All 21 checks pass per capacity,
including exact hardware bank accounting, real getty/login, init/services,
SD/UFS, kernel hash, storage I/O, synchronization, natural reboot and unchanged
source inputs. The [verification record](pi5-os-verification-20261111.json)
binds every profile to the actual binary, prepared inputs, runner and serial log
hashes. The strengthened host refusal control also rejects a missing 1 GiB bank
at 16 GiB that the old percentage check alone would accept.

This is software SD/bootstrap acceptance. It does not close the remaining RP1,
GPU, VideoCore, alternate pinctrl profile or complete distribution acceptance.

## Actual U-Boot and EFI lane

`run_pi5_firmware.py` boots the U-Boot binary extracted from a complete GPT SD
image, then lets that binary read its own `boot.scr`, enter the actual EFI loader
and load FreeBSD from SD. Supply the exact board DTB present on the image's ESP.
The host does not remove devices or replace the kernel with a direct boot here.
A 4 KiB AArch64 entry transport preserves every U-Boot byte and supplies the
firmware-equivalent x0/FDT and EL2 entry. VideoCore ROM and EEPROM code are not
executed. The machine changes RAM sizing for the selected virtual capacity.

The model supplies the BCM2712 mailbox window, QEMU's shared firmware-property
and framebuffer devices with DMA-only VideoCore aliases, both SDHCI hosts, the
reset/watchdog block, SPI0 and MIP0 host-edge register/interrupt behavior. The
second SDIO host has no invented Wi-Fi card. MIP0 is not a complete PCIe host or
RP1 model; its selected edge mode does not implement VPU or level delivery.
`board-revision` must match BCM2712 Pi5/Pi500 and the selected RAM capacity.

```sh
python3 tools/run_pi5_firmware.py \
  --qemu build/emulator/qemu-build/qemu-system-aarch64 \
  --image /path/to/instrumented-pi500-hwtrace.img \
  --dtb /path/to/the-images-bcm2712-rpi-500.dtb \
  --memory-gib 8 --card-size-gib 32 \
  --output /path/to/new-firmware-run
```

This lane expects a diagnostic image whose boot script writes `UBOOT.LOG`
through the `PI500-HANDOFF-04-ABOUT-TO-ENTER-EFI` checkpoint and whose hwtrace
root saves `DUMPS/<pending>/done.txt`. It refuses physical devices and existing
output directories, creates a fresh COW overlay, and verifies unchanged input
hashes. The default 32 GiB virtual card exercises SDHC addressing; smaller
SDSC and larger SDHC capacities are explicit options. Captured readback contains
the source-image-sized GPT/ESP/root/DATRACE prefix, excluding unallocated card
space. It records serial milestones, actual EFI/init framebuffer PNGs, durable
dump hashes, each probe's exit status and natural guest reboot. Probe completion
does not imply every peripheral exists or passed a functional test.

The original unmodified firmware-tree lane exposed an additional native kernel
panic: the legacy VideoCore framebuffer driver claimed BCM2712 before finding
that its translation table has no matching SoC. Source
[PR78](https://github.com/jordanhubbard/daemonarchy-src/pull/78), commit
`31ec5babf638de96cf6568c9e8e01de4bd64f015`, declines that unsupported probe and
preserves EFI framebuffer output. Its native kernel SHA256 is
`70e11bc76666a11d72347fb4a75e9ff419e600ccd822afe030b2c39e503cfb2c`.
The preceding kernel remains a negative control for that panic.

The development firmware-chain run reaches all four CPUs, SD UFS root, init,
durable diagnostics and clean reboot with the original Pi500 D0 board bindings
and PCIe2 disabled. It does not diagnose the physical Pi500's earlier U-Boot
failure, prove hardware startup, qualify C1 or RP1, or establish persistent EFI
variables across real power loss. The direct multi-user lane remains a separate
regression rather than evidence for this firmware path.

## Regression controls

```sh
python3 tools/test_prepare_pi5_boot.py
python3 tools/test_pi5_firmware_inputs.py
python3 tools/test_emulator_pi5_boot.py \
  --qemu build/emulator/qemu-build/qemu-system-aarch64
```

The model checks exercise actual MMIO masking, real SD-backend presence/absence,
mailbox DMA aliases, MIP-to-GIC edge delivery/masking, CPU topology, random
words, VMState migration, reset and unsupported RAM/CPU
refusals. They run against rebuilt and packaged QEMU in native package CI.
Those controls alone do not establish a guest multi-user boot; the native
FreeBSD console/storage run above is the runtime acceptance.

The [RP1 endpoint development record](emulator-rp1.md) tracks the separate PCI
endpoint and its native MSI-X/IACK/migration tests. The Pi5 board does not yet
connect it; full southbridge and peripheral behavior remain open.
