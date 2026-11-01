#!/usr/bin/env python3
"""Apply the ZMK v0.3.0 Eyelash/nice!nano VDDH battery patch.

The patch keeps ZMK's STATE_OF_CHARGE reporting mode.  It obtains GAUGE_VOLTAGE
from the same VDDH sensor sample and uses that voltage only to decide when a
post-USB reading is stable enough to publish.

USB history is detected without polling while the battery timer is stopped:
- central half: ZMK USB connection events;
- peripheral half (where ZMK_USB is not enabled): latched nRF52840 POWER events;
- System OFF wake caused by VBUS: RESETREAS.VBUS.

The script is intentionally tied to the upstream ZMK v0.3.0 app/src/battery.c
layout.  It fails closed if the source differs or if an older V1 patch is found.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

PATCH_MARKER = "ZMK_VDDH_STATE_OF_CHARGE_PATCH_V2"
OLD_PATCH_MARKER = "ZMK_VDDH_STATE_OF_CHARGE_PATCH_V1"


def require_once(source: str, anchor: str, description: str) -> None:
    count = source.count(anchor)
    if count != 1:
        raise SystemExit(
            f"battery patch: expected exactly one {description} anchor, found {count}; "
            "no file changed"
        )


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {sys.argv[0]} /path/to/zmk/app/src/battery.c")

    path = Path(sys.argv[1])
    if not path.is_file():
        raise SystemExit(f"battery patch: source file not found: {path}")

    source = path.read_text(encoding="utf-8")

    if PATCH_MARKER in source:
        print(f"VDDH state-of-charge stabilization patch V2 already applied: {path}")
        return 0

    if OLD_PATCH_MARKER in source:
        raise SystemExit(
            "battery patch: V1 patch marker found. Restore the original unpatched "
            "ZMK v0.3.0 app/src/battery.c from backup before applying V2; no file changed"
        )

    # This patch is specifically for the STATE_OF_CHARGE mode plus the VDDH
    # driver's GAUGE_VOLTAGE channel.  Do not silently patch another mode.
    soc_fetch_anchor = (
        "#if IS_ENABLED(CONFIG_ZMK_BATTERY_REPORTING_FETCH_MODE_STATE_OF_CHARGE)\n"
        "\n"
        "    rc = sensor_sample_fetch_chan(battery, SENSOR_CHAN_GAUGE_STATE_OF_CHARGE);\n"
    )
    soc_get_anchor = (
        "    rc = sensor_channel_get(battery, SENSOR_CHAN_GAUGE_STATE_OF_CHARGE, &state_of_charge);\n"
        "    if (rc != 0) {\n"
        '        LOG_DBG("Failed to get battery state of charge: %d", rc);\n'
        "        return rc;\n"
        "    }\n"
    )
    listener_anchor = (
        "static int battery_event_listener(const zmk_event_t *eh) {\n"
        "\n"
        "    if (as_zmk_activity_state_changed(eh)) {"
    )
    subscription_anchor = "ZMK_SUBSCRIPTION(battery, zmk_activity_state_changed);\n"
    init_anchor = "    zmk_battery_start_reporting();\n    return 0;\n}\n"

    require_once(source, "#include <zephyr/drivers/sensor.h>\n", "sensor include")
    require_once(source, soc_fetch_anchor, "STATE_OF_CHARGE fetch")
    require_once(source, soc_get_anchor, "STATE_OF_CHARGE channel_get")
    require_once(source, listener_anchor, "battery event listener")
    require_once(source, subscription_anchor, "activity subscription")
    require_once(source, init_anchor, "battery initialization")

    # ------------------------------------------------------------------
    # Includes. ZMK USB event support exists only on the central build.
    # ------------------------------------------------------------------
    source = source.replace(
        "#include <zephyr/drivers/sensor.h>\n",
        "#include <zephyr/drivers/sensor.h>\n"
        "\n"
        "#if defined(CONFIG_SOC_NRF52840)\n"
        "#include <hal/nrf_power.h>\n"
        "#endif\n"
        "\n"
        "#if defined(CONFIG_ZMK_USB)\n"
        "#include <zmk/events/usb_conn_state_changed.h>\n"
        "#include <zmk/usb.h>\n"
        "#endif\n",
        1,
    )

    # ------------------------------------------------------------------
    # USB history + voltage settling state machine.
    # ------------------------------------------------------------------
    state_anchor = "static uint8_t last_state_of_charge = 0;\n"
    require_once(source, state_anchor, "last_state_of_charge")

    state_block = r'''static uint8_t last_state_of_charge = 0;

/* ZMK_VDDH_STATE_OF_CHARGE_PATCH_V2
 * Eyelash/nice!nano VDDH battery stabilization.
 *
 * The battery sensor's STATE_OF_CHARGE and GAUGE_VOLTAGE values come from the
 * same VDDH ADC sample.  VBUS makes VDDH unsuitable as a LiPo measurement.
 * After USB removal, keep the last published percentage until three adjacent
 * voltage samples differ by no more than 15 mV.  Fail open after 15 minutes.
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

static void zmk_battery_mark_vbus_present(void) {
    if (!battery_vbus_was_present) {
        LOG_DBG("USB power detected; post-charge settling armed");
    }
    battery_vbus_was_present = true;
    battery_post_charge_settling = false;
    battery_settle_stable_samples = 0;
}

static bool zmk_battery_vbus_present(void) {
#if defined(CONFIG_SOC_NRF52840) && NRF_POWER_HAS_USBREG
    return nrf_power_usbregstatus_vbusdet_get(NRF_POWER);
#else
    return false;
#endif
}

/*
 * On the central half, ZMK's USB event listener records VBUS transitions.
 * The peripheral half does not run ZMK_USB, so its POWER event registers are
 * left latched and are consumed here when battery reporting resumes.  Do not
 * consume these registers on a USB-enabled central: usb_dc_nrfx owns them.
 */
static void zmk_battery_capture_vbus_history(void) {
#if defined(CONFIG_SOC_NRF52840) && NRF_POWER_HAS_USBREG && \
    !defined(CONFIG_ZMK_USB) && !defined(CONFIG_ZMK_USB_LOGGING)
    const bool usb_detected =
        nrf_power_event_get_and_clear(NRF_POWER, NRF_POWER_EVENT_USBDETECTED);
    const bool usb_removed =
        nrf_power_event_get_and_clear(NRF_POWER, NRF_POWER_EVENT_USBREMOVED);

    if (usb_detected || usb_removed) {
        zmk_battery_mark_vbus_present();
    }
#endif

/* VBUS wakes nRF52840 from System OFF by reset.  This catches a complete
 * connect/charge/disconnect cycle that happened while the battery timer was
 * stopped and RAM state was lost.  Clear only the VBUS reason bit.
 */
#if defined(CONFIG_SOC_NRF52840) && defined(POWER_RESETREAS_VBUS_Msk)
    const uint32_t reset_reason = nrf_power_resetreas_get(NRF_POWER);
    if (reset_reason & NRF_POWER_RESETREAS_VBUS_MASK) {
        nrf_power_resetreas_clear(NRF_POWER, NRF_POWER_RESETREAS_VBUS_MASK);
        zmk_battery_mark_vbus_present();
    }
#endif
}

static bool zmk_battery_hold_while_vbus(void) {
    zmk_battery_capture_vbus_history();

    if (zmk_battery_vbus_present()) {
        zmk_battery_mark_vbus_present();
        /* At USB-connected boot, allow one reading to initialize the value. */
        return battery_has_valid_sample;
    }

    return false;
}

static int32_t zmk_battery_abs_i32(int32_t value) {
    return value < 0 ? -value : value;
}

static bool zmk_battery_accept_vddh_sample(int32_t measured_mv) {
#if defined(CONFIG_SOC_NRF52840) && NRF_POWER_HAS_USBREG
    const bool vbus_present = zmk_battery_vbus_present();
    const int64_t now = k_uptime_get();

    if (vbus_present) {
        zmk_battery_mark_vbus_present();
        /* Do not publish VDDH-derived values while charging, except for the
         * first reading after a USB-connected boot when no trusted value exists.
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
        const int32_t delta_mv =
            zmk_battery_abs_i32(measured_mv - battery_settle_prev_mv);

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
    source = source.replace(state_anchor, state_block, 1)

    # ------------------------------------------------------------------
    # Hold the last percentage before fetching while VBUS is present.
    # ------------------------------------------------------------------
    source = source.replace(
        soc_fetch_anchor,
        soc_fetch_anchor.replace(
            "    rc = sensor_sample_fetch_chan(battery, SENSOR_CHAN_GAUGE_STATE_OF_CHARGE);\n",
            "    if (zmk_battery_hold_while_vbus()) {\n"
            '        LOG_DBG("Holding battery level while USB is connected");\n'
            "        return 0;\n"
            "    }\n\n"
            "    rc = sensor_sample_fetch_chan(battery, SENSOR_CHAN_GAUGE_STATE_OF_CHARGE);\n",
            1,
        ),
        1,
    )

    # ------------------------------------------------------------------
    # Read GAUGE_VOLTAGE from the same sample and gate publication.
    # ------------------------------------------------------------------
    voltage_block = soc_get_anchor + r'''
    struct sensor_value voltage;
    rc = sensor_channel_get(battery, SENSOR_CHAN_GAUGE_VOLTAGE, &voltage);
    if (rc != 0) {
        LOG_DBG("Failed to get battery voltage: %d", rc);
        return rc;
    }

    const int32_t measured_mv = voltage.val1 * 1000 + (voltage.val2 / 1000);
    if (!zmk_battery_accept_vddh_sample(measured_mv)) {
        return 0;
    }
    battery_has_valid_sample = true;
'''
    source = source.replace(soc_get_anchor, voltage_block, 1)

    # ------------------------------------------------------------------
    # USB connection events on the central half.  The disconnect event does
    # not clear the flag: it is consumed by the next battery sample.
    # ------------------------------------------------------------------
    listener_block = '''static int battery_event_listener(const zmk_event_t *eh) {
#if defined(CONFIG_ZMK_USB)
    if (as_zmk_usb_conn_state_changed(eh)) {
        const struct zmk_usb_conn_state_changed *usb_event =
            as_zmk_usb_conn_state_changed(eh);

        if (usb_event->conn_state != ZMK_USB_CONN_NONE) {
            zmk_battery_mark_vbus_present();
        }
        return 0;
    }
#endif

    if (as_zmk_activity_state_changed(eh)) {'''
    source = source.replace(listener_anchor, listener_block, 1)

    subscription_block = (
        subscription_anchor
        + "#if defined(CONFIG_ZMK_USB)\n"
        + "ZMK_SUBSCRIPTION(battery, zmk_usb_conn_state_changed);\n"
        + "#endif\n"
    )
    source = source.replace(subscription_anchor, subscription_block, 1)

    # Capture a cable that is already connected at boot, and a VBUS wake from
    # System OFF, before the first scheduled battery measurement can run.
    init_block = (
        "    zmk_battery_capture_vbus_history();\n"
        "    if (zmk_battery_vbus_present()) {\n"
        "        zmk_battery_mark_vbus_present();\n"
        "    }\n\n"
        "    zmk_battery_start_reporting();\n"
        "    return 0;\n"
        "}\n"
    )
    source = source.replace(init_anchor, init_block, 1)

    # Check that all intended parts exist before writing anything.
    for required in (
        PATCH_MARKER,
        "SENSOR_CHAN_GAUGE_VOLTAGE",
        "zmk_battery_capture_vbus_history",
        "NRF_POWER_RESETREAS_VBUS_MASK",
        "ZMK_SUBSCRIPTION(battery, zmk_usb_conn_state_changed);",
        "ZMK_VDDH_SETTLE_STABLE_SAMPLES 3",
    ):
        if required not in source:
            raise SystemExit(
                f"battery patch: internal validation failed ({required!r}); no file changed"
            )

    # Preserve a pristine source copy and write atomically.
    backup = path.with_name(path.name + ".before_vddh_patch.bak")
    if backup.exists():
        raise SystemExit(
            f"battery patch: backup already exists: {backup}; "
            "move it aside or restore the original source deliberately; no file changed"
        )

    backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as temp_file:
            temp_file.write(source)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        # Keep the backup for recovery if the final write failed.
        raise

    print(f"VDDH state-of-charge stabilization patch V2 applied: {path}")
    print(f"Original battery.c backup: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
