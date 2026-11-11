# RP1 endpoint development

The pinned QEMU stack now includes a separate `raspberrypi-rp1` PCI
endpoint. It is the first part of the RP1 southbridge model. The
`raspi5b` machine does not yet connect it through the BCM2712 PCIe host.
Whole Pi 5 peripheral fidelity remains unfinished.

The model supplies the C0 PCI identity, three correctly sized memory BARs,
the MSI-X table and pending-bit array, SRAM, and the local interrupt bridge.
PCI enumeration, memory decode, MSI writes and migration use QEMU's PCI and
VMState implementations. Peripheral sources connect to the endpoint's 61
interrupt inputs. The documented TEST input is combined with the peripheral
level; it is not a synthetic MSI counter. The bridge implements raw status,
ENABLE, SET/CLR aliases and the optional self-clearing IACK handshake. Its
local acknowledgement mask remains independent of PCI vector/function masks.

Run the native device test after building the pinned QEMU:

```sh
python3 tools/test_emulator_rp1.py \
  --qemu build/emulator/qemu-build/qemu-system-aarch64
```

The test runs the endpoint on generic ARM `virt` to isolate it from the
unfinished BCM2712 host integration. It reads actual MSI DMA writes in guest
RAM for every vector; checks held levels, TEST/physical input interaction,
read-only raw status, vector/function masks and pending delivery; then
migrates a locally blocked level, a pending PCI interrupt and SRAM through
the real migration stream. It also exercises PCI memory decode, reset and
post-reset interrupt delivery. Host CI runs it with the other native models.

The endpoint does not yet implement RP1 management cores/ROM, peripheral
engines, system clocks/resets, power management, AXI configuration, nonzero
PCI function/traffic-class routing, or PCIe PHY/link timing. Unsupported
peripheral accesses report unimplemented operations rather than returning
fabricated ready flags. BAR2 provides 64 KiB storage; reset retention has not
been qualified against silicon. Full GPIO/UART/SPI/I2C, DMA, USB, Ethernet,
audio/video and board integration still require implementation and tests.
The existing BCM2711 PCIe2 contract remains a separate synthetic test machine.

References:

- [RP1 Peripherals, 2023-11-07](https://datasheets.raspberrypi.com/rp1/rp1-peripherals.pdf), interrupt bridge and register layout.
- [Vendor RP1 driver](https://github.com/raspberrypi/linux/blob/ab4372076bb840600e522ae776887ae2bc488fa6/drivers/mfd/rp1.c), Linux interrupt setup and atomic aliases.
- [Vendor bindings](https://github.com/raspberrypi/linux/blob/ab4372076bb840600e522ae776887ae2bc488fa6/include/dt-bindings/mfd/rp1.h), connected source numbers.
- [Published hardware PCI dump](https://github.com/raspberrypi/linux/issues/6471), BAR sizes and MSI-X table/PBA location.

These are software-model results, not physical-device acceptance.

The [native 2026-11-11 result](rp1-endpoint-verification-20261111.json) records
the tested model and executable hashes. The endpoint test, existing Pi5
boot/reset/migration checks, all ten RAM/FDT layouts, preparation and firmware
input suites pass on that executable; 33 host-tool controls also pass.
