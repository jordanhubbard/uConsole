/* SPDX-License-Identifier: GPL-2.0-or-later */
/* BCM2712 MIP: selected host-edge register/message mode. Register semantics
 * follow the enrolled firmware FDT and raspberrypi/linux irq-bcm2712-mip.c.
 * No PCI bus-to-CPU address alias, VPU delivery or level-mode success is
 * fabricated. A PCI endpoint must reach the message register through its
 * actual host bridge translation; merely mapping a CPU address is not DMA.
 */
#include "qemu/osdep.h"
#include "qemu/module.h"
#include "hw/irq.h"
#include "hw/misc/bcm2712_mip.h"
#include "migration/vmstate.h"

static MemTxResult mip_read(void *opaque, hwaddr off, uint64_t *value,
                            unsigned size, MemTxAttrs attrs)
{
    BCM2712MIPState *s = opaque;
    if (size != 4 || (off & 3)) {
        return MEMTX_ERROR;
    }
    switch (off) {
    case 0: case 0x10: case 0x80: case 0x90: case 0xa0: case 0xb0:
        *value = 0; /* host-edge mode has no held interrupt status */
        return MEMTX_OK;
    case 0x20: case 0x30:
        *value = s->config[off == 0x30];
        return MEMTX_OK;
    case 0x40: case 0x50:
        *value = s->host_mask[off == 0x50];
        return MEMTX_OK;
    case 0x60: case 0x70:
        *value = s->vpu_mask[off == 0x70];
        return MEMTX_OK;
    default:
        return MEMTX_ERROR;
    }
}
static MemTxResult mip_write(void *opaque, hwaddr off, uint64_t value,
                             unsigned size, MemTxAttrs attrs)
{
    BCM2712MIPState *s = opaque;
    if (size != 4 || (off & 3)) {
        return MEMTX_ERROR;
    }
    switch (off) {
    case 0:
        if (value >= 64) {
            return MEMTX_ERROR;
        }
        unsigned bank = value / 32, bit = value % 32;
        if (!(s->config[bank] & (1U << bit)) ||
            !(s->vpu_mask[bank] & (1U << bit))) {
            return MEMTX_ERROR;
        }
        if (!(s->host_mask[bank] & (1U << bit))) {
            qemu_irq_pulse(s->irq[value]);
        }
        return MEMTX_OK;
    case 0x10:
        return value < 64 ? MEMTX_OK : MEMTX_ERROR;
    case 0x20: case 0x30:
        s->config[off == 0x30] = value;
        return MEMTX_OK;
    case 0x40: case 0x50:
        s->host_mask[off == 0x50] = value;
        return MEMTX_OK;
    case 0x60: case 0x70:
        s->vpu_mask[off == 0x70] = value;
        return MEMTX_OK;
    default:
        return MEMTX_ERROR;
    }
}
static const MemoryRegionOps mip_ops = {
    .read_with_attrs = mip_read, .write_with_attrs = mip_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 4, .valid.max_access_size = 4,
    .impl.min_access_size = 4, .impl.max_access_size = 4,
};
static void mip_reset(DeviceState *dev)
{
    BCM2712MIPState *s = BCM2712_MIP(dev);
    for (int i = 0; i < 2; i++) {
        s->config[i] = 0;
        s->host_mask[i] = s->vpu_mask[i] = UINT32_MAX;
    }
}
static const VMStateDescription vmstate_mip = {
    .name = TYPE_BCM2712_MIP, .version_id = 1, .minimum_version_id = 1,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32_ARRAY(config, BCM2712MIPState, 2),
        VMSTATE_UINT32_ARRAY(host_mask, BCM2712MIPState, 2),
        VMSTATE_UINT32_ARRAY(vpu_mask, BCM2712MIPState, 2),
        VMSTATE_END_OF_LIST()
    }
};
static void mip_init(Object *obj)
{
    BCM2712MIPState *s = BCM2712_MIP(obj);
    memory_region_init_io(&s->regs, obj, &mip_ops, s, TYPE_BCM2712_MIP, 0xc0);
    sysbus_init_mmio(SYS_BUS_DEVICE(s), &s->regs);
    for (int i = 0; i < 64; i++) {
        sysbus_init_irq(SYS_BUS_DEVICE(s), &s->irq[i]);
    }
}
static void mip_class_init(ObjectClass *oc, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    dc->vmsd = &vmstate_mip;
    device_class_set_legacy_reset(dc, mip_reset);
}
static const TypeInfo mip_type = {
    .name = TYPE_BCM2712_MIP, .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(BCM2712MIPState), .instance_init = mip_init,
    .class_init = mip_class_init,
};
static void mip_register_types(void) { type_register_static(&mip_type); }
type_init(mip_register_types)
