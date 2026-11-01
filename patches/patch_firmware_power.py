#!/usr/bin/env python3
"""Apply narrow power fixes to this Corne's pinned ZMK/Zephyr modules.

Targets:
  1) Zephyr v3.5.0+zmk-fixes SAADC STOP/disable sequence, including clearing
     the pending IRQ through the nrfx glue API provided by this Zephyr tree.
  2) The manifest-pinned hal_nordic nrfx_spim.c Anomaly 195 workaround
     after every SPIM3 disable, not only at nrfx_spim_uninit().
  3) james-xenon/zmk-ws2812-driver main: restore the widget-owned external LED
     rail state after the widget blanks its strip on idle.

The script is fail-closed and idempotent. It validates all three sources before
writing any, keeps byte-for-byte backups, and migrates the previously generated
SAADC patch that called the unavailable irq_clear_pending() symbol.
"""
from __future__ import annotations

import argparse
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path


SAADC_PATCH_MARKER = b"ZMK_SAADC_LOW_POWER_STOP_SEQUENCE_V1"
SAADC_IRQ_MARKER = b"ZMK_SAADC_IRQ_PENDING_CLEAR_V2"
SAADC_INCLUDE_ANCHOR = b"#include <zephyr/irq.h>\n"
SAADC_GLUE_INCLUDE = b"#include <nrfx_glue.h>\n"
SAADC_STOP_ANCHOR = (
    b"\t\tnrf_saadc_task_trigger(NRF_SAADC, NRF_SAADC_TASK_STOP);\n"
    b"\t\tnrf_saadc_disable(NRF_SAADC);"
)
SAADC_OLD_IRQ_CLEAR = b"irq_clear_pending(DT_INST_IRQN(0));"
SAADC_NEW_IRQ_CLEAR = b"NRFX_IRQ_PENDING_CLEAR(DT_INST_IRQN(0));"


@dataclass
class Change:
    path: Path
    original: bytes
    updated: bytes
    marker: bytes


def _patch_saadc(path: Path) -> Change:
    if not path.is_file():
        raise RuntimeError(f"file not found: {path}")

    original = path.read_bytes()
    updated = original

    # The first published version inserted the correct STOPPED-event clear,
    # but used irq_clear_pending(), which is not implemented by the pinned
    # Zephyr v3.5.0 kernel. Migrate that version in place as well as patching
    # a pristine source tree.
    if SAADC_PATCH_MARKER in updated:
        if SAADC_OLD_IRQ_CLEAR in updated:
            if updated.count(SAADC_OLD_IRQ_CLEAR) != 1:
                raise RuntimeError(
                    f"{path}: expected one legacy irq_clear_pending() call"
                )
            updated = updated.replace(SAADC_OLD_IRQ_CLEAR, SAADC_NEW_IRQ_CLEAR, 1)
        elif SAADC_NEW_IRQ_CLEAR not in updated:
            raise RuntimeError(
                f"{path}: SAADC patch marker exists but IRQ-clear call is unknown"
            )
    else:
        count = updated.count(SAADC_STOP_ANCHOR)
        if count != 1:
            raise RuntimeError(
                f"{path}: expected exactly one SAADC STOP/disable anchor, found {count}"
            )
        replacement = (
            b"\t\tnrf_saadc_task_trigger(NRF_SAADC, NRF_SAADC_TASK_STOP);\n"
            b"\t\t/* ZMK_SAADC_LOW_POWER_STOP_SEQUENCE_V1\n"
            b"\t\t * Clear STOPPED and the latched NVIC pending bit before disabling\n"
            b"\t\t * the SAADC analog core. Use the IRQ-clear API from nrfx_glue.\n"
            b"\t\t */\n"
            b"\t\tnrf_saadc_event_clear(NRF_SAADC, NRF_SAADC_EVENT_STOPPED);\n"
            b"\t\t/* ZMK_SAADC_IRQ_PENDING_CLEAR_V2 */\n"
            b"\t\tNRFX_IRQ_PENDING_CLEAR(DT_INST_IRQN(0));\n"
            b"\t\tnrf_saadc_disable(NRF_SAADC);"
        )
        updated = updated.replace(SAADC_STOP_ANCHOR, replacement, 1)

    if SAADC_GLUE_INCLUDE not in updated:
        count = updated.count(SAADC_INCLUDE_ANCHOR)
        if count != 1:
            raise RuntimeError(
                f"{path}: expected one <zephyr/irq.h> include anchor, found {count}"
            )
        updated = updated.replace(
            SAADC_INCLUDE_ANCHOR,
            SAADC_INCLUDE_ANCHOR + SAADC_GLUE_INCLUDE,
            1,
        )

    if SAADC_NEW_IRQ_CLEAR not in updated:
        raise RuntimeError(f"{path}: NRFX IRQ pending-clear call was not installed")
    if SAADC_OLD_IRQ_CLEAR in updated:
        raise RuntimeError(f"{path}: obsolete irq_clear_pending() call remains")
    if SAADC_IRQ_MARKER not in updated:
        # Preserve the existing workflow's V1 marker check, while adding a
        # second marker that identifies the fixed Zephyr IRQ-glue call.
        updated = updated.replace(
            SAADC_NEW_IRQ_CLEAR,
            b"/* " + SAADC_IRQ_MARKER + b" */\n\t\t" + SAADC_NEW_IRQ_CLEAR,
            1,
        )

    return Change(path, original, updated, SAADC_PATCH_MARKER)


def build_changes(paths: list[Path]) -> list[Change]:
    if len(paths) != 3:
        raise ValueError("exactly three source paths are required")

    saadc_path, spim_path, widget_path = paths
    changes: list[Change] = []
    errors: list[str] = []

    try:
        changes.append(_patch_saadc(saadc_path))
    except (OSError, RuntimeError) as exc:
        errors.append(str(exc))

    specs = [
        (
            spim_path,
            b"ZMK_SPIM3_ANOMALY_195_AFTER_EACH_DISABLE_V1",
            (
                b"        nrfy_spim_disable(p_spim);\n"
                b"    }"
            ),
            (
                b"        nrfy_spim_disable(p_spim);\n"
                b"#if NRFX_CHECK(USE_WORKAROUND_FOR_ANOMALY_195)\n"
                b"        /* ZMK_SPIM3_ANOMALY_195_AFTER_EACH_DISABLE_V1 */\n"
                b"        if (p_spim == NRF_SPIM3) {\n"
                b"            *(volatile uint32_t *)0x4002F004 = 1;\n"
                b"        }\n"
                b"#endif\n"
                b"    }"
            ),
            "nrfx SPIM disable block",
        ),
        (
            widget_path,
            b"ZMK_WS2812_IDLE_EXT_POWER_PATCH_V1",
            (
                b"\t\t\tled_strip_update_rgb(led_strip, output_pixels, WS2812_NUM_PIXELS);\n"
                b"\t\t}"
            ),
            (
                b"\t\t\tled_strip_update_rgb(led_strip, output_pixels, WS2812_NUM_PIXELS);\n"
                b"\t\t\t/* ZMK_WS2812_IDLE_EXT_POWER_PATCH_V1\n"
                b"\t\t\t * The frame is now black. Return the external LED rail to its\n"
                b"\t\t\t * pre-widget state; the ACTIVE path powers it before redraw.\n"
                b"\t\t\t */\n"
                b"\t\t\tif (normal_state_saved) {\n"
                b"\t\t\t\trestore_ext_power_if_needed(normal_ext_power_was_on,\n"
                b"\t\t\t\t\t\t\t    normal_underglow_was_on);\n"
                b"\t\t\t}\n"
                b"\t\t}"
            ),
            "WS2812 idle blank-frame block",
        ),
    ]

    # Validate the remaining two source files before writing any of them.
    for path, marker, anchor, replacement, description in specs:
        if not path.is_file():
            errors.append(f"file not found: {path}")
            continue
        original = path.read_bytes()
        if marker in original:
            changes.append(Change(path, original, original, marker))
            continue
        count = original.count(anchor)
        if count != 1:
            errors.append(
                f"{path}: expected exactly one {description} anchor, found {count}"
            )
            continue
        updated = original.replace(anchor, replacement, 1)
        if marker not in updated:
            errors.append(f"{path}: internal validation failed for marker {marker.decode()}")
            continue
        changes.append(Change(path, original, updated, marker))

    if errors:
        raise RuntimeError("No files changed:\n- " + "\n- ".join(errors))

    return changes


def atomic_write(path: Path, data: bytes, mode: int | None = None) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("saadc_source", type=Path, help="Zephyr drivers/adc/adc_nrfx_saadc.c")
    parser.add_argument("spim_source", type=Path, help="hal/nordic/nrfx/drivers/src/nrfx_spim.c")
    parser.add_argument("widget_source", type=Path, help="zmk-ws2812-driver/src/widget.c")
    args = parser.parse_args()

    paths = [args.saadc_source, args.spim_source, args.widget_source]
    changes = build_changes(paths)
    pending = [change for change in changes if change.updated != change.original]

    if not pending:
        print("SAADC, SPIM3 Anomaly 195 and WS2812 idle-power patches are already applied.")
        return 0

    # Create every backup before replacing any source file.
    backups: list[tuple[Path, bytes]] = []
    for change in pending:
        backup = change.path.with_name(change.path.name + ".before_power_patch.bak")
        if backup.exists():
            raise RuntimeError(f"backup already exists: {backup}; no source changed")
        backups.append((backup, change.original))

    created_backups: list[Path] = []
    try:
        for backup, original in backups:
            backup.write_bytes(original)
            created_backups.append(backup)
        for change in pending:
            mode = stat.S_IMODE(change.path.stat().st_mode)
            atomic_write(change.path, change.updated, mode)
    except Exception:
        # Restore originals if a later write fails; retain backups for diagnosis.
        for change in pending:
            if change.path.exists() and change.path.read_bytes() != change.original:
                atomic_write(change.path, change.original, stat.S_IMODE(change.path.stat().st_mode))
        raise

    for change in pending:
        print(f"Patched: {change.path}")
        print(f"Backup:  {change.path.name}.before_power_patch.bak")
    print("All three firmware power patches applied successfully.")
    print("SAADC IRQ clearing uses NRFX_IRQ_PENDING_CLEAR from the pinned Zephyr nrfx_glue.h.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(f"power patch: {exc}")
