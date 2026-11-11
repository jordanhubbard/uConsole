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
engines other than the initial UART implementation, system clocks/resets,
power management, AXI configuration, nonzero
PCI function/traffic-class routing, or PCIe PHY/link timing. Unsupported
peripheral accesses report unimplemented operations rather than returning
fabricated ready flags. BAR2 provides 64 KiB storage; reset retention has not
been qualified against silicon. Full GPIO/SPI/I2C, remaining UART serial/error behavior,
DMA, USB, Ethernet,
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

The RP1 receive and transmit profiles implement programmed FIFO interrupts and
virtual-time deadlines as described below. Serial-bit error/break timing, IrDA,
host-stream backpressure, DMA and the RP1 clock/reset/pin-mux models remain open.
These engines do not establish full RP1 UART fidelity or a bootable Pi5 RP1 bus.



## Programmed receive interrupts and timeout

The RP1 receive profile uses thresholds of 4, 8, 16, 24 or 28 characters for
its 32-entry FIFO. UARTEN and RXE independently gate reception. The integer
and fractional baud registers take effect on an LCR_H write. A nonempty FIFO
raises timeout status after 32 baud periods without a new received character.
New arrivals restart that deadline; partial reads preserve a latched timeout,
while draining the FIFO clears it. Raw status, interrupt masking, acknowledgement
and RP1 IACK remain distinct operations.

The clock is a fixed nominal 48 MHz until the RP1 clock tree is implemented.
Tests complete timed loopback frames before checking receive deadlines and
explicitly enable qtest virtual-timer dispatch. They do not qualify serial-bit
reception or the unfinished clock tree. Timer state and the receive policy
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

## Timed transmit and flow control

The RP1 profile has a 32-entry TX FIFO (one holding register with FEN clear)
and a separate shift register. TXFE/TXFF describe queued data; BUSY also covers
the active frame. Programmed thresholds generate interrupts on FIFO crossings.
UARTEN, TXE and CTS stop between frames while preserving queued data. Frame
duration includes the selected5–8 data bits, parity and1–2 stop bits, with
fractional baud rounding and LCR_H divisor latching.

The asserted-level CTS input and RTS output are named GPIOs on each UART child.
Automatic RTS follows the receive watermark; loopback connects it to CTS.
Both modem edges latch until ICR. Migration binds the TX policy, queue, shift,
modem input and exact deadline; reset cancels pending transmissions. Default
legacy PL011 profiles retain their original wire format.

Run:

    python3 tools/test_emulator_rp1_tx.py --qemu build/emulator/qemu-build/qemu-system-aarch64

The [native TX observation](rp1-tx-verification-20261111.json) records frame
boundaries, actual MSI-X writes, external CTS/RTS/byte streams, wrapped and blocked
migration, reset, receive regressions and old/new legacy migration.

This is frame-level UART behavior. Serial-bit parity/framing errors, break/IrDA,
DMA, pin routing and clock-tree fidelity still require implementation. The
inherited blocking host writer also needs replacement and backpressure tests.
The [manufacturer's PL011 reference](https://documentation-service.arm.com/static/5e8e36c2fd977155116a90b5)
defines the FIFO, flag, framing, interrupt and flow-control rules.
