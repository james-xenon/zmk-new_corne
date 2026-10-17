#!/usr/bin/env python3
"""Report Nordic QDEC accumulator counts as ZMK v0.3.0 legacy encoder ticks.

The pinned ZMK behavior_sensor_rotate_common.c deliberately supports the legacy
sensor convention where value.val1 == 0 and value.val2 is a signed tick count.
This patch makes the nRF QDEC driver use that path, avoiding angle/remainder
quantization for the rotary encoder. It does not change QDEC sampling, reporting,
filtering, pins, or other sensors.

Usage: python3 patch_qdec_raw_ticks.py /path/to/zephyr
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

DRIVER_REL = Path("drivers/sensor/qdec_nrfx/qdec_nrfx.c")

OLD_BLOCK = re.compile(
    r"(?m)^(?P<indent>[ \t]*)val->val1 = \(acc \* FULL_ANGLE\) / config->steps;\r?\n"
    r"(?P=indent)val->val2 = \(acc \* FULL_ANGLE\) - \(val->val1 \* config->steps\);\r?\n"
    r"(?P=indent)if \(val->val2 != 0\) \{\r?\n"
    r"(?P=indent)[ \t]+val->val2 \*= 1000000;\r?\n"
    r"(?P=indent)[ \t]+val->val2 /= config->steps;\r?\n"
    r"(?P=indent)\}\r?\n"
)

RAW_BLOCK = """{indent}/*
{indent} * This driver is paired with ZMK v0.3.0's legacy tick path:
{indent} * when val1 == 0, behavior_sensor_rotate_common uses val2 directly
{indent} * as the signed number of encoder triggers. Preserve each valid QDEC
{indent} * accumulator count as one trigger instead of converting it to degrees
{indent} * and accumulating fractional rotation against triggers-per-rotation.
{indent} */
{indent}val->val1 = 0;
{indent}val->val2 = acc;
"""


def fail(message: str) -> "NoReturn":
    raise SystemExit(f"QDEC raw-tick patch: ERROR: {message}")


def channel_get_body(text: str) -> str:
    start = text.find("static int qdec_nrfx_channel_get(")
    end = text.find("static int qdec_nrfx_trigger_set(", start + 1)
    if start < 0 or end < 0:
        fail("could not locate qdec_nrfx_channel_get() in the pinned driver")
    return text[start:end]


def main() -> None:
    if len(sys.argv) != 2:
        fail("usage: python3 patch_qdec_raw_ticks.py /path/to/zephyr")

    zephyr = Path(sys.argv[1]).resolve()
    driver = zephyr / DRIVER_REL
    if not driver.is_file():
        fail(f"driver not found: {driver}")

    text = driver.read_text(encoding="utf-8")
    body = channel_get_body(text)
    old_count = len(list(OLD_BLOCK.finditer(body)))
    raw_pair_count = body.count("val->val1 = 0;") + body.count("val->val2 = acc;")

    if old_count == 1 and raw_pair_count == 0:
        match = OLD_BLOCK.search(body)
        assert match is not None
        replacement = RAW_BLOCK.format(indent=match.group("indent"))
        new_body, count = OLD_BLOCK.subn(lambda _m: replacement, body, count=1)
        if count != 1:
            fail(f"expected one channel_get conversion block, replaced {count}")
        function_start = text.find("static int qdec_nrfx_channel_get(")
        function_end = text.find("static int qdec_nrfx_trigger_set(", function_start + 1)
        if function_start < 0 or function_end < 0:
            fail("could not replace qdec_nrfx_channel_get() safely")
        config_decl = re.compile(
            r"(?m)^[ \t]*const struct qdec_nrfx_config \*config = dev->config;\r?\n"
        )
        new_body, config_count = config_decl.subn("", new_body, count=1)
        if config_count != 1:
            fail(
                "expected to remove the now-unused qdec_nrfx_config pointer "
                f"from channel_get(), removed {config_count}"
            )
        text = text[:function_start] + new_body + text[function_end:]
        driver.write_text(text, encoding="utf-8", newline="\n")
    elif (
        old_count == 0
        and body.count("val->val1 = 0;") == 1
        and body.count("val->val2 = acc;") == 1
        and "const struct qdec_nrfx_config *config = dev->config;" not in body
    ):
        # Idempotent when the same workspace is built again.
        pass
    else:
        fail(
            "unexpected qdec_nrfx_channel_get() source; refusing an unsafe patch. "
            f"conversion_blocks={old_count}, val1_zero={body.count('val->val1 = 0;')}, "
            f"val2_acc={body.count('val->val2 = acc;')}"
        )

    final_body = channel_get_body(driver.read_text(encoding="utf-8"))
    if "val->val1 = 0;" not in final_body or "val->val2 = acc;" not in final_body:
        fail("raw tick mapping verification failed")
    if "config->steps" in final_body:
        fail("angle scaling still remains in channel_get(); refusing to claim raw tick mode")
    if "const struct qdec_nrfx_config *config = dev->config;" in final_body:
        fail("unused qdec_nrfx_config local remains in channel_get()")

    print(
        "QDEC raw-tick patch verified: val1=0, val2=acc; "
        "SAMPLEPER/REPORTPER/filter settings unchanged."
    )


if __name__ == "__main__":
    main()
