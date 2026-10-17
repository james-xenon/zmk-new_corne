#!/usr/bin/env python3
"""Use a faster hardware QDEC sample period in the pinned Zephyr source.

Only the nRF QDEC sampling interval changes. The hardware debounce filter remains disabled.
Usage: python3 patch_qdec_sampling.py /path/to/zephyr
"""
from __future__ import annotations

import sys
from pathlib import Path

DRIVER_REL = Path("drivers/sensor/qdec_nrfx/qdec_nrfx.c")
OLD = ".sampleper = NRF_QDEC_SAMPLEPER_2048US,"
NEW = ".sampleper = NRF_QDEC_SAMPLEPER_256US,"
REPORT = ".reportper = NRF_QDEC_REPORTPER_40,"


def fail(message: str) -> "NoReturn":
    raise SystemExit(f"QDEC sampling patch: ERROR: {message}")


def main() -> None:
    if len(sys.argv) != 2:
        fail("usage: patch_qdec_sampling.py /path/to/zephyr")

    zephyr = Path(sys.argv[1]).resolve()
    driver = zephyr / DRIVER_REL
    if not driver.is_file():
        fail(f"driver not found: {driver}")

    text = driver.read_text(encoding="utf-8")
    old_count = text.count(OLD)
    new_count = text.count(NEW)

    if old_count == 1 and new_count == 0:
        text = text.replace(OLD, NEW, 1)
        driver.write_text(text, encoding="utf-8", newline="\n")
    elif old_count == 0 and new_count == 1:
        # Idempotent for repeated runs in the same workspace.
        pass
    else:
        fail(
            "expected exactly one stock 2048-us initializer or one already-patched "
            f"256-us initializer; found old={old_count}, new={new_count}. "
            "The pinned Zephyr source may have changed."
        )

    final = driver.read_text(encoding="utf-8")
    if final.count(NEW) != 1:
        fail("256-us SAMPLEPER verification failed")
    if final.count(REPORT) != 1:
        fail("expected exactly one REPORTPER_40 initializer; refusing to change report cadence")
    if ".dbfen =" in final:
        # Stock pinned source does not explicitly set dbfen. Don't retain custom filter code.
        fail("unexpected custom dbfen initializer present; restore the pinned driver source first")

    print("QDEC sampling patch verified: SAMPLEPER=256us, REPORTPER=40 samples, hardware debounce unchanged/off.")


if __name__ == "__main__":
    main()
