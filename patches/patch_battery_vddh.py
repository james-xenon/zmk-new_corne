#!/usr/bin/env python3
from pathlib import Path
import sys

if len(sys.argv) != 2:
    raise SystemExit(f"usage: {sys.argv[0]} /path/to/zmk/app/src/battery.c")

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")

include_anchor = "#include <zephyr/drivers/sensor.h>\n"
include_insert = include_anchor + "\n#if defined(CONFIG_SOC_NRF52840)\n#include <hal/nrf_power.h>\n#endif\n"
if "#include <hal/nrf_power.h>" not in text:
    if include_anchor not in text:
        raise SystemExit("battery patch: include anchor not found")
    text = text.replace(include_anchor, include_insert, 1)

state_anchor = "static uint8_t last_state_of_charge = 0;\n"
state_insert = r'''static uint8_t last_state_of_charge = 0;

/*
 * Eyelash/nice!nano VDDH battery workaround with dynamic post-charge settling.
 *
 * Why this exists:
 * - The board uses zmk,battery-nrf-vddh.
 * - While USB is connected, VDDH may not represent the LiPo cell voltage.
 * - Immediately after charging, the cell voltage can also be temporarily high.
 *
 * Behaviour:
 * 1. While VBUS is present, preserve the last trustworthy battery value.
 * 2. After VBUS is removed, keep sampling but do not publish immediately.
 * 3. Consider the battery relaxed when three consecutive voltage samples are
 *    within 15 mV of each other.
 * 4. Fail open after 15 minutes so a noisy ADC can never freeze the displayed
 *    battery level indefinitely.
 *
 * With ZMK v0.3.0's default 60 s battery report interval, a cleanly settling
 * battery normally needs roughly 2-3 minutes, but it may take longer when the
 * voltage is still relaxing. This is intentionally dynamic rather than a fixed
 * delay.
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
 * Called before sampling. If we already have a trustworthy value, do not even
 * touch the VDDH ADC while USB is connected.
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
 * Decide whether a freshly sampled VDDH value is trustworthy enough to publish.
 * measured_mv is the exact voltage returned by the VDDH driver from the same
 * ADC sample that produced measured_soc.
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
         * If the board booted while USB was already attached, there is no old
         * trustworthy value to preserve. Keep the legacy behaviour for that
         * one exceptional case rather than exposing 0% forever.
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

update_anchor = "static int zmk_battery_update(const struct device *battery) {\n    struct sensor_value state_of_charge;\n    int rc;\n"
update_insert = update_anchor + "\n    if (zmk_battery_hold_while_vbus()) {\n        LOG_DBG(\"Holding battery level at %u while USB is connected\", last_state_of_charge);\n        return 0;\n    }\n"
if "Holding battery level at %u while USB is connected" not in text:
    if update_anchor not in text:
        raise SystemExit("battery patch: update anchor not found")
    text = text.replace(update_anchor, update_insert, 1)

# In the v0.3.0 state-of-charge path the nRF VDDH driver stores voltage and SOC
# from the same ADC conversion, and exposes both channels. Read the voltage after
# SOC so the relaxation detector works in millivolts instead of coarse % steps.
channel_anchor = "    rc = sensor_channel_get(battery, SENSOR_CHAN_GAUGE_STATE_OF_CHARGE, &state_of_charge);\n    if (rc != 0) {\n        LOG_DBG(\"Failed to get battery state of charge: %d\", rc);\n        return rc;\n    }\n"
channel_insert = channel_anchor + r'''

    struct sensor_value gauge_voltage;
    rc = sensor_channel_get(battery, SENSOR_CHAN_GAUGE_VOLTAGE, &gauge_voltage);
    if (rc == 0) {
        const int32_t measured_mv = gauge_voltage.val1 * 1000 + (gauge_voltage.val2 / 1000);
        if (!zmk_battery_accept_vddh_sample(measured_mv)) {
            return 0;
        }
    } else {
        /*
         * Keep compatibility with a future/non-VDDH sensor that might expose
         * SOC but not voltage. The workaround then degrades gracefully to the
         * original ZMK behaviour.
         */
        LOG_DBG("Battery gauge voltage unavailable: %d", rc);
        rc = 0;
    }
'''
if "Battery gauge voltage unavailable" not in text:
    if channel_anchor not in text:
        raise SystemExit("battery patch: state-of-charge channel anchor not found")
    text = text.replace(channel_anchor, channel_insert, 1)

change_anchor = "    if (last_state_of_charge != state_of_charge.val1) {\n"
change_insert = "    battery_has_valid_sample = true;\n\n" + change_anchor
if "battery_has_valid_sample = true;" not in text:
    if change_anchor not in text:
        raise SystemExit("battery patch: change anchor not found")
    text = text.replace(change_anchor, change_insert, 1)

path.write_text(text, encoding="utf-8")
print(f"dynamic VDDH battery patch applied: {path}")
