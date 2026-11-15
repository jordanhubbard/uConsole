/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Optional eMMC command queue, included by sd.c after its card state.
 * Ordinary cards retain their existing protocol and migration wire format.
 * References: Linux mmc.h EXT_CSD_CMDQ_* and mmc/core.c task management.
 * Queue readiness follows accepted, range-checked tasks. All data still uses
 * the existing block backend and bounded multi-block card transfer engine.
 */

static void emmc_cmdq_finish(SDState *sd)
{
    if (sd->cmdq_active >= 0) {
        unsigned tag = sd->cmdq_active;
        sd->cmdq_pending &= ~(1u << tag);
        sd->cmdq_tasks[tag] = sd->cmdq_addresses[tag] = 0;
        sd->cmdq_active = -1;
    }
}

static sd_rsp_type_t emmc_cmdq_command(SDState *sd, SDRequest req)
{
    unsigned tag = (req.arg >> 16) & 31;
    uint32_t mask = 1u << tag;
    uint64_t address, length;
    bool read;

    if (!sd->cmdq_capable || !(sd->ext_csd[EMMC_CMDQ_MODE_EN] & 1) ||
        (sd->state != sd_transfer_state &&
         sd->state != sd_sendingdata_state &&
         sd->state != sd_receivingdata_state)) {
        return sd_illegal;
    }
    switch (req.cmd) {
    case 13:
        if (!sd_req_rca_same(sd, req) || !(req.arg & (1u << 15))) {
            return sd_illegal;
        }
        return sd_r1_task_status;
    case 44:
        /* Reliable/tagged/context/forced programming are not advertised. */
        if ((req.arg & ~0x409fffffu) || !(req.arg & 0xffff) ||
            (sd->cmdq_pending & mask)) {
            return sd_illegal;
        }
        sd->cmdq_params = req.arg;
        sd->cmdq_params_valid = true;
        return sd_r1;
    case 45:
        if (!sd->cmdq_params_valid) {
            return sd_illegal;
        }
        sd->cmdq_params_valid = false;
        tag = (sd->cmdq_params >> 16) & 31;
        mask = 1u << tag;
        address = (uint64_t)req.arg * 512;
        length = (sd->cmdq_params & 0xffffu) * 512u;
        if ((sd->cmdq_pending & mask) || address > sd->size ||
            length > sd->size - address) {
            sd->card_status |= ADDRESS_ERROR;
            return sd_r1;
        }
        sd->cmdq_tasks[tag] = sd->cmdq_params;
        sd->cmdq_addresses[tag] = req.arg;
        sd->cmdq_pending |= mask;
        return sd_r1;
    case 46:
    case 47:
        read = req.cmd == 46;
        if ((req.arg & ~0x001f0000u) || !(sd->cmdq_pending & mask) ||
            sd->cmdq_active != -1 || sd->state != sd_transfer_state ||
            read != !!(sd->cmdq_tasks[tag] & (1u << 30))) {
            return sd_illegal;
        }
        address = (uint64_t)sd->cmdq_addresses[tag] * 512;
        if (!read && (sd->wp_switch || (sd->csd[14] & 0x30) ||
                      sd_wp_addr(sd, address))) {
            sd->card_status |= WP_VIOLATION;
            return sd_r1;
        }
        if (!read) {
            length = (sd->cmdq_tasks[tag] & 0xffffu) * 512u;
            for (uint64_t pos = address; pos < address + length; pos += 512) {
                if (sd_wp_addr(sd, pos)) {
                    sd->card_status |= WP_VIOLATION;
                    return sd_r1;
                }
            }
        }
        sd->cmdq_active = tag;
        sd->state = read ? sd_sendingdata_state : sd_receivingdata_state;
        sd->data_start = address;
        sd->data_offset = 0;
        sd->blk_len = 512;
        sd->multi_blk_cnt = sd->cmdq_tasks[tag] & 0xffff;
        sd->blk_written = 0;
        return sd_r1;
    case 48:
        if (req.arg == 1) {
            mask = UINT32_MAX;
        } else if ((req.arg & ~0x001f000fu) || (req.arg & 15) != 2) {
            return sd_illegal;
        }
        if (sd->cmdq_active >= 0 && (mask & (1u << sd->cmdq_active))) {
            /* Complete blocks stay written; discard the partial FIFO. */
            sd->state = sd_transfer_state;
            sd->multi_blk_cnt = sd->data_offset = 0;
            memset(sd->data, 0, sizeof(sd->data));
            sd->cmdq_active = -1;
        }
        sd->cmdq_pending &= ~mask;
        for (unsigned i = 0; i < 32; i++) {
            if (mask & (1u << i)) {
                sd->cmdq_tasks[i] = sd->cmdq_addresses[i] = 0;
            }
        }
        return sd_r1b;
    default:
        return sd_illegal;
    }
}

static bool emmc_cmdq_needed(void *opaque)
{
    return SDMMC_COMMON(opaque)->cmdq_capable;
}

static int emmc_cmdq_post_load(void *opaque, int version)
{
    SDState *sd = opaque;
    if (!sd_is_emmc(sd) || !sd->cmdq_capable ||
        sd->cmdq_cookie != 0x434d5131 || sd->cmdq_active < -1 ||
        sd->cmdq_active > 31 || (sd->ext_csd[EMMC_CMDQ_MODE_EN] & ~1) ||
        (sd->cmdq_pending && !(sd->ext_csd[EMMC_CMDQ_MODE_EN] & 1)) ||
        (sd->cmdq_params_valid &&
         ((sd->cmdq_params & ~0x409fffffu) ||
          !(sd->cmdq_params & 0xffff)))) {
        return -EINVAL;
    }
    for (unsigned i = 0; i < 32; i++) {
        if (sd->cmdq_pending & (1u << i)) {
            uint64_t address = (uint64_t)sd->cmdq_addresses[i] * 512;
            uint64_t length = (sd->cmdq_tasks[i] & 0xffffu) * 512u;
            if (!length || (sd->cmdq_tasks[i] & ~0x409fffffu) ||
                ((sd->cmdq_tasks[i] >> 16) & 31) != i ||
                address > sd->size || length > sd->size - address) {
                return -EINVAL;
            }
        }
    }
    if (sd->cmdq_active >= 0) {
        unsigned tag = sd->cmdq_active;
        bool read = sd->cmdq_tasks[tag] & (1u << 30);
        if (!(sd->cmdq_pending & (1u << tag)) || sd->blk_len != 512 ||
            !sd->multi_blk_cnt ||
            sd->multi_blk_cnt > (sd->cmdq_tasks[tag] & 0xffff) ||
            sd->data_offset >= 512 || sd->current_cmd != (read ? 46 : 47) ||
            sd->state != (read ? sd_sendingdata_state : sd_receivingdata_state)) {
            return -EINVAL;
        }
    }
    sd->cmdq_incoming = true;
    return 0;
}

static int emmc_cmdq_profile_post_load(void *opaque, int version)
{
    SDState *sd = opaque;
    return sd->cmdq_incoming == sd->cmdq_capable ? 0 : -EINVAL;
}

static const VMStateDescription emmc_cmdq_vmstate = {
    .name = "sd-card/emmc-command-queue",
    .version_id = 1, .minimum_version_id = 1,
    .needed = emmc_cmdq_needed, .post_load = emmc_cmdq_post_load,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32(cmdq_cookie, SDState),
        VMSTATE_UINT32(cmdq_pending, SDState),
        VMSTATE_UINT32(cmdq_params, SDState),
        VMSTATE_UINT32_ARRAY(cmdq_tasks, SDState, 32),
        VMSTATE_UINT32_ARRAY(cmdq_addresses, SDState, 32),
        VMSTATE_INT32(cmdq_active, SDState),
        VMSTATE_BOOL(cmdq_params_valid, SDState),
        VMSTATE_END_OF_LIST()
    },
};
