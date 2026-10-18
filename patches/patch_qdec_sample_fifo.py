#!/usr/bin/env python3
"""Patch Zephyr v3.5.0+zmk-fixes QDEC to preserve every sample as a queued tick.

For SAMPLERDY values -1/+1, enqueue the valid direction. Value 2 denotes an
invalid/double quadrature transition. Its direction is not encoded by hardware;
when a previous direction is known, recover the two missed counts in that
last direction. This is necessarily a heuristic for an ambiguous transition.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

DRIVER_REL = Path("drivers/sensor/qdec_nrfx/qdec_nrfx.c")
MARKER = "QDEC_SAMPLE_FIFO_PATCH_V1_INVALID_RECOVERY"
QUEUE_SIZE = 128


def fail(message: str) -> "NoReturn":
    raise SystemExit(f"QDEC sample FIFO patch: ERROR: {message}")


def replace_region(text: str, start_marker: str, end_marker: str, replacement: str) -> str:
    start = text.find(start_marker)
    end = text.find(end_marker, start + 1)
    if start < 0 or end < 0:
        fail(f"cannot locate source region: {start_marker!r} -> {end_marker!r}")
    return text[:start] + replacement + text[end:]


def verify(text: str) -> None:
    required = [
        MARKER,
        "int8_t last_direction;",
        "event.data.sample.value == 2",
        "tick = (int8_t)(2 * dev_data->last_direction);",
        ".sample_inten = true,",
        ".reportper_inten = false,",
        "data->fetched_tick = data->tick_queue[data->tick_tail];",
    ]
    for item in required:
        if item not in text:
            fail(f"verification failed: missing {item!r}")

    # Both fields must remain inside the macro-defined initializer. Without the
    # backslash, GCC reports '.reportper_inten' as an unexpected top-level token.
    if not re.search(
        r"\.sample_inten\s*=\s*true,[ \t]*\\\r?\n[ \t]*\.reportper_inten\s*=\s*false,",
        text,
    ):
        fail("sample/report interrupt fields lack the required C macro continuation")
    if ".reportper_inten = true," in text:
        fail("aggregated report interrupt is still enabled")


def main() -> None:
    if len(sys.argv) != 2:
        fail("usage: python3 patch_qdec_sample_fifo.py /path/to/zephyr")

    zephyr = Path(sys.argv[1]).resolve()
    driver = zephyr / DRIVER_REL
    if not driver.is_file():
        fail(f"driver not found: {driver}")

    text = driver.read_text(encoding="utf-8")
    if MARKER in text:
        verify(text)
        print("QDEC sample FIFO/invalid-transition patch already applied and verified.")
        return

    if "static int qdec_nrfx_sample_fetch(" not in text or "static int qdec_nrfx_channel_get(" not in text:
        fail("unexpected qdec_nrfx.c structure; no changes made")
    if "case NRF_QDEC_EVENT_SAMPLERDY:" not in text or ".reportper_inten = true," not in text:
        fail("expected Zephyr v3.5.0+zmk-fixes QDEC code not found; no changes made")

    data_struct = f'''/* {MARKER}: preserve ordered SAMPLE events until ZMK consumes them. */
#define QDEC_TICK_QUEUE_SIZE {QUEUE_SIZE}
struct qdec_nrfx_data {{
    int8_t tick_queue[QDEC_TICK_QUEUE_SIZE];
    uint16_t tick_head;
    uint16_t tick_tail;
    uint16_t tick_count;
    int32_t fetched_tick;
    int8_t last_direction;
    uint32_t invalid_double_samples;
    uint32_t recovered_double_samples;
    uint32_t tick_queue_overflows;
    sensor_trigger_handler_t data_ready_handler;
    const struct sensor_trigger *data_ready_trigger;
}};

'''
    text = replace_region(text, "struct qdec_nrfx_data {", "struct qdec_nrfx_config {", data_struct)

    # The report accumulator path is deliberately replaced: it coalesces signed
    # movements across REPORTPER and can cancel a forward/backward pair.
    text = replace_region(text, "static void accumulate(", "static int qdec_nrfx_sample_fetch(", "")

    sample_fetch = '''static int qdec_nrfx_sample_fetch(const struct device *dev,
                                  enum sensor_channel chan)
{
    struct qdec_nrfx_data *data = dev->data;
    unsigned int key;

    if ((chan != SENSOR_CHAN_ALL) && (chan != SENSOR_CHAN_ROTATION)) {
        return -ENOTSUP;
    }

    key = irq_lock();
    if (data->tick_count == 0) {
        irq_unlock(key);
        return -EAGAIN;
    }

    data->fetched_tick = data->tick_queue[data->tick_tail];
    data->tick_tail = (data->tick_tail + 1) & (QDEC_TICK_QUEUE_SIZE - 1);
    data->tick_count--;
    irq_unlock(key);

    return 0;
}

'''
    text = replace_region(text, "static int qdec_nrfx_sample_fetch(", "static int qdec_nrfx_channel_get(", sample_fetch)

    channel_get = '''static int qdec_nrfx_channel_get(const struct device *dev,
                                 enum sensor_channel chan,
                                 struct sensor_value *val)
{
    const struct qdec_nrfx_config *config = dev->config;
    struct qdec_nrfx_data *data = dev->data;
    int32_t tick = data->fetched_tick;

    if (chan != SENSOR_CHAN_ROTATION) {
        return -ENOTSUP;
    }

    val->val1 = (tick * FULL_ANGLE) / config->steps;
    val->val2 = (tick * FULL_ANGLE) - (val->val1 * config->steps);
    if (val->val2 != 0) {
        val->val2 *= 1000000;
        val->val2 /= config->steps;
    }

    return 0;
}

'''
    text = replace_region(text, "static int qdec_nrfx_channel_get(", "static int qdec_nrfx_trigger_set(", channel_get)

    event_handler = f'''static bool qdec_nrfx_queue_tick(struct qdec_nrfx_data *data, int8_t tick)
{{
    bool queued = false;
    unsigned int key = irq_lock();

    if (data->tick_count < QDEC_TICK_QUEUE_SIZE) {{
        data->tick_queue[data->tick_head] = tick;
        data->tick_head = (data->tick_head + 1) & (QDEC_TICK_QUEUE_SIZE - 1);
        data->tick_count++;
        queued = true;
    }} else {{
        data->tick_queue_overflows++;
    }}

    irq_unlock(key);
    return queued;
}}

static void qdec_nrfx_event_handler(nrfx_qdec_event_t event, void *p_context)
{{
    const struct device *dev = p_context;
    struct qdec_nrfx_data *dev_data = dev->data;
    sensor_trigger_handler_t handler;
    const struct sensor_trigger *trig;
    unsigned int key;
    int8_t tick = 0;

    switch (event.type) {{
    case NRF_QDEC_EVENT_SAMPLERDY:
        /*
         * SAMPLE is the decoded quadrature motion value: -1, 0, +1, or 2.
         * A value of 2 is a double transition (both A/B changed between
         * samples). Hardware does not encode its direction. If direction is
         * known from the last valid sample, recover two counts in that direction;
         * this is the only possible heuristic from QDEC's sample value alone.
         */
        key = irq_lock();
        if ((event.data.sample.value == -1) || (event.data.sample.value == 1)) {{
            tick = event.data.sample.value;
            dev_data->last_direction = tick;
        }} else if (event.data.sample.value == 2) {{
            dev_data->invalid_double_samples++;
            if (dev_data->last_direction != 0) {{
                tick = (int8_t)(2 * dev_data->last_direction);
                dev_data->recovered_double_samples++;
            }}
        }}
        handler = dev_data->data_ready_handler;
        trig = dev_data->data_ready_trigger;
        irq_unlock(key);

        if (tick == 0) {{
            break;
        }}
        if (!qdec_nrfx_queue_tick(dev_data, tick)) {{
            break;
        }}
        if (handler) {{
            handler(dev, trig);
        }}
        break;

    case NRF_QDEC_EVENT_REPORTRDY:
        /* Accumulated report events are intentionally not consumed. */
        break;

    default:
        /* Keep the ISR quiet: no high-frequency logging that could disturb BLE. */
        break;
    }}
}}

'''
    text = replace_region(text, "static void qdec_nrfx_event_handler(", "static void qdec_nrfx_gpio_ctrl(", event_handler)

    # SAMPLERDY delivers the per-sample values. Keep REPORTPER configured as the
    # hardware's periodic accumulator clear, but stop using REPORTPER as the
    # sensor notification source.
    text = text.replace(
        ".reportper_inten = true,",
        ".sample_inten = true, \\\n\t\t\t.reportper_inten = false,",
        1,
    )

    driver.write_text(text, encoding="utf-8", newline="\n")
    verify(text)
    print("QDEC sample FIFO and double-transition recovery applied and verified.")


if __name__ == "__main__":
    main()
