/* SPDX-License-Identifier: GPL-2.0-or-later */
/*
 * Raspberry Pi RP1 PCI endpoint and local interrupt bridge.
 *
 * Register facts: RP1 Peripherals (2023-11-07), chapters 6 and 6.2;
 * raspberrypi/linux drivers/mfd/rp1.c and dt-bindings/mfd/rp1.h.
 * This is the endpoint foundation, not a completed RP1 southbridge:
 * no ROM/management CPUs, clocks, non-UART engines or PCIe PHY yet.
 */
#include "qemu/osdep.h"
#include "qemu/log.h"
#include "qemu/module.h"
#include "qapi/error.h"
#include "hw/pci/pci.h"
#include "hw/pci/pci_device.h"
#include "hw/pci/pcie.h"
#include "hw/pci/msix.h"
#include "hw/qdev-core.h"
#include "hw/qdev-properties.h"
#include "hw/char/pl011.h"
#include "migration/vmstate.h"
#include "qom/object.h"

#define TYPE_RP1 "raspberrypi-rp1"
OBJECT_DECLARE_SIMPLE_TYPE(RP1State, RP1)

#define RP1_VECTORS 61
#define RP1_CFG_REGS 64
#define RP1_PCIE_CFG 0x108000
#define CFG_ENABLE 1
#define CFG_TEST 2
#define CFG_IACK 4
#define CFG_IACK_EN 8
#define CFG_RW_MASK 0x0000000b

struct RP1State {
    PCIDevice parent_obj;
    MemoryRegion bar0;
    MemoryRegion peripherals;
    MemoryRegion unimplemented;
    MemoryRegion sram;
    DeviceState *uart[6];
    uint32_t cfg[RP1_CFG_REGS];
    uint64_t inputs;
    uint64_t blocked;
    uint64_t previous;
};

static bool rp1_raw(RP1State *s, unsigned n)
{
    return (s->inputs & (1ULL << n)) || (s->cfg[n] & CFG_TEST);
}

static void rp1_update(RP1State *s, unsigned n)
{
    uint64_t bit = 1ULL << n;
    bool level = rp1_raw(s, n) && (s->cfg[n] & CFG_ENABLE) &&
                 !((s->cfg[n] & CFG_IACK_EN) && (s->blocked & bit));

    if (level && !(s->previous & bit) && n < RP1_VECTORS) {
        /*
         * The local acknowledge mask is independent of PCI vector/function
         * masking. QEMU owns the MSI-X table, pending bit and PCI DMA route.
         */
        if (s->cfg[n] & CFG_IACK_EN) {
            s->blocked |= bit;
            level = false;
        }
        if (msix_enabled(PCI_DEVICE(s))) {
            msix_notify(PCI_DEVICE(s), n);
        }
    }
    s->previous = (s->previous & ~bit) | (level ? bit : 0);
}

static void rp1_input(void *opaque, int n, int level)
{
    RP1State *s = opaque;
    uint64_t bit = 1ULL << n;

    s->inputs = (s->inputs & ~bit) | (level ? bit : 0);
    rp1_update(s, n);
}

static uint64_t rp1_read(void *opaque, hwaddr offset, unsigned size)
{
    RP1State *s = opaque;
    hwaddr reg;
    uint64_t raw = 0;

    if (offset == 0) {
        return 0x20001927; /* C0 chip ID; this device is revision 2. */
    }
    if (offset >= RP1_PCIE_CFG && offset < RP1_PCIE_CFG + 0x1000) {
        reg = (offset - RP1_PCIE_CFG) & 0x3ff;
        if (reg >= 8 && reg < 8 + 4 * RP1_CFG_REGS) {
            return s->cfg[(reg - 8) / 4];
        }
        if (reg == 0x108 || reg == 0x10c) {
            for (unsigned n = 0; n < RP1_CFG_REGS; n++) {
                if (rp1_raw(s, n)) {
                    raw |= 1ULL << n;
                }
            }
            return reg == 0x108 ? (uint32_t)raw : raw >> 32;
        }
    }
    qemu_log_mask(LOG_UNIMP, "rp1: unimplemented read at 0x%" HWADDR_PRIx "\n",
                  offset);
    return 0;
}

static void rp1_write(void *opaque, hwaddr offset, uint64_t value,
                      unsigned size)
{
    RP1State *s = opaque;
    hwaddr local, reg;
    unsigned n, alias;
    uint32_t next;
    bool ack;

    if (offset == 0) {
        return; /* Read-only SYSINFO chip ID. */
    }
    if (offset >= RP1_PCIE_CFG && offset < RP1_PCIE_CFG + 0x1000) {
        local = offset - RP1_PCIE_CFG;
        reg = local & 0x3ff;
        alias = local & 0xc00;
        if (reg >= 8 && reg < 8 + 4 * RP1_CFG_REGS) {
            n = (reg - 8) / 4;
            if (alias == 0) {
                next = value;
            } else if (alias == 0x800) {
                next = s->cfg[n] | value;
            } else if (alias == 0xc00) {
                next = s->cfg[n] & ~value;
            } else {
                goto unsupported;
            }
            ack = (alias != 0xc00) && (value & CFG_IACK);
            if (next & 0x00077000) {
                qemu_log_mask(LOG_UNIMP,
                              "rp1: nonzero MSI-X function/traffic class\n");
            }
            s->cfg[n] = next & CFG_RW_MASK;
            if (ack) {
                s->blocked &= ~(1ULL << n);
                s->previous &= ~(1ULL << n);
            }
            rp1_update(s, n);
            return;
        }
        if (reg == 0x108 || reg == 0x10c) {
            return; /* Raw status is read-only, not write-to-clear. */
        }
    }
unsupported:
    qemu_log_mask(LOG_UNIMP, "rp1: unimplemented write at 0x%" HWADDR_PRIx "\n",
                  offset);
}

static const MemoryRegionOps rp1_ops = {
    .read = rp1_read,
    .write = rp1_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 4, .max_access_size = 4 },
    .impl = { .min_access_size = 4, .max_access_size = 4 },
};

static void rp1_reset(DeviceState *dev)
{
    RP1State *s = RP1(dev);

    memset(s->cfg, 0, sizeof(s->cfg));
    s->inputs = s->blocked = s->previous = 0;
    msix_reset(PCI_DEVICE(s));
    /* SRAM contents are not invented as a hardware reset guarantee. */
}

static void rp1_config_write(PCIDevice *dev, uint32_t address, uint32_t val,
                             int len)
{
    pci_default_write_config(dev, address, val, len);
    msix_write_config(dev, address, val, len);
}

static void rp1_init(Object *obj)
{
    RP1State *s = RP1(obj);

    for (unsigned n = 0; n < ARRAY_SIZE(s->uart); n++) {
        g_autofree char *name = g_strdup_printf("uart%u", n);
        s->uart[n] = qdev_new(TYPE_PL011);
        g_autofree char *child = g_strdup_printf("serial%u", n);
        object_property_add_child(obj, child, OBJECT(s->uart[n]));
        object_property_add_alias(obj, name, OBJECT(s->uart[n]), "chardev");
        object_unref(OBJECT(s->uart[n]));
    }
}

static void rp1_realize(PCIDevice *dev, Error **errp)
{
    RP1State *s = RP1(dev);

    memory_region_init(&s->bar0, OBJECT(s), "rp1-msix", 0x4000);
    memory_region_init(&s->peripherals, OBJECT(s), "rp1-peripherals", 0x400000);
    memory_region_init_io(&s->unimplemented, OBJECT(s), &rp1_ops, s,
                          "rp1-unimplemented", 0x400000);
    memory_region_add_subregion_overlap(&s->peripherals, 0,
                                        &s->unimplemented, -1);
    if (!memory_region_init_ram(&s->sram, OBJECT(s), "rp1-sram",
                                0x10000, errp)) {
        return;
    }
    pci_register_bar(dev, 0, PCI_BASE_ADDRESS_SPACE_MEMORY, &s->bar0);
    pci_register_bar(dev, 1, PCI_BASE_ADDRESS_SPACE_MEMORY, &s->peripherals);
    pci_register_bar(dev, 2, PCI_BASE_ADDRESS_SPACE_MEMORY, &s->sram);
    pcie_endpoint_cap_init(dev, 0x70);
    pci_set_byte(dev->config + PCI_INTERRUPT_PIN, 1);
    if (msix_init(dev, RP1_VECTORS, &s->bar0, 0, 0,
                  &s->bar0, 0, 0x2000, 0xb0, errp)) {
        return;
    }
    for (unsigned n = 0; n < RP1_VECTORS; n++) {
        msix_vector_use(dev, n);
    }
    qdev_init_gpio_in(DEVICE(s), rp1_input, RP1_VECTORS);
    for (unsigned n = 0; n < ARRAY_SIZE(s->uart); n++) {
        /* Linux RP1 binding advertises PrimeCell identity 0x00341011. */
        qdev_prop_set_uint32(s->uart[n], "fifo-depth", 32);
        qdev_prop_set_uint32(s->uart[n], "periph-id", 0x00341011);
        if (!sysbus_realize(SYS_BUS_DEVICE(s->uart[n]), errp)) {
            return;
        }
        memory_region_add_subregion(&s->peripherals, 0x30000 + 0x4000 * n,
            sysbus_mmio_get_region(SYS_BUS_DEVICE(s->uart[n]), 0));
        sysbus_connect_irq(SYS_BUS_DEVICE(s->uart[n]), 0,
            qdev_get_gpio_in(DEVICE(s), n == 0 ? 25 : 41 + n));
    }
}

static void rp1_exit(PCIDevice *dev)
{
    RP1State *s = RP1(dev);

    msix_uninit(dev, &s->bar0, &s->bar0);
    pcie_cap_exit(dev);
}

static const VMStateDescription vmstate_rp1 = {
    .name = TYPE_RP1,
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (const VMStateField[]) {
        VMSTATE_PCI_DEVICE(parent_obj, RP1State),
        VMSTATE_MSIX(parent_obj, RP1State),
        VMSTATE_UINT32_ARRAY(cfg, RP1State, RP1_CFG_REGS),
        VMSTATE_UINT64(inputs, RP1State),
        VMSTATE_UINT64(blocked, RP1State),
        VMSTATE_UINT64(previous, RP1State),
        VMSTATE_END_OF_LIST()
    }
};

static void rp1_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    PCIDeviceClass *pc = PCI_DEVICE_CLASS(klass);

    pc->realize = rp1_realize;
    pc->exit = rp1_exit;
    pc->config_write = rp1_config_write;
    pc->vendor_id = 0x1de4;
    pc->device_id = 0x0001;
    pc->revision = 2;
    pc->class_id = PCI_CLASS_NETWORK_ETHERNET;
    dc->desc = "Raspberry Pi RP1 endpoint (peripheral engines incomplete)";
    dc->vmsd = &vmstate_rp1;
    device_class_set_legacy_reset(dc, rp1_reset);
    set_bit(DEVICE_CATEGORY_BRIDGE, dc->categories);
}

static const TypeInfo rp1_type = {
    .name = TYPE_RP1,
    .parent = TYPE_PCI_DEVICE,
    .instance_size = sizeof(RP1State),
    .instance_init = rp1_init,
    .class_init = rp1_class_init,
    .interfaces = (const InterfaceInfo[]) {
        { INTERFACE_PCIE_DEVICE },
        { }
    },
};

static void rp1_register_types(void)
{
    type_register_static(&rp1_type);
}
type_init(rp1_register_types)
