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
engines other than the initial UART implementation, system clocks/resets,\npower management, AXI configuration, nonzero
PCI function/traffic-class routing, or PCIe PHY/link timing. Unsupported
peripheral accesses report unimplemented operations rather than returning
fabricated ready flags. BAR2 provides 64 KiB storage; reset retention has not
been qualified against silicon. Full GPIO/SPI/I2C, UART timing/flow control,\nDMA, USB, Ethernet,
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


## Six UART engines

The endpoint now maps all six UART banks and routes their combined interrupts
to RP1 sources 25 and 42 through 46. The child devices expose uart0 through
uart5 chardev properties on the PCI endpoint. Their peripheral identity follows
the vendor RP1 device-tree binding. Receive FIFOs hold 32 characters; when FIFO
mode is disabled they hold one. Full receive FIFOs preserve old data and report
overrun instead of overwriting unread characters.

The [native UART record](rp1-uart-verification-20261111.json) binds the exact
executable, model, patches and logs. Tests cover all six register banks,
external host RX/TX streams and MSI-X delivery, both FIFO capacities, overrun,
independent wrapped FIFO state across migration, interrupt acknowledgement
after migration, and reset. Separate tests migrate the original 16-entry UART
between the old and new executables in both directions, and reject both
16/32 profile mismatches. The original 16-word migration fields stay intact;
a bound subsection carries the extended profile and upper FIFO half.

Run the new tests with the pinned executable:

    python3 tools/test_emulator_rp1_uart.py --qemu build/emulator/qemu-build/qemu-system-aarch64
    python3 tools/test_emulator_pl011_profile.py --qemu build/emulator/qemu-build/qemu-system-aarch64

The profile test accepts an optional --legacy-qemu executable for actual
old/new migration controls. CI runs current/current and profile refusal controls.

The UARTs retain QEMU PL011's immediate TX and host-stream buffering. The RP1
receive profile now implements programmed RX thresholds and timeout behavior as
described below. Timed 32-entry transmit FIFOs, serial-bit/framing/error timing,
complete hardware flow control and DMA are still required. The RP1 clock/reset and pin-mux models also
remain open. These initial UART engines do not establish full RP1 UART fidelity
or a bootable Pi5 RP1 bus.


## Programmed receive interrupts and timeout

The RP1 receive profile uses thresholds of 4, 8, 16, 24 or 28 characters for
its 32-entry FIFO. UARTEN and RXE independently gate reception. The integer
and fractional baud registers take effect on an LCR_H write. A nonempty FIFO
raises timeout status after 32 baud periods without a new received character.
New arrivals restart that deadline; partial reads preserve a latched timeout,
while draining the FIFO clears it. Raw status, interrupt masking, acknowledgement
and RP1 IACK remain distinct operations.

The clock is a fixed nominal 48 MHz until the RP1 clock tree is implemented.
Tests drive controlled byte arrivals through the existing immediate loopback
path and explicitly enable qtest virtual-timer dispatch. They do not qualify
serial-bit reception or transmit timing. Timer state and the receive policy
are migration-bound: a target with a different accurate-rx policy is refused.
Original default 16-entry UART behavior and wire compatibility remain retained.

Run:

    python3 tools/test_emulator_rp1_rx.py --qemu build/emulator/qemu-build/qemu-system-aarch64

The [native receive result](rp1-rx-verification-20261111.json) records all six
UARTs at every threshold, exact timeout boundary, fractional rounding, divisor
latching, independent receive enables, restart/drain behavior, raw versus masked
interrupts, real MSI-X delivery, remaining-deadline migration and reset
cancellation. The old UART model fails the below-threshold control. The extended
UART/external-stream, legacy migration and Pi5 boot/memory regressions pass on
the same executable.

The common PL011 receive rules are documented in the manufacturer's
[RP2040 datasheet, UART interrupt and register sections](https://datasheets.raspberrypi.com/rp2040/rp2040-datasheet.pdf);
RP1's PL011 integration and nominal clock are specified in the
[RP1 peripheral reference](https://datasheets.raspberrypi.com/rp1/rp1-peripherals.pdf).
RP2040's surrounding clock/reset and address map are not used for RP1.
