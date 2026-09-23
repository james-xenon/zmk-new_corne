#!/usr/bin/env python3
from pathlib import Path
import sys

if len(sys.argv) != 2:
    raise SystemExit(f"usage: {sys.argv[0]} /path/to/zmk/app/src/battery.c")

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")

include_anchor = "#include <zephyr/drivers/sensor.h>\n"
include_insert = include_anchor + r'''
#if defined(CONFIG_SOC_NRF52840)
#include <hal/nrf_power.h>
#endif

#if IS_ENABLED(CONFIG_SETTINGS)
#include <zephyr/settings/settings.h>
#endif
'''

if "#include <hal/nrf_power.h>" not in text:
    if include_anchor not in text:
        raise SystemExit("battery patch: include anchor not found")
    text = text.replace(include_anchor, include_insert, 1)

state_anchor = "static uint8_t last_state_of_charge = 0;\n"

state_insert = r'''static uint8_t last_state_of_charge = 0;

/*
 * Eyelash Corne VDDH battery workaround.
 *
 * Problem:
 *   zmk,battery-nrf-vddh estimates SOC from voltage. During/just after
 *   charging, VDDH can be high enough for the stock ZMK curve to report 100%
 *   long before a large battery is actually full.
 *
 * Strategy:
 *   - Persist the last known SOC so deep sleep/reboot does not erase it.
 *   - While USB is attached, DO NOT use VDDH to increase SOC.
 *   - Instead estimate charge gained from elapsed charge time.
 *   - After USB removal, voltage may lower SOC, but may not jump it upward.
 *
 * This is still an estimate, not a hardware fuel gauge.
 */
static bool battery_has_valid_sample;
static bool battery_vbus_was_present;
static int64_t battery_charge_started_at;
static uint8_t battery_charge_start_soc;
static uint8_t battery_last_saved_soc = 0xFF;
static bool battery_initial_settle_active;
static int64_t battery_initial_settle_started_at;

#define ZMK_VDDH_BATTERY_CAPACITY_MAH 1000
#define ZMK_VDDH_CHARGE_CURRENT_MA 220
#define ZMK_VDDH_SETTINGS_KEY "zmk_battery/soc"
#define ZMK_VDDH_PERSIST_STEP 1
#define ZMK_VDDH_INITIAL_SETTLE_MS (20LL * 60LL * 1000LL)

static bool zmk_battery_vbus_present(void) {
#if defined(CONFIG_SOC_NRF52840) && NRF_POWER_HAS_USBREG
    return nrf_power_usbregstatus_vbusdet_get(NRF_POWER);
#else
    return false;
#endif
}

#if IS_ENABLED(CONFIG_SETTINGS)
static int zmk_battery_settings_set(const char *name, size_t len,
                                    settings_read_cb read_cb, void *cb_arg) {
    const char *next;

    if (settings_name_steq(name, "soc", &next) && !next) {
        uint8_t saved_soc = 0;
        if (len != sizeof(saved_soc)) {
            return -EINVAL;
        }

        int rc = read_cb(cb_arg, &saved_soc, sizeof(saved_soc));
        if (rc < 0) {
            return rc;
        }

        if (saved_soc <= 100) {
            last_state_of_charge = saved_soc;
            battery_last_saved_soc = saved_soc;
            battery_has_valid_sample = true;
            LOG_DBG("Loaded persisted battery SOC: %u%%", saved_soc);
        }

        return 0;
    }

    return -ENOENT;
}

SETTINGS_STATIC_HANDLER_DEFINE(zmk_battery_soc, "zmk_battery", NULL,
                               zmk_battery_settings_set, NULL, NULL);

static void zmk_battery_persist_soc(void) {
    if (!battery_has_valid_sample) {
        return;
    }

    if (battery_last_saved_soc <= 100) {
        int delta = (int)last_state_of_charge - (int)battery_last_saved_soc;
        if (delta < 0) {
            delta = -delta;
        }

        if (delta < ZMK_VDDH_PERSIST_STEP) {
            return;
        }
    }

    int rc = settings_save_one(ZMK_VDDH_SETTINGS_KEY,
                               &last_state_of_charge,
                               sizeof(last_state_of_charge));
    if (rc == 0) {
        battery_last_saved_soc = last_state_of_charge;
    } else {
        LOG_WRN("Failed to persist battery SOC: %d", rc);
    }
}
#else
static void zmk_battery_persist_soc(void) {}
#endif

static uint8_t zmk_battery_charge_estimate(void) {
    const int64_t now = k_uptime_get();

    if (!battery_vbus_was_present) {
        battery_vbus_was_present = true;
        battery_charge_started_at = now;
        battery_charge_start_soc = last_state_of_charge;
        LOG_DBG("Battery charge session started at %u%%", battery_charge_start_soc);
    }

    int64_t elapsed_ms = now - battery_charge_started_at;
    if (elapsed_ms < 0) {
        elapsed_ms = 0;
    }

    const int64_t numerator =
        elapsed_ms * (int64_t)ZMK_VDDH_CHARGE_CURRENT_MA * 100LL;
    const int64_t denominator =
        (int64_t)ZMK_VDDH_BATTERY_CAPACITY_MAH * 60LL * 60LL * 1000LL;

    int64_t added_pct = numerator / denominator;
    int64_t estimated = (int64_t)battery_charge_start_soc + added_pct;

    if (estimated > 100) {
        estimated = 100;
    }
    if (estimated < battery_charge_start_soc) {
        estimated = battery_charge_start_soc;
    }

    return (uint8_t)estimated;
}

/* Marker retained for build.yml verification. */
static bool zmk_battery_hold_while_vbus(void) {
    return zmk_battery_vbus_present();
}

/*
 * On battery power SOC should not increase. Voltage rebound and surface
 * charge are therefore prevented from creating an upward jump.
 *
 * Marker name is retained for build.yml verification.
 */
static uint8_t zmk_battery_accept_vddh_sample(uint8_t raw_soc) {
    if (battery_has_valid_sample && raw_soc > last_state_of_charge) {
        return last_state_of_charge;
    }

    return raw_soc;
}
'''

if "ZMK_VDDH_BATTERY_CAPACITY_MAH" not in text:
    if state_anchor not in text:
        raise SystemExit("battery patch: state anchor not found")
    text = text.replace(state_anchor, state_insert, 1)

voltage_branch_anchor = '''#elif IS_ENABLED(CONFIG_ZMK_BATTERY_REPORTING_FETCH_MODE_LITHIUM_VOLTAGE)
    rc = sensor_sample_fetch_chan(battery, SENSOR_CHAN_VOLTAGE);
'''

voltage_branch_insert = '''#elif IS_ENABLED(CONFIG_ZMK_BATTERY_REPORTING_FETCH_MODE_LITHIUM_VOLTAGE)
    if (zmk_battery_hold_while_vbus()) {
        if (!battery_has_valid_sample) {
            /*
             * First installation / settings reset: remember that USB was seen,
             * but never accept the charging VDDH reading as a baseline.
             */
            battery_vbus_was_present = true;
            battery_initial_settle_active = false;
            LOG_DBG("USB present but no trusted battery baseline yet");
            return 0;
        }

        state_of_charge.val1 = zmk_battery_charge_estimate();
        rc = 0;
        goto battery_publish;
    }

    if (!battery_has_valid_sample && battery_vbus_was_present) {
        const int64_t now = k_uptime_get();

        if (!battery_initial_settle_active) {
            battery_initial_settle_active = true;
            battery_initial_settle_started_at = now;
            battery_vbus_was_present = false;
            LOG_DBG("Initial battery baseline settling started");
        }

        if ((now - battery_initial_settle_started_at) < ZMK_VDDH_INITIAL_SETTLE_MS) {
            return 0;
        }

        battery_initial_settle_active = false;
        LOG_DBG("Initial battery baseline settling complete");
    } else if (battery_vbus_was_present) {
        battery_vbus_was_present = false;
        LOG_DBG("Battery charge session ended at %u%%", last_state_of_charge);
    }

    rc = sensor_sample_fetch_chan(battery, SENSOR_CHAN_VOLTAGE);
'''

if "USB present but no trusted battery baseline yet" not in text:
    if voltage_branch_anchor not in text:
        raise SystemExit("battery patch: lithium-voltage branch anchor not found")
    text = text.replace(voltage_branch_anchor, voltage_branch_insert, 1)

mv_anchor = '''    uint16_t mv = voltage.val1 * 1000 + (voltage.val2 / 1000);
    state_of_charge.val1 = lithium_ion_mv_to_pct(mv);

    LOG_DBG("State of change %d from %d mv", state_of_charge.val1, mv);
'''

mv_insert = '''    uint16_t mv = voltage.val1 * 1000 + (voltage.val2 / 1000);
    uint8_t raw_soc = lithium_ion_mv_to_pct(mv);

    state_of_charge.val1 = zmk_battery_accept_vddh_sample(raw_soc);
    battery_has_valid_sample = true;

    LOG_DBG("Battery %d%% from %d mV (raw %u%%)",
            state_of_charge.val1, mv, raw_soc);
'''

if "Battery %d%% from %d mV (raw %u%%)" not in text:
    if mv_anchor not in text:
        raise SystemExit("battery patch: voltage conversion anchor not found")
    text = text.replace(mv_anchor, mv_insert, 1)

publish_anchor = '''#endif

    if (last_state_of_charge != state_of_charge.val1) {
'''

publish_insert = '''#endif

battery_publish:
    if (last_state_of_charge != state_of_charge.val1) {
'''

if "battery_publish:" not in text:
    if publish_anchor not in text:
        raise SystemExit("battery patch: publish anchor not found")
    text = text.replace(publish_anchor, publish_insert, 1)

return_anchor = '''#endif

    return rc;
}
'''

return_insert = '''#endif

    battery_has_valid_sample = true;
    zmk_battery_persist_soc();

    return rc;
}
'''

if "zmk_battery_persist_soc();" not in text:
    if return_anchor not in text:
        raise SystemExit("battery patch: return anchor not found")
    text = text.replace(return_anchor, return_insert, 1)

timer_anchor = '''        k_timer_start(&battery_timer, K_NO_WAIT, K_SECONDS(CONFIG_ZMK_BATTERY_REPORT_INTERVAL));
'''

timer_insert = '''        k_timer_start(&battery_timer, K_SECONDS(2),
                      K_SECONDS(CONFIG_ZMK_BATTERY_REPORT_INTERVAL));
'''

if "k_timer_start(&battery_timer, K_SECONDS(2)," not in text:
    if timer_anchor not in text:
        raise SystemExit("battery patch: timer anchor not found")
    text = text.replace(timer_anchor, timer_insert, 1)

path.write_text(text, encoding="utf-8")
print(f"time-model VDDH battery patch applied: {path}")
