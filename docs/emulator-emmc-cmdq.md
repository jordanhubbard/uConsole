# Virtual eMMC command-queue transport

`raspi5b,sd-emmc=on` selects a block-backed virtual eMMC card with command
queuing enabled as a supported capability. It requires an SD drive image and
the CQHCI host. The default `raspi5b` machine continues to use its ordinary SD
card and existing host capabilities. Set the profile before machine
initialization; SD/eMMC migration mismatches are refused in both directions.

This is a diagnostic transport profile. It does not describe a physical Pi 5
eMMC connection or establish BCM2712 DMA capabilities. Its virtual host exposes
ADMA2 and 64-bit DMA so a normal guest can initialize the card through SDHCI,
then exercise CQHCI. Ordinary ADMA transfers work even when SDMA is absent.

The card advertises EXT_CSD revision 8 and 32 queue slots. Queue mode starts
disabled and is enabled through CMD6. CMD44/CMD45 accept bounded sector requests;
CMD13 reports accepted tasks awaiting execution; CMD46/CMD47 move actual data
through the existing BlockBackend read/write engine. CMD48 manages card tasks.
The profile supports the ordinary user partition with 512-byte sectors.
Reliable writes, data tagging, nonzero context IDs, forced programming and
non-user-partition queueing are refused and are not advertised as supported.

The controller reads real task and scatter/gather tables and transfers real
card data. Host cancellation preserves completed writes, discards the partial
host FIFO and leaves card task management to the guest. Card and controller
migration retain their in-flight data and timer state. These semantics need
runtime evidence; descriptor acceptance alone does not establish data integrity.

Build and run the controls with fresh output directories:

```sh
python3 tools/build_emulator_qemu.py --jobs 4
python3 tools/test_emulator_cqhci_data.py \
  --qemu build/emulator/qemu-build/qemu-system-aarch64 \
  --output /path/to/new-emmc-results
python3 tools/test_emulator_pi5_cqhci.py \
  --qemu build/emulator/qemu-build/qemu-system-aarch64 \
  --output /path/to/new-default-sd-results
```

The data controls use disposable synthetic images. They exercise ordinary
ADMA32/64 EXT_CSD initialization without SDMA, card queue protocol errors,
scatter/gather boundaries, partial read migration, write cancellation,
interrupt coalescing by threshold and migrated timer, and descriptors and data
above 4 GiB. A 69,632-byte read includes a zero-encoded 65,536-byte segment.
Readback compares actual patterns and final backing bytes. Results record the
binary and harness hashes; raw images are excluded from CI uploads.

On Puck, writable QEMU images must currently use local scratch: its NFS mount
rejects QEMU's OFD lock operation. Keep locking enabled. Native source builds
use the existing compiler and SDK; the Raspberry Pi recipe disables QEMU's
unrelated Apple paravirtual GPU, whose older API is unavailable in newer SDKs.
No host tools are replaced.

The [November 15 native observations](pi5-emmc-cmdq-verification-20261115.json)
bind the model inputs to 51 passing data controls on both Linux and macOS ARM64,
42 Linux default controls (including both pre-CQ migration directions), 40 macOS
default controls, the generic storage/boot/memory regressions, a Linux queued
cold-cache storage readback and default Linux/FreeBSD reference replays. The
Linux queue probe requires actual MMC/CMDQ state and rejects I/O errors, CQE
recovery and data-loss warnings before accepting a natural reboot.

Synthetic controls and retained reference guests do not qualify matched
CURRENT system images, desktop behavior, physical storage timing, power loss
or the Daemonarchy 1.6 release. Run the default SDHCI, Pi 5 boot/memory and
reference OS regressions on the same candidate binary.
