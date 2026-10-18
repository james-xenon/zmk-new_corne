#!/usr/bin/env python3
"""Replace ZMK v0.3.0's coalescing sensor pending bitset with a FIFO."""
from __future__ import annotations

import sys
from pathlib import Path

SENSORS_REL = Path("app/src/sensors.c")
MARKER = "ZMK_SENSOR_TRIGGER_FIFO_PATCH_V1"
QUEUE_SIZE = 128


def fail(message: str) -> "NoReturn":
    raise SystemExit(f"ZMK sensor FIFO patch: ERROR: {message}")


def replace_region(text: str, start_marker: str, end_marker: str, replacement: str) -> str:
    start = text.find(start_marker)
    end = text.find(end_marker, start + 1)
    if start < 0 or end < 0:
        fail(f"cannot locate source region: {start_marker!r} -> {end_marker!r}")
    return text[:start] + replacement + text[end:]


def main() -> None:
    if len(sys.argv) != 2:
        fail("usage: python3 patch_zmk_sensor_fifo.py /path/to/zmk")

    zmk = Path(sys.argv[1]).resolve()
    source = zmk / SENSORS_REL
    if not source.is_file():
        fail(f"source not found: {source}")

    text = source.read_text(encoding="utf-8")
    if MARKER in text:
        verify(text)
        print("ZMK sensor FIFO patch already applied.")
        return

    if "static ATOMIC_DEFINE(pending_sensors, ZMK_KEYMAP_SENSORS_LEN);" not in text:
        fail("expected v0.3.0 pending_sensors bitset not found; no changes made")
    if "static void run_sensors_data_trigger(struct k_work *work)" not in text:
        fail("expected v0.3.0 sensor work handler not found; no changes made")

    # Ensure the message-queue API is declared.
    if "#include <zephyr/kernel.h>" not in text:
        include_anchor = "#include <zephyr/init.h>"
        if text.count(include_anchor) != 1:
            fail("cannot insert Zephyr kernel include safely")
        text = text.replace(include_anchor, include_anchor + "\n#include <zephyr/kernel.h>", 1)

    text = text.replace(
        "static ATOMIC_DEFINE(pending_sensors, ZMK_KEYMAP_SENSORS_LEN);",
        f"/* {MARKER}: preserve one notification per QDEC sample rather than coalescing a bit. */\n"
        f"#define ZMK_SENSOR_TRIGGER_QUEUE_SIZE {QUEUE_SIZE}\n"
        "K_MSGQ_DEFINE(pending_sensor_indices, sizeof(uint8_t), ZMK_SENSOR_TRIGGER_QUEUE_SIZE, 1);",
        1,
    )

    new_handlers = f'''static void run_sensors_data_trigger(struct k_work *work)
{{
    uint8_t sensor_index;

    /* Drain every queued interrupt notification; do not merge opposite ticks. */
    while (k_msgq_get(&pending_sensor_indices, &sensor_index, K_NO_WAIT) == 0) {{
        if (sensor_index >= ARRAY_SIZE(sensors)) {{
            LOG_ERR("Invalid queued sensor index %u", sensor_index);
            continue;
        }}
        trigger_sensor_data_for_position(sensor_index);
    }}
}}

K_WORK_DEFINE(sensor_data_work, run_sensors_data_trigger);
static void zmk_sensors_trigger_handler(const struct device *dev,
                                        const struct sensor_trigger *trigger)
{{
    const struct sensors_item_cfg *test_item =
        CONTAINER_OF(trigger, struct sensors_item_cfg, trigger);
    int sensor_index = test_item - sensors;

    if (sensor_index < 0 || sensor_index >= ARRAY_SIZE(sensors)) {{
        LOG_ERR("Invalid sensor item triggered our callback (%d)", sensor_index);
        return;
    }}
    if (k_is_in_isr()) {{
        uint8_t queued_index = (uint8_t)sensor_index;
        if (k_msgq_put(&pending_sensor_indices, &queued_index, K_NO_WAIT) != 0) {{
            LOG_ERR("Sensor trigger FIFO full; notification dropped");
            return;
        }}
        k_work_submit(&sensor_data_work);
    }} else {{
        trigger_sensor_data_for_position(sensor_index);
    }}
}}

'''
    text = replace_region(
        text,
        "static void run_sensors_data_trigger(struct k_work *work)",
        "static void zmk_sensors_init_item(uint8_t i)",
        new_handlers,
    )

    source.write_text(text, encoding="utf-8", newline="\n")
    verify(text)
    print("ZMK sensor FIFO patch applied and verified.")


def verify(text: str) -> None:
    required = [
        MARKER,
        "K_MSGQ_DEFINE(pending_sensor_indices, sizeof(uint8_t), ZMK_SENSOR_TRIGGER_QUEUE_SIZE, 1);",
        "while (k_msgq_get(&pending_sensor_indices, &sensor_index, K_NO_WAIT) == 0)",
        "k_msgq_put(&pending_sensor_indices, &queued_index, K_NO_WAIT)",
    ]
    for item in required:
        if item not in text:
            fail(f"verification failed: missing {item!r}")
    if "ATOMIC_DEFINE(pending_sensors" in text or "atomic_test_and_clear_bit(pending_sensors" in text:
        fail("old coalescing pending_sensors bitset remains")


if __name__ == "__main__":
    main()
