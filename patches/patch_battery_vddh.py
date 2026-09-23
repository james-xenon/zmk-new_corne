#!/usr/bin/env python3
from pathlib import Path
import sys

if len(sys.argv) != 2:
    raise SystemExit(f"usage: {sys.argv[0]} /path/to/zmk/app/src/battery.c")

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")

# -----------------------------------------------------------------------------
# Nordic POWER HAL: lets us detect whether USB VBUS is physically present.
# -----------------------------------------------------------------------------
include_anchor = "#include <zephyr/drivers/sensor.h>\n"
include_insert = include_anchor + "\n#if defined(CONFIG_SOC_NRF52840)\n#include <hal/nrf_power.h>\n#endif\n"

if "#include <hal/nrf_power.h>" not in text:
    if include_anchor not in text:
        raise SystemExit("battery patch: include anchor not found")
    text = text.replace(include_anchor, include_insert, 1)

# -----------------------------------------------------------------------------
# State + dynamic relaxation detector.
# -----------------------------------------------------------------------------
state_anchor = "static uint8_t last_state_of_charge = 0;\n"
state_insert = r'''static uint8_t last_state_of_charge = 0;

/*
 * Eyelash/nice!nano VDDH battery workaround.
 *
 * zmk,battery-nrf-vddh measures the MCU VDDH rail. While USB is attached that
 * rail is not a trustworthy representation of the LiPo cell, and immediately
 * after charging the cell can also retain an elevated surface voltage.
 *
 * Behaviour for the lithium-voltage reporting path:
 *   1. While USB VBUS is present, keep the last trustworthy battery percentage.
 *   2. After USB removal, sample voltage but do not publish it immediately.
 *   3. Accept the battery as relaxed after 3 consecutive samples whose adjacent
 *      differences are <= 15 mV.
 *   4. Fail open after 15 minutes so ADC noise cannot freeze the value forever.
 */
static bool battery_has_valid_sample;
static bool battery_vbus_was_present;
static bool battery_post_charge_settling;
static int64_t battery_settle_started_at;
static int32_t battery_settle_prev_mv;
static uint8_t battery_settle_stable_samples;

#define ZMK_VDDH_SETTLE_DELTA_MV 15
#define ZMK_VDDH_SETTLE_STABLE_SAMPLES 3
#define ZMK_VDDH_SETTLE_MAX_MS (15 * 60 * 1000)

static bool zmk_battery_vbus_present(void) {
#if defined(CONFIG_SOC_NRF52840) && NRF_POWER_HAS_USBREG
    return nrf_power_usbregstatus_vbusdet_get(NRF_POWER);
#else
    return false;
#endif
}

static int32_t zmk_battery_abs_i32(int32_t value) { return value < 0 ? -value : value; }

/*
 * If we already have a trustworthy reading, do not replace it with VDDH while
 * USB is connected.
 */
static bool zmk_battery_hold_while_vbus(void) {
    if (!zmk_battery_vbus_present()) {
        return false;
    }

    battery_vbus_was_present = true;
    battery_post_charge_settling = false;
    battery_settle_stable_samples = 0;

    return battery_has_valid_sample;
}

/*
 * Decide whether a post-USB voltage sample is relaxed enough to publish.
 */
static bool zmk_battery_accept_vddh_sample(int32_t measured_mv) {
#if defined(CONFIG_SOC_NRF52840) && NRF_POWER_HAS_USBREG
    const bool vbus_present = zmk_battery_vbus_present();
    const int64_t now = k_uptime_get();

    if (vbus_present) {
        battery_vbus_was_present = true;
        battery_post_charge_settling = false;
        battery_settle_stable_samples = 0;

        /*
         * On a boot that happens while USB is already connected there is no
         * trustworthy previous value in RAM. Allow the legacy reading once so
         * the battery level is not stuck at 0%. After USB removal it will be
         * replaced only after dynamic relaxation completes.
         */
        return !battery_has_valid_sample;
    }

    if (battery_vbus_was_present) {
        battery_vbus_was_present = false;
        battery_post_charge_settling = true;
        battery_settle_started_at = now;
        battery_settle_prev_mv = measured_mv;
        battery_settle_stable_samples = 1;
        LOG_DBG("Battery relaxation started at %d mV", measured_mv);
        return false;
    }

    if (battery_post_charge_settling) {
        const int32_t delta_mv = zmk_battery_abs_i32(measured_mv - battery_settle_prev_mv);

        if (delta_mv <= ZMK_VDDH_SETTLE_DELTA_MV) {
            if (battery_settle_stable_samples < 255) {
                battery_settle_stable_samples++;
            }
        } else {
            battery_settle_stable_samples = 1;
        }

        battery_settle_prev_mv = measured_mv;

        const bool stable =
            battery_settle_stable_samples >= ZMK_VDDH_SETTLE_STABLE_SAMPLES;
        const bool timed_out =
            (now - battery_settle_started_at) >= ZMK_VDDH_SETTLE_MAX_MS;

        if (!stable && !timed_out) {
            LOG_DBG("Battery still relaxing: %d mV, delta %d mV, stable samples %u",
                    measured_mv, delta_mv, battery_settle_stable_samples);
            return false;
        }

        LOG_DBG("Battery relaxation complete: %d mV (%s)", measured_mv,
                stable ? "stable" : "timeout");
        battery_post_charge_settling = false;
        battery_settle_stable_samples = 0;
    }
#else
    ARG_UNUSED(measured_mv);
#endif

    return true;
}
'''

if "zmk_battery_accept_vddh_sample" not in text:
    if state_anchor not in text:
        raise SystemExit("battery patch: state anchor not found")
    text = text.replace(state_anchor, state_insert, 1)

# -----------------------------------------------------------------------------
# Patch the actual VDDH path used by ZMK 0.3.0:
# CONFIG_ZMK_BATTERY_REPORTING_FETCH_MODE_LITHIUM_VOLTAGE / SENSOR_CHAN_VOLTAGE
# -----------------------------------------------------------------------------
voltage_branch_anchor = '''#elif IS_ENABLED(CONFIG_ZMK_BATTERY_REPORTING_FETCH_MODE_LITHIUM_VOLTAGE)\n    rc = sensor_sample_fetch_chan(battery, SENSOR_CHAN_VOLTAGE);\n'''
voltage_branch_insert = '''#elif IS_ENABLED(CONFIG_ZMK_BATTERY_REPORTING_FETCH_MODE_LITHIUM_VOLTAGE)\n    if (zmk_battery_hold_while_vbus()) {\n        LOG_DBG("Holding battery level at %u while USB is connected", last_state_of_charge);\n        return 0;\n    }\n\n    rc = sensor_sample_fetch_chan(battery, SENSOR_CHAN_VOLTAGE);\n'''

if "Holding battery level at %u while USB is connected" not in text:
    if voltage_branch_anchor not in text:
        raise SystemExit("battery patch: lithium-voltage branch anchor not found")
    text = text.replace(voltage_branch_anchor, voltage_branch_insert, 1)

mv_anchor = '''    uint16_t mv = voltage.val1 * 1000 + (voltage.val2 / 1000);\n    state_of_charge.val1 = lithium_ion_mv_to_pct(mv);\n'''
mv_insert = '''    uint16_t mv = voltage.val1 * 1000 + (voltage.val2 / 1000);\n\n    if (!zmk_battery_accept_vddh_sample(mv)) {\n        return 0;\n    }\n\n    state_of_charge.val1 = lithium_ion_mv_to_pct(mv);\n    battery_has_valid_sample = true;\n'''

if "battery_has_valid_sample = true;" not in text:
    if mv_anchor not in text:
        raise SystemExit("battery patch: voltage conversion anchor not found")
    text = text.replace(mv_anchor, mv_insert, 1)

path.write_text(text, encoding="utf-8")
print(f"dynamic VDDH battery patch applied: {path}")