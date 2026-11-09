/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef HW_MISC_BCM2712_MIP_H
#define HW_MISC_BCM2712_MIP_H
#include "hw/sysbus.h"
#define TYPE_BCM2712_MIP "bcm2712-mip"
OBJECT_DECLARE_SIMPLE_TYPE(BCM2712MIPState, BCM2712_MIP)
struct BCM2712MIPState {
    SysBusDevice parent_obj;
    MemoryRegion regs;
    uint32_t config[2], host_mask[2], vpu_mask[2];
    qemu_irq irq[64];
};
#endif
