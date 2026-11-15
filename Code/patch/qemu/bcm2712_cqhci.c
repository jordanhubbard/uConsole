/* SPDX-License-Identifier: GPL-2.0-or-later */
/*
 * BCM2712 CQHCI host extension. Register/descriptor interface: the pinned
 * raspberrypi/linux cqhci.h and cqhci-core.c; SD/MMC commands go through the
 * actual QEMU card bus. Unsupported card commands return errors, never data.
 *
 * The virtual integration selects CQHCI version 5.10, no inline crypto and
 * a reported 1 MHz internal timer. These are model parameters, not silicon
 * frequency measurements. Transport/PHY and analog timing are not modeled.
 */
#include "qemu/osdep.h"
#include "qapi/error.h"
#include "qemu/bswap.h"
#include "qemu/log.h"
#include "qemu/module.h"
#include "qemu/timer.h"
#include "hw/irq.h"
#include "hw/qdev-properties.h"
#include "hw/sd/bcm2712_cqhci.h"
#include "migration/vmstate.h"
#include "system/dma.h"
#include "sdhci-internal.h"

enum {
    VER = 0, CAP = 4, CFG = 8, CTL = 12, IS = 16, ISTE = 20,
    ISGE = 24, IC = 28, TDLBA = 32, TDLBAU = 36, TDBR = 40,
    TCN = 44, DQS = 48, DPT = 52, TCLR = 56, TDPE = 60,
    SSC1 = 64, SSC2 = 68, CRDCT = 72, RMEM = 80,
    TERRI = 84, CRI = 88, CRA = 92,
};
enum { IDLE, PARAMS, ADDRESS, POLL, EXECUTE, DATA };
#define R(s, offset) ((s)->regs[(offset) / 4])
#define ENABLE 1u
#define DESC128 (1u << 8)
#define DCMD (1u << 12)
#define HALT 1u
#define HAC 1u
#define TCC 2u
#define RED 4u
#define TCL 8u
#define ICCE 32u
#define IC_ENABLE (1u << 31)
#define IC_RESET (1u << 16)
#define IC_STATUS (1u << 20)
#define CQ_VERSION 0x510u
#define CQ_CAP 0x1064u /* 100 * 10 kHz; no crypto */
#define TICK_NS 1000
#define CQ_DATA_TIMEOUT (1u << 4) /* SDHCI Error Interrupt Status: data timeout */

static SDHCIState *host(BCM2712CQHCIState *s)
{
    return SYSBUS_SDHCI(s->sdhci);
}

static void update_irq(BCM2712CQHCIState *s)
{
    qemu_set_irq(s->irq, (R(s, IS) & R(s, ISGE)) != 0);
}

static void event(BCM2712CQHCIState *s, uint32_t bits)
{
    R(s, IS) |= bits & R(s, ISTE);
    update_irq(s);
}

static void schedule(BCM2712CQHCIState *s, int64_t delay)
{
    if ((R(s, CFG) & ENABLE) && !(R(s, CTL) & HALT) &&
        !s->faulted && s->count) {
        timer_mod(s->work_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
                  MAX(delay, TICK_NS));
    }
}

static void enter_halt(BCM2712CQHCIState *s)
{
    if (!(R(s, CTL) & HALT)) {
        R(s, CTL) |= HALT;
        event(s, HAC);
    }
    s->halt_requested = false;
    timer_del(s->work_timer);
}

static void pop_task(BCM2712CQHCIState *s)
{
    uint32_t bit = 1u << s->active;
    R(s, TDBR) &= ~bit;
    R(s, DPT) &= ~bit;
    R(s, DQS) &= ~bit;
    s->head = (s->head + 1) & 31;
    s->count--;
    s->active = -1;
    s->phase = IDLE;
    s->sg_left = s->block_offset = 0;
}

static void fail_task(BCM2712CQHCIState *s, unsigned cmd,
                      bool data_error, bool descriptor_error)
{
    SDHCIState *h = host(s);
    unsigned tag = s->active;
    uint16_t error = descriptor_error ? SDHC_EIS_ADMAERR :
                     data_error ? CQ_DATA_TIMEOUT : SDHC_EIS_CMDTIMEOUT;

    R(s, TERRI) = data_error ? (1u << 31) | (tag << 24) | (cmd << 16) :
                              (1u << 15) | (tag << 8) | cmd;
    if (descriptor_error) {
        R(s, TDPE) |= 1u << tag;
    }
    /* Preserve the SDHCI error path as well as CQIS/TERRI. */
    h->errintsts |= error & h->errintstsen;
    if (h->errintsts) {
        h->norintsts |= SDHC_NIS_ERR;
    }
    pop_task(s);
    s->faulted = true;
    timer_del(s->work_timer);
    event(s, RED);
    if (s->halt_requested) {
        enter_halt(s);
    }
}

/* Descriptor and payload DMA must not reenter device register callbacks. */
static const MemTxAttrs dma_attrs = { .memory = true };

static bool dma_read(BCM2712CQHCIState *s, uint64_t addr,
                     void *data, size_t length)
{
    return addr <= UINT64_MAX - length &&
           (s->dma64 || addr + length <= (1ULL << 32)) &&
           address_space_read(host(s)->dma_as, addr, dma_attrs,
                              data, length) == MEMTX_OK;
}

static bool command(BCM2712CQHCIState *s, unsigned cmd, uint32_t arg,
                    bool no_response, bool queue_status, uint32_t *value)
{
    SDHCIState *h = host(s);
    SDRequest request = { .cmd = cmd, .arg = arg };
    uint8_t response[16] = {};
    size_t length;

    R(s, CRI) = cmd;
    R(s, CRA) = 0;
    if (!(h->pwrcon & SDHC_POWER_ON) ||
        !(h->clkcon & SDHC_CLOCK_SDCLK_EN)) {
        fail_task(s, cmd, false, false);
        return false;
    }
    length = sdbus_do_command(&h->sdbus, &request, response, sizeof(response));
    if (no_response && length == 0) {
        *value = 0;
        return true;
    }
    if (length != 4) {
        fail_task(s, cmd, false, false);
        return false;
    }
    *value = ldl_be_p(response);
    R(s, CRA) = *value;
    if (!queue_status && (*value & R(s, RMEM))) {
        fail_task(s, cmd, false, false);
        return false;
    }
    return true;
}

static void coalesced_interrupt(void *opaque)
{
    BCM2712CQHCIState *s = opaque;
    s->coal_fired = true;
    event(s, TCC);
}

static void completed(BCM2712CQHCIState *s)
{
    bool interrupt = s->task & 4;
    bool direct = s->active == 31 && (R(s, CFG) & DCMD);
    R(s, TCN) |= 1u << s->active;
    pop_task(s);
    if (interrupt) {
        event(s, TCC);
    } else if (!direct && (R(s, IC) & IC_ENABLE)) {
        unsigned threshold = (R(s, IC) >> 8) & 31;
        unsigned timeout = R(s, IC) & 127;
        if (!s->coal_fired && s->coal_count < 31) {
            s->coal_count++;
        }
        if (!s->coal_fired && timeout && !timer_pending(s->coal_timer)) {
            timer_mod(s->coal_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
                      (int64_t)timeout * 1024 * TICK_NS);
        }
        if (!s->coal_fired && threshold && s->coal_count >= threshold) {
            timer_del(s->coal_timer);
            coalesced_interrupt(s);
        }
    }
    if (s->halt_requested) {
        enter_halt(s);
    } else {
        schedule(s, TICK_NS);
    }
}

/* One descriptor or one SD block per virtual tick keeps HALT/migration fair. */
static void work(void *opaque)
{
    BCM2712CQHCIState *s = opaque;
    SDHCIState *h = host(s);
    uint8_t descriptor[16] = {};
    uint32_t response;
    uint64_t base;
    unsigned task_size, link_size, cmd;

    if (!(R(s, CFG) & ENABLE) || (R(s, CTL) & HALT) ||
        s->faulted || !s->count) {
        return;
    }
    if (s->phase == IDLE) {
        s->active = s->order[s->head];
        s->dma64 = SDHC_DMA_TYPE(h->hostctl1) == SDHC_CTRL_ADMA2_64;
        if (s->dma64 && !(h->capareg & (1ULL << 28))) {
            fail_task(s, 0, false, true);
            return;
        }
        task_size = (R(s, CFG) & DESC128) ? 16 : 8;
        link_size = s->dma64 ? 16 : 8;
        base = ((uint64_t)R(s, TDLBAU) << 32) | R(s, TDLBA);
        if (base > UINT64_MAX - (task_size + link_size) * 32 ||
            !dma_read(s, base + s->active * (task_size + link_size),
                      descriptor, task_size)) {
            fail_task(s, 0, false, true);
            return;
        }
        s->task = ldq_le_p(descriptor);
        if ((s->task & 0x3b) != 0x2b) { /* VALID, END, ACT=TASK */
            fail_task(s, 0, false, true);
            return;
        }
        if (task_size == 16 && ldq_le_p(descriptor + 8)) {
            /* Inline crypto is not advertised. Never fall back to plaintext. */
            R(s, TDPE) |= 1u << s->active;
            event(s, ICCE);
            fail_task(s, 0, false, true);
            return;
        }
        if (s->active == 31 && (R(s, CFG) & DCMD)) {
            unsigned response_type = (s->task >> 23) & 3;
            cmd = (s->task >> 16) & 63;
            if (response_type == 1) {
                fail_task(s, cmd, false, true);
                return;
            }
            if (command(s, cmd, s->task >> 32, response_type == 0,
                        false, &response)) {
                R(s, CRDCT) = response;
                completed(s);
            }
            return;
        }
        s->blocks_left = (s->task >> 16) & 0xffff;
        if (!s->blocks_left ||
            !dma_read(s, base + s->active * (task_size + link_size) + task_size,
                      descriptor, link_size) ||
            (ldl_le_p(descriptor) & 0x39) != 0x31) {
            fail_task(s, 0, false, true);
            return;
        }
        s->sg_next = s->dma64 ? ldq_le_p(descriptor + 4) : ldl_le_p(descriptor + 4);
        s->sg_left = s->block_offset = 0;
        s->phase = PARAMS;
    } else if (s->phase == PARAMS) {
        uint32_t params = ((s->task >> 15) & 1) << 31 |
                          ((s->task >> 12) & 1) << 30 |
                          ((s->task >> 11) & 1) << 29 |
                          ((s->task >> 7) & 15) << 25 |
                          ((s->task >> 6) & 1) << 24 |
                          ((s->task >> 13) & 1) << 23 |
                          (s->active << 16) | s->blocks_left;
        if (!command(s, 44, params, false, false, &response)) {
            return;
        }
        s->phase = ADDRESS;
    } else if (s->phase == ADDRESS) {
        if (!command(s, 45, s->task >> 32, false, false, &response)) {
            return;
        }
        R(s, DPT) |= 1u << s->active;
        s->phase = POLL;
    } else if (s->phase == POLL) {
        if (!command(s, 13, (R(s, SSC2) << 16) | (1u << 15),
                     false, true, &response)) {
            return;
        }
        R(s, DQS) = response;
        if (!(response & (1u << s->active))) {
            schedule(s, MAX(R(s, SSC1) & 0xffff, 1u) * (int64_t)TICK_NS);
            return;
        }
        s->phase = EXECUTE;
    } else if (s->phase == EXECUTE) {
        cmd = (s->task & (1u << 12)) ? 46 : 47;
        if (!command(s, cmd, s->active << 16, false, false, &response)) {
            return;
        }
        s->phase = DATA;
    } else if (s->phase == DATA) {
        bool read = s->task & (1u << 12);
        unsigned amount, attr;
        if (s->block_offset == 0 && read) {
            if (!sdbus_data_ready(&h->sdbus)) {
                fail_task(s, 46, true, false);
                return;
            }
            sdbus_read_data(&h->sdbus, s->block, sizeof(s->block));
        }
        if (s->sg_left == 0) {
            if (!dma_read(s, s->sg_next, descriptor, s->dma64 ? 16 : 8)) {
                fail_task(s, read ? 46 : 47, true, true);
                return;
            }
            attr = ldl_le_p(descriptor);
            if ((attr & 0x39) != 0x21) { /* VALID, ACT=TRANSFER */
                fail_task(s, read ? 46 : 47, true, true);
                return;
            }
            s->sg_left = (attr >> 16) ? (attr >> 16) : 65536;
            s->sg_end = attr & 2;
            s->sg_address = s->dma64 ? ldq_le_p(descriptor + 4) : ldl_le_p(descriptor + 4);
            s->sg_next += s->dma64 ? 16 : 8;
        }
        amount = MIN(s->sg_left, 512 - s->block_offset);
        if (s->sg_address > UINT64_MAX - amount ||
            (!s->dma64 && s->sg_address + amount > (1ULL << 32)) ||
            (read ? address_space_write(h->dma_as, s->sg_address, dma_attrs,
                                        s->block + s->block_offset, amount) != MEMTX_OK :
                    !dma_read(s, s->sg_address, s->block + s->block_offset, amount))) {
            fail_task(s, read ? 46 : 47, true, true);
            return;
        }
        s->sg_left -= amount;
        s->sg_address += amount;
        s->block_offset += amount;
        if (s->block_offset == 512) {
            if (!read) {
                if (!sdbus_receive_ready(&h->sdbus)) {
                    fail_task(s, 47, true, false);
                    return;
                }
                sdbus_write_data(&h->sdbus, s->block, sizeof(s->block));
            }
            s->block_offset = 0;
            if (--s->blocks_left == 0) {
                if (!s->sg_end || s->sg_left) {
                    fail_task(s, read ? 46 : 47, true, true);
                } else {
                    completed(s);
                }
                return;
            }
        }
        if (!s->sg_left && s->sg_end) {
            fail_task(s, read ? 46 : 47, true, true);
            return;
        }
    }
    schedule(s, TICK_NS);
}

static void clear_tasks(BCM2712CQHCIState *s, uint32_t mask)
{
    uint8_t remaining[32] = {};
    unsigned count = 0;
    for (unsigned i = 0; i < s->count; i++) {
        unsigned tag = s->order[(s->head + i) & 31];
        if (!(mask & (1u << tag))) {
            remaining[count++] = tag;
        }
    }
    memcpy(s->order, remaining, sizeof(remaining));
    s->head = 0;
    s->count = count;
    R(s, TDBR) &= ~mask;
    R(s, DPT) &= ~mask;
    R(s, DQS) &= ~mask;
    s->faulted = false;
    event(s, TCL);
}

static void cancel_queue(BCM2712CQHCIState *s)
{
    /* Disabling CQE clears its doorbells, not the card's command queue.
     * In particular, it cannot generate TCN/TCC or restart stale DMA when
     * software next enables the host. Already transferred bytes stay put.
     */
    timer_del(s->work_timer);
    R(s, TDBR) = R(s, DPT) = R(s, DQS) = 0;
    memset(s->order, 0, sizeof(s->order));
    memset(s->block, 0, sizeof(s->block));
    s->head = s->count = 0;
    s->active = -1;
    s->phase = IDLE;
    s->task = s->sg_next = s->sg_address = 0;
    s->sg_left = s->blocks_left = s->block_offset = 0;
    s->faulted = s->halt_requested = false;
    s->dma64 = s->sg_end = false;
}

static uint64_t read_reg(void *opaque, hwaddr offset, unsigned size)
{
    BCM2712CQHCIState *s = opaque;
    if (offset == TCLR || offset == 0x4c) {
        return 0;
    }
    if (offset == IC) {
        return R(s, IC) | (s->coal_count ? IC_STATUS : 0);
    }
    return R(s, offset);
}

static void write_reg(void *opaque, hwaddr offset, uint64_t value, unsigned size)
{
    BCM2712CQHCIState *s = opaque;
    uint32_t v = value;
    switch (offset) {
    case CFG:
        if (R(s, CFG) & ENABLE) {
            R(s, CFG) = (R(s, CFG) & ~ENABLE) | (v & ENABLE);
        } else {
            R(s, CFG) = v & (ENABLE | DESC128 | DCMD);
        }
        if (!(R(s, CFG) & ENABLE)) {
            cancel_queue(s);
        } else {
            schedule(s, TICK_NS);
        }
        break;
    case CTL:
        if ((v & (1u << 8)) && (R(s, CTL) & HALT)) {
            clear_tasks(s, UINT32_MAX);
        }
        if (v & HALT) {
            s->halt_requested = true;
            if (s->active == -1) {
                enter_halt(s);
            }
        } else {
            R(s, CTL) &= ~HALT;
            s->halt_requested = false;
            schedule(s, TICK_NS);
        }
        break;
    case IS:
        R(s, IS) &= ~v;
        update_irq(s);
        break;
    case ISTE:
        R(s, ISTE) = v & 0x3f;
        R(s, IS) &= R(s, ISTE);
        update_irq(s);
        break;
    case ISGE:
        R(s, ISGE) = v & 0x3f;
        update_irq(s);
        break;
    case IC:
        R(s, IC) = (R(s, IC) & ~IC_ENABLE) | (v & IC_ENABLE);
        if (v & (1u << 15)) {
            R(s, IC) = (R(s, IC) & ~0x1f00u) | (v & 0x1f00);
        }
        if (v & (1u << 7)) {
            R(s, IC) = (R(s, IC) & ~127u) | (v & 127);
        }
        if ((v & IC_RESET) || !(v & IC_ENABLE)) {
            s->coal_count = 0;
            s->coal_fired = false;
            timer_del(s->coal_timer);
        }
        break;
    case TDLBA:
    case TDLBAU:
        if (!(R(s, CFG) & ENABLE)) {
            R(s, offset) = offset == TDLBA ? v & ~1023u : v;
        }
        break;
    case TDBR:
        if (!(R(s, CFG) & ENABLE)) {
            break;
        }
        v &= ~R(s, TDBR);
        for (unsigned i = 0; i < 32; i++) {
            if (v & (1u << i)) {
                s->order[(s->head + s->count) & 31] = i;
                s->count++;
                R(s, TDBR) |= 1u << i;
            }
        }
        schedule(s, TICK_NS);
        break;
    case TCN:
        R(s, TCN) &= ~v;
        break;
    case TCLR:
        if (R(s, CTL) & HALT) {
            clear_tasks(s, v);
        }
        break;
    case SSC1:
        R(s, SSC1) = v & 0xfffff;
        break;
    case SSC2:
        R(s, SSC2) = v & 0xffff;
        break;
    case RMEM:
        R(s, RMEM) = v;
        break;
    default:
        qemu_log_mask(LOG_UNIMP, "bcm2712 CQHCI read-only/reserved write +0x%" HWADDR_PRIx "\n", offset);
        break;
    }
}

static const MemoryRegionOps ops = {
    .read = read_reg, .write = write_reg, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 4, .valid.max_access_size = 4,
    .impl.min_access_size = 4, .impl.max_access_size = 4,
};

static void reset(DeviceState *dev)
{
    BCM2712CQHCIState *s = BCM2712_CQHCI(dev);
    timer_del(s->work_timer);
    timer_del(s->coal_timer);
    memset(s->regs, 0, sizeof(s->regs));
    memset(s->order, 0, sizeof(s->order));
    memset(s->block, 0, sizeof(s->block));
    R(s, VER) = CQ_VERSION;
    R(s, CAP) = CQ_CAP;
    R(s, RMEM) = 0xfdf9a080;
    s->head = s->count = s->phase = 0;
    s->active = -1;
    s->faulted = s->halt_requested = s->dma64 = s->sg_end = s->coal_fired = false;
    s->task = s->sg_next = s->sg_address = 0;
    s->sg_left = s->blocks_left = s->block_offset = s->coal_count = 0;
    update_irq(s);
}

static void reset_input(void *opaque, int n, int level)
{
    if (level) {
        reset(DEVICE(opaque));
    }
}

static int post_load(void *opaque, int version)
{
    BCM2712CQHCIState *s = opaque;
    uint32_t mask = 0;
    if (R(s, VER) != CQ_VERSION || R(s, CAP) != CQ_CAP ||
        s->count > 32 || s->head > 31 || s->phase > DATA ||
        s->active < -1 || s->active > 31 || s->sg_left > 65536 ||
        s->blocks_left > 65535 || s->block_offset > 511 || s->coal_count > 31) {
        return -EINVAL;
    }
    for (unsigned i = 0; i < s->count; i++) {
        unsigned tag = s->order[(s->head + i) & 31];
        if (tag > 31 || (mask & (1u << tag))) {
            return -EINVAL;
        }
        mask |= 1u << tag;
    }
    if (mask != R(s, TDBR) ||
        (s->active != -1 && (!s->count || s->order[s->head] != s->active)) ||
        ((s->phase == IDLE) != (s->active == -1))) {
        return -EINVAL;
    }
    update_irq(s);
    return 0;
}

static const VMStateDescription vmstate = {
    .name = TYPE_BCM2712_CQHCI, .version_id = 1, .minimum_version_id = 1,
    .post_load = post_load,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32_ARRAY(regs, BCM2712CQHCIState, 24),
        VMSTATE_UINT8_ARRAY(order, BCM2712CQHCIState, 32),
        VMSTATE_UINT32(head, BCM2712CQHCIState),
        VMSTATE_UINT32(count, BCM2712CQHCIState),
        VMSTATE_UINT32(phase, BCM2712CQHCIState),
        VMSTATE_INT32(active, BCM2712CQHCIState),
        VMSTATE_BOOL(faulted, BCM2712CQHCIState),
        VMSTATE_BOOL(halt_requested, BCM2712CQHCIState),
        VMSTATE_BOOL(dma64, BCM2712CQHCIState),
        VMSTATE_BOOL(sg_end, BCM2712CQHCIState),
        VMSTATE_BOOL(coal_fired, BCM2712CQHCIState),
        VMSTATE_UINT64(task, BCM2712CQHCIState),
        VMSTATE_UINT64(sg_next, BCM2712CQHCIState),
        VMSTATE_UINT64(sg_address, BCM2712CQHCIState),
        VMSTATE_UINT32(sg_left, BCM2712CQHCIState),
        VMSTATE_UINT32(blocks_left, BCM2712CQHCIState),
        VMSTATE_UINT32(block_offset, BCM2712CQHCIState),
        VMSTATE_UINT8_ARRAY(block, BCM2712CQHCIState, 512),
        VMSTATE_UINT32(coal_count, BCM2712CQHCIState),
        VMSTATE_TIMER_PTR(work_timer, BCM2712CQHCIState),
        VMSTATE_TIMER_PTR(coal_timer, BCM2712CQHCIState),
        VMSTATE_END_OF_LIST()
    },
};

static const Property properties[] = {
    DEFINE_PROP_LINK("sdhci", BCM2712CQHCIState, sdhci, TYPE_SYSBUS_SDHCI, DeviceState *),
};

static void realize(DeviceState *dev, Error **errp)
{
    BCM2712CQHCIState *s = BCM2712_CQHCI(dev);
    if (!s->sdhci || !host(s)->dma_as || !host(s)->cqhci_capable) {
        error_setg(errp, "CQHCI requires a realized SDHCI host");
    }
}

static void init(Object *obj)
{
    BCM2712CQHCIState *s = BCM2712_CQHCI(obj);
    memory_region_init_io(&s->iomem, obj, &ops, s, "bcm2712-cqhci", 0x60);
    sysbus_init_mmio(SYS_BUS_DEVICE(s), &s->iomem);
    sysbus_init_irq(SYS_BUS_DEVICE(s), &s->irq);
    qdev_init_gpio_in(DEVICE(s), reset_input, 1);
    s->work_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, work, s);
    s->coal_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, coalesced_interrupt, s);
    s->active = -1;
}

static void finalize(Object *obj)
{
    BCM2712CQHCIState *s = BCM2712_CQHCI(obj);
    timer_free(s->work_timer);
    timer_free(s->coal_timer);
}

static void class_init(ObjectClass *oc, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    dc->vmsd = &vmstate;
    dc->realize = realize;
    device_class_set_props(dc, properties);
    device_class_set_legacy_reset(dc, reset);
}

static const TypeInfo type = {
    .name = TYPE_BCM2712_CQHCI, .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(BCM2712CQHCIState), .instance_init = init,
    .instance_finalize = finalize, .class_init = class_init,
};
static void register_types(void) { type_register_static(&type); }
type_init(register_types)
