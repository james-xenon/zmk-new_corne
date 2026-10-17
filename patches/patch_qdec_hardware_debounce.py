#!/usr/bin/env python3
"""Enable the nRF QDEC hardware debounce filter in the pinned Zephyr source.

Usage: python3 patch_qdec_hardware_debounce.py /path/to/zephyr
The edit is intentionally limited to the binding and qdec_nrfx driver.
"""
from __future__ import annotations

import sys
from pathlib import Path


BINDING_REL = Path("dts/bindings/sensor/nordic,nrf-qdec.yaml")
DRIVER_REL = Path("drivers/sensor/qdec_nrfx/qdec_nrfx.c")
BINDING_PROPERTY = "  debounce-filter:\n    type: boolean\n    description: |\n      Enable the hardware debounce filters for both quadrature inputs.\n\n"
DRIVER_PROPERTY = ".dbfen = DT_NODE_HAS_PROP(QDEC(idx), debounce_filter),"
SAMPLEPER_MARKER = ".sampleper = NRF_QDEC_SAMPLEPER_2048US,"


def fail(message: str) -> "NoReturn":
    raise SystemExit(f"QDEC debounce patch: ERROR: {message}")


def insert_binding_property(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    has_property = "  debounce-filter:" in text
    has_steps = "  steps:\n" in text
    if has_property:
        if BINDING_PROPERTY.rstrip() not in text:
            fail(f"'{path}' already has a different debounce-filter definition")
        return
    if not has_steps or text.count("  steps:\n") != 1:
        fail(f"expected one 'steps:' property in '{path}'; source version is unexpected")
    if "  led-pre:\n" not in text:
        fail(f"expected 'led-pre:' in '{path}'; source version is unexpected")
    # Add the optional boolean immediately before 'steps' without depending on
    # whitespace/alignment used elsewhere in the YAML file.
    text = text.replace("  steps:\n", BINDING_PROPERTY + "  steps:\n", 1)
    path.write_text(text, encoding="utf-8", newline="\n")


def insert_driver_property(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    if DRIVER_PROPERTY in text:
        return
    if text.count(SAMPLEPER_MARKER) != 1:
        fail(
            f"expected exactly one '{SAMPLEPER_MARKER}' in '{path}'; "
            "the pinned driver source may have changed"
        )

    lines = text.splitlines(keepends=True)
    matching = [i for i, line in enumerate(lines) if SAMPLEPER_MARKER in line]
    if len(matching) != 1:
        fail(f"could not locate the unique SAMPLEPER initializer in '{path}'")
    i = matching[0]
    current_line = lines[i]
    if not current_line.rstrip("\r\n").rstrip().endswith("\\"):
        fail(f"SAMPLEPER initializer in '{path}' is not a continued macro line")
    indent = current_line[: len(current_line) - len(current_line.lstrip())]
    newline = "\r\n" if current_line.endswith("\r\n") else "\n"
    lines.insert(i + 1, f"{indent}{DRIVER_PROPERTY} \\{newline}")
    path.write_text("".join(lines), encoding="utf-8", newline="")


def main() -> None:
    if len(sys.argv) != 2:
        fail("usage: patch_qdec_hardware_debounce.py /path/to/zephyr")
    zephyr = Path(sys.argv[1]).resolve()
    binding = zephyr / BINDING_REL
    driver = zephyr / DRIVER_REL
    if not binding.is_file():
        fail(f"binding not found: {binding}")
    if not driver.is_file():
        fail(f"driver not found: {driver}")

    insert_binding_property(binding)
    insert_driver_property(driver)

    binding_text = binding.read_text(encoding="utf-8")
    driver_text = driver.read_text(encoding="utf-8")
    if "  debounce-filter:" not in binding_text:
        fail("binding property verification failed")
    if DRIVER_PROPERTY not in driver_text:
        fail("driver DBFEN initializer verification failed")
    print("QDEC hardware debounce property and driver support applied and verified.")


if __name__ == "__main__":
    main()
