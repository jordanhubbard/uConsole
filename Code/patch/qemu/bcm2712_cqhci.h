/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef HW_SD_BCM2712_CQHCI_H
#define HW_SD_BCM2712_CQHCI_H

#include "hw/sysbus.h"
#include "hw/sd/sdhci.h"

#define TYPE_BCM2712_CQHCI "bcm2712-cqhci"
OBJECT_DECLARE_SIMPLE_TYPE(BCM2712CQHCIState, BCM2712_CQHCI)

struct BCM2712CQHCIState {
    SysBusDevice parent_obj;
    DeviceState *sdhci;
    MemoryRegion iomem;
    qemu_irq irq;
    QEMUTimer *work_timer, *coal_timer;
    uint32_t regs[24];
    uint8_t order[32];
    uint32_t head, count, phase;
    int32_t active;
    bool faulted, halt_requested, dma64, sg_end, coal_fired;
    uint64_t task, sg_next, sg_address;
    uint32_t sg_left, blocks_left, block_offset;
    uint8_t block[512];
    uint32_t coal_count;
};

#endif
