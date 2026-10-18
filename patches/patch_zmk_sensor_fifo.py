#!/usr/bin/env python3
"""Patch pinned Zephyr nRF QDEC to deliver each valid sample as a queued sensor tick."""
from __future__ import annotations

import re
import sys
from pathlib import Path

DRIVER_REL = Path("drivers/sensor/qdec_nrfx/qdec_nrfx.c")
MARKER = "QDEC_SAMPLE_FIFO_PATCH_V1"
QUEUE_SIZE = 128


def fail(message: str) -> "NoReturn":
    raise SystemExit(f"QDEC sample FIFO patch: ERROR: {message}")


def replace_region(text: str, start_marker: str, end_marker: str, replacement: str) -> str:
    start = text.find(start_marker)
    end = text.find(end_marker, start + 1)
    if start < 0 or end < 0:
        fail(f"cannot locate source region: {start_marker!r} -> {end_marker!r}")
    return text[:start] + replacement + text[end:]


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
        print("QDEC sample FIFO patch already applied.")
        return

    # The patch is intentionally pinned to the qdec_nrfx layout in Zephyr 3.5.0+zmk-fixes.
    if "static int qdec_nrfx_sample_fetch(" not in text or "static int qdec_nrfx_channel_get(" not in text:
        fail("unexpected qdec_nrfx.c structure; no changes made")
    if "case NRF_QDEC_EVENT_SAMPLERDY:" not in text or "NRF_QDEC_SAMPLEPER_2048US" not in text:
        fail("expected nRF QDEC event/configuration code not found; no changes made")

    # Replace the data structure and remove the old report-accumulator helper.
    data_struct = f'''/* {MARKER}: preserve ordered QDEC sample values until ZMK consumes them. */
#define QDEC_TICK_QUEUE_SIZE {QUEUE_SIZE}
struct qdec_nrfx_data {{
    int8_t tick_queue[QDEC_TICK_QUEUE_SIZE];
    uint16_t tick_head;
    uint16_t tick_tail;
    uint16_t tick_count;
    int32_t fetched_tick;
    sensor_trigger_handler_t data_ready_handler;
    const struct sensor_trigger *data_ready_trigger;
}};

'''
    text = replace_region(text, "struct qdec_nrfx_data {", "struct qdec_nrfx_config {", data_struct)

    # The old helper only accumulated periodic reports; per-sample FIFO no longer uses it.
    text = replace_region(text, "static void accumulate(", "static int qdec_nrfx_sample_fetch(", "")

    sample_fetch = f'''static int qdec_nrfx_sample_fetch(const struct device *dev,
                                  enum sensor_channel chan)
{{
    struct qdec_nrfx_data *data = dev->data;
    unsigned int key;

    if ((chan != SENSOR_CHAN_ALL) && (chan != SENSOR_CHAN_ROTATION)) {{
        return -ENOTSUP;
    }}

    key = irq_lock();
    if (data->tick_count == 0) {{
        irq_unlock(key);
        return -EAGAIN;
    }}

    data->fetched_tick = data->tick_queue[data->tick_tail];
    data->tick_tail = (data->tick_tail + 1) & (QDEC_TICK_QUEUE_SIZE - 1);
    data->tick_count--;
    irq_unlock(key);

    return 0;
}}

'''
    text = replace_region(text, "static int qdec_nrfx_sample_fetch(", "static int qdec_nrfx_channel_get(", sample_fetch)

    channel_get = '''static int qdec_nrfx_channel_get(const struct device *dev,
                                 enum sensor_channel chan,
                                 struct sensor_value *val)
{
    const struct qdec_nrfx_config *config = dev->config;
    struct qdec_nrfx_data *data = dev->data;
    int32_t tick;

    if (chan != SENSOR_CHAN_ROTATION) {
        return -ENOTSUP;
    }

    tick = data->fetched_tick;
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
    }}

    irq_unlock(key);
    return queued;
}}

static void qdec_nrfx_event_handler(nrfx_qdec_event_t event, void *p_context)
{{
    const struct device *dev = p_context;
    struct qdec_nrfx_data *dev_data = dev->data;
    const struct qdec_nrfx_config *config = dev->config;
    sensor_trigger_handler_t handler;
    const struct sensor_trigger *trig;
    unsigned int key;
    int32_t discarded_acc;
    uint32_t discarded_accdbl;

    switch (event.type) {{
    case NRF_QDEC_EVENT_SAMPLERDY:
        /*
         * Do not let the hardware ACC register collapse opposite movements
         * between REPORTPER events. The SAMPLE register already contains this
         * individual quadrature sample (-1, 0, +1, or an invalid double step).
         * Clear hardware accumulators each sample; consume valid +/-1 samples
         * through the software FIFO, one ZMK sensor event per sample.
         */
        nrfx_qdec_accumulators_read(&config->qdec, &discarded_acc, &discarded_accdbl);

        if ((event.data.sample.value != -1) && (event.data.sample.value != 1)) {{
            break;
        }}

        if (!qdec_nrfx_queue_tick(dev_data, event.data.sample.value)) {{
            LOG_ERR("QDEC sample FIFO full; sample dropped");
            break;
        }}

        key = irq_lock();
        handler = dev_data->data_ready_handler;
        trig = dev_data->data_ready_trigger;
        irq_unlock(key);

        if (handler) {{
            handler(dev, trig);
        }}
        break;

    case NRF_QDEC_EVENT_REPORTRDY:
        /* Report interrupts are disabled in this configuration. */
        break;

    case NRF_QDEC_EVENT_ACCOF:
        /* Accumulators are cleared on each SAMPLE event; this should not occur. */
        LOG_ERR("Unexpected QDEC accumulator overflow");
        break;

    default:
        LOG_ERR("unhandled event (0x%x)", event.type);
        break;
    }}
}}

'''
    text = replace_region(text, "static void qdec_nrfx_event_handler(", "static void qdec_nrfx_gpio_ctrl(", event_handler)

    # Use per-sample IRQ notifications, not the aggregated report interrupt.
    old_config = ".reportper_inten = true,"
    if text.count(old_config) != 1:
        fail("expected exactly one .reportper_inten = true in QDEC config")
    text = text.replace(
        old_config,
        ".sample_inten = true, " + "\\\n\t\t\t.reportper_inten = false,",
        1,
    )

    driver.write_text(text, encoding="utf-8", newline="\n")
    verify(text)
    print("QDEC sample FIFO patch applied and verified.")


def verify(text: str) -> None:
    required = [
        MARKER,
        ".sample_inten = true,",
        ".reportper_inten = false,",
        "event.data.sample.value != -1",
        "event.data.sample.value != 1",
        "QDEC_TICK_QUEUE_SIZE 128",
        "data->fetched_tick = data->tick_queue[data->tick_tail];",
    ]
    for item in required:
        if item not in text:
            fail(f"verification failed: missing {item!r}")
    # Keep both designated fields inside SENSOR_NRFX_QDEC_DEVICE's continued macro.
    if not re.search(
        r"\.sample_inten\s*=\s*true,[ \t]*\\\r?\n[ \t]*\.reportper_inten\s*=\s*false,",
        text,
    ):
        fail("sample/report interrupt fields are not joined by a C preprocessor line continuation")
    if ".reportper_inten = true," in text:
        fail("aggregated report interrupt remains enabled")
    if "val->val1 = 0;" in text and "val->val2 = acc;" in text:
        fail("old raw-tick mapping remains; expected per-sample angular conversion")


if __name__ == "__main__":
    main()
