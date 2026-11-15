# Pi 5 CQHCI development model

The BCM2712 SD host exposes CQHCI at `0x1000fff200`, alongside its SDHCI
registers. The controller submits commands through the actual QEMU SD bus and
reads descriptors and data through the host's DMA address space. An unsupported
card command reports an error; it cannot produce a successful data completion.

This is development work. The current QEMU SD v2/v3 card does not implement
command queuing. Positive queued read/write DMA, scatter/gather boundaries,
coalescing and migration during queued data transfer still need qualification
with a command-queue-capable card backend. A successful Linux probe or direct
CMD13 does not establish those results. Existing FreeBSD boot records identify
their original binaries and do not qualify this new controller.

The [November 15 observation](pi5-cqhci-verification-20261115.json) binds the
native Linux ARM64 binary and model bytes to 37 passing CQHCI controls, the
Pi 5 boot/memory and generic SDHCI regressions, a retained Linux shell/storage
replay and all 21 retained FreeBSD multi-user checks at 1 GiB. Both OS replays
use fresh COW disks and finish with natural reboot; their base inputs remain
unchanged. These are reference contexts, not the final matched CURRENT release.

The virtual profile reports CQHCI version 5.10, no inline crypto and a nominal
1 MHz internal timer. That frequency is a model parameter, not a measured
BCM2712 clock. Analog timing, transport/PHY behavior and physical SD performance
are outside this model. Register and descriptor references are the pinned
Raspberry Pi Linux
[`cqhci.h`](https://github.com/raspberrypi/linux/blob/ab4372076bb840600e522ae776887ae2bc488fa6/drivers/mmc/host/cqhci.h)
and
[`cqhci-core.c`](https://github.com/raspberrypi/linux/blob/ab4372076bb840600e522ae776887ae2bc488fa6/drivers/mmc/host/cqhci-core.c).

Build and exercise the native QEMU binary:

```sh
python3 tools/build_emulator_qemu.py --jobs 4
python3 tools/test_emulator_pi5_cqhci.py \
  --qemu build/emulator/qemu-build/qemu-system-aarch64 \
  --output /path/to/new-cqhci-results
```

The test retains synthetic card images, QEMU logs and a JSON result. It checks
real SD initialization and CMD13, separate interrupt capture/signal gates and
the GIC input, descriptor execution across migration, unsupported-card refusal,
task clearing, reset, malformed descriptors, crypto refusal and migration
profile mismatches. Its source and target card images are disposable; no
operator disk is opened.

The default `raspi5b` profile includes CQHCI. Set `sd-cqhci=off` before machine
initialization only when migrating a pre-CQHCI machine. Cross-profile migration
is refused rather than silently discarding controller state. The optional
`--legacy-qemu /path/to/original/qemu-system-aarch64` additionally tests both
wire-migration directions against the original executable. The legacy profile
is not a substitute for implementing the hardware declared by a guest DTB.

Because CQHCI connects to generic SDHCI interrupts and reset, also run
`tools/test_emulator_sdhci_stop.py`, `tools/test_emulator_sdhci_adma_stop.py`,
the Pi 5 boot/memory controls and Linux/FreeBSD guest regressions on the exact
candidate binary. Broader platform, desktop, physical and distribution
acceptance remain separate gates.
