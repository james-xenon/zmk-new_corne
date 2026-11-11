#!/usr/bin/env python3
"""Apply narrow low-power fixes to this Corne's pinned ZMK/Zephyr modules.

Targets:
  1) Zephyr v3.5.0+zmk-fixes SAADC STOP/disable sequence, including clearing
     the pending IRQ through nrfx_glue.
  2) The manifest-pinned hal_nordic nrfx_spim.c Anomaly 195 workaround
     after every SPIM3 disable.
  3) james-xenon/zmk-ws2812-driver: power the WS2812 rail down while the
     widget's idle display is blank, restore the rail before wake redraw, and
     turn the rail off after boot when neither underglow nor static widget
     lighting needs it.

The script is fail-closed and idempotent. It validates all source anchors before
writing any file, creates byte-for-byte backups, and refuses to stack the new
WS2812 V3 patch on top of the older V1/V2 patch.
"""
from __future__ import annotations

import argparse
import os
import re
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

SPIM_PATCH_MARKER = b"ZMK_SPIM3_ANOMALY_195_AFTER_EACH_DISABLE_V1"

WIDGET_PATCH_MARKER = b"ZMK_WS2812_IDLE_EXT_POWER_PATCH_V3"
WIDGET_OLD_PATCH_MARKERS = (
    b"ZMK_WS2812_IDLE_EXT_POWER_PATCH_V1",
    b"ZMK_WS2812_IDLE_EXT_POWER_PATCH_V2",
)


@dataclass
class Change:
    path: Path
    original: bytes
    updated: bytes
    marker: bytes


def _require_once(source: bytes, anchor: bytes, path: Path, description: str) -> None:
    count = source.count(anchor)
    if count != 1:
        raise RuntimeError(
            f"{path}: expected exactly one {description} anchor, found {count}"
        )


def _patch_saadc(path: Path) -> Change:
    if not path.is_file():
        raise RuntimeError(f"file not found: {path}")

    original = path.read_bytes()
    updated = original

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
        _require_once(updated, SAADC_STOP_ANCHOR, path, "SAADC STOP/disable")
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
        _require_once(updated, SAADC_INCLUDE_ANCHOR, path, "<zephyr/irq.h> include")
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
        updated = updated.replace(
            SAADC_NEW_IRQ_CLEAR,
            b"/* " + SAADC_IRQ_MARKER + b" */\n\t\t" + SAADC_NEW_IRQ_CLEAR,
            1,
        )

    return Change(path, original, updated, SAADC_PATCH_MARKER)


def _patch_spim(path: Path) -> Change:
    if not path.is_file():
        raise RuntimeError(f"file not found: {path}")

    original = path.read_bytes()
    if SPIM_PATCH_MARKER in original:
        return Change(path, original, original, SPIM_PATCH_MARKER)

    anchor = (
        b"        nrfy_spim_disable(p_spim);\n"
        b"    }"
    )
    replacement = (
        b"        nrfy_spim_disable(p_spim);\n"
        b"#if NRFX_CHECK(USE_WORKAROUND_FOR_ANOMALY_195)\n"
        b"        /* ZMK_SPIM3_ANOMALY_195_AFTER_EACH_DISABLE_V1 */\n"
        b"        if (p_spim == NRF_SPIM3) {\n"
        b"            *(volatile uint32_t *)0x4002F004 = 1;\n"
        b"        }\n"
        b"#endif\n"
        b"    }"
    )
    _require_once(original, anchor, path, "nrfx SPIM disable block")
    updated = original.replace(anchor, replacement, 1)

    if SPIM_PATCH_MARKER not in updated:
        raise RuntimeError(f"{path}: SPIM3 patch marker was not installed")

    return Change(path, original, updated, SPIM_PATCH_MARKER)


def _regex_sub_once(source: bytes, pattern: bytes, replacement: bytes,
                    path: Path, description: str, flags: int = 0) -> bytes:
    updated, count = re.subn(pattern, replacement, source, count=1, flags=flags)
    if count != 1:
        raise RuntimeError(
            f"{path}: expected exactly one {description} regex match, found {count}"
        )
    return updated


def _patch_widget(path: Path) -> Change:
    if not path.is_file():
        raise RuntimeError(f"file not found: {path}")

    original = path.read_bytes()

    if WIDGET_PATCH_MARKER in original:
        return Change(path, original, original, WIDGET_PATCH_MARKER)

    for old_marker in WIDGET_OLD_PATCH_MARKERS:
        if old_marker in original:
            raise RuntimeError(
                f"{path}: old WS2812 idle-power patch {old_marker.decode()} is already present. "
                "Restore pristine zmk-ws2812-driver src/widget.c before applying V3."
            )

    helper_anchor = (
        b"/* -------------------------------------------------------------------------\n"
        b"Persistent/static state\n"
        b"------------------------------------------------------------------------- */\n"
    )
    _require_once(original, helper_anchor, path, "WS2812 persistent/static section")

    helper_block = r'''/* ZMK_WS2812_IDLE_EXT_POWER_PATCH_V3
 * The strip can be visually black while its external rail is still powered.
 * This keyboard uses EXT_POWER for that rail, so idle explicitly removes rail
 * power after sending the black frame. Wake restores the rail before the
 * underglow/static redraw, but leaves it off when there is nothing to display.
 *
 * ext_power_enable()/disable() are used deliberately so the driver's logical
 * state stays synchronized with the GPIO. The ZMK settings save is delayed and
 * coalesced by CONFIG_ZMK_SETTINGS_SAVE_DEBOUNCE.
 */
static bool ws2812_lighting_needs_ext_power(void) {
	if (static_lighting_needed()) {
		return true;
	}

#if IS_ENABLED(CONFIG_ZMK_RGB_UNDERGLOW)
	bool underglow_on = false;
	if (zmk_rgb_underglow_get_state(&underglow_on) == 0 && underglow_on) {
		return true;
	}
#endif

	return false;
}

static bool ws2812_ext_power_prepare(bool need_power) {
#if IS_ENABLED(CONFIG_WS2812_WIDGET_USE_EXT_POWER) && IS_ENABLED(CONFIG_ZMK_EXT_POWER)
	const struct device *ext_power = get_ext_power_device();
	if (ext_power == NULL) {
		LOG_WRN("EXT_POWER device not found");
		return true;
	}

	bool is_on = ext_power_get(ext_power) > 0;

	if (!need_power) {
		if (is_on) {
			int rc = ext_power_disable(ext_power);
			if (rc != 0) {
				LOG_WRN("Failed to disable idle WS2812 EXT_POWER: %d", rc);
			}
		}
		return false;
	}

	if (!is_on) {
		int rc = ext_power_enable(ext_power);
		if (rc != 0) {
			LOG_WRN("Failed to enable WS2812 EXT_POWER: %d", rc);
			return false;
		}
		k_sleep(K_MSEC(CONFIG_WS2812_WIDGET_EXT_POWER_STARTUP_DELAY_MS));
	}

	return true;
#else
	ARG_UNUSED(need_power);
	return true;
#endif
}

static void ws2812_idle_ext_power_suspend(void) {
#if IS_ENABLED(CONFIG_WS2812_RGB_IDLE_TIMER)
	(void)ws2812_ext_power_prepare(false);
#else
	if (!ws2812_lighting_needs_ext_power()) {
		(void)ws2812_ext_power_prepare(false);
	}
#endif
}

static bool ws2812_idle_ext_power_prepare_wake(void) {
	bool need_power = idle_underglow_suspended || static_lighting_needed();
	return ws2812_ext_power_prepare(need_power);
}

static void ws2812_ext_power_disable_if_unused(void) {
	if (!ws2812_lighting_needs_ext_power()) {
		(void)ws2812_ext_power_prepare(false);
	}
}

'''.encode("utf-8") + helper_anchor

    updated = original.replace(helper_anchor, helper_block, 1)

    idle_black_pattern = (
        rb"(?m)^([ \t]*)led_strip_update_rgb\(led_strip, output_pixels, WS2812_NUM_PIXELS\);\r?\n"
        rb"([ \t]*)\}\r?\n"
        rb"([ \t]*)\} else \{"
    )
    idle_black_replacement = (
        b"\\1led_strip_update_rgb(led_strip, output_pixels, WS2812_NUM_PIXELS);\n"
        b"\\1ws2812_idle_ext_power_suspend();\n"
        b"\\2}\n"
        b"\\3} else {"
    )
    updated = _regex_sub_once(
        updated, idle_black_pattern, idle_black_replacement,
        path, "WS2812 idle black-frame block", re.MULTILINE
    )

    wake_pattern = (
        rb"(?ms)^[ \t]*if \(idle_display_off\) \{\r?\n"
        rb"[ \t]*idle_display_off = false;\r?\n"
        rb"[ \t]*if \(idle_underglow_suspended\) \{\r?\n"
        rb"[ \t]*idle_underglow_suspended = false;\r?\n"
        rb"[ \t]*restore_underglow_if_needed\(true\);\r?\n"
        rb"[ \t]*\} else \{\r?\n"
        rb"[ \t]*/\* pixels\[\] keeps the logical static/indicator state\. Re-apply\r?\n"
        rb"[ \t]*\* that state when the widget owns the strip\. \*/\r?\n"
        rb"[ \t]*flush_pixels\(\);\r?\n"
        rb"[ \t]*\}\r?\n"
        rb"[ \t]*\}"
    )
    wake_replacement = (
        b"\t\tif (idle_display_off) {\n"
        b"\t\t\tidle_display_off = false;\n"
        b"\t\t\tbool ext_power_ready = ws2812_idle_ext_power_prepare_wake();\n"
        b"\t\t\tif (idle_underglow_suspended) {\n"
        b"\t\t\t\tidle_underglow_suspended = false;\n"
        b"\t\t\t\tif (ext_power_ready) {\n"
        b"\t\t\t\t\trestore_underglow_if_needed(true);\n"
        b"\t\t\t\t}\n"
        b"\t\t\t} else if (ext_power_ready) {\n"
        b"\t\t\t\t/* pixels[] keeps the logical static/indicator state. Re-apply\n"
        b"\t\t\t\t * that state when the widget owns the strip. */\n"
        b"\t\t\t\tflush_pixels();\n"
        b"\t\t\t}\n"
        b"\t\t}"
    )
    updated = _regex_sub_once(
        updated, wake_pattern, wake_replacement,
        path, "WS2812 idle wake block", re.MULTILINE | re.DOTALL
    )

    startup_pattern = (
        rb"(?ms)([ \t]*/\* User config has underglow OFF on start; this guarantees the widget\r?\n"
        rb"[ \t]*\* itself starts from a known black frame without changing ZMK state\. \*/\r?\n"
        rb"[ \t]*set_all_pixels\(\(struct led_rgb\)\{0, 0, 0\}\);\r?\n)"
    )
    startup_replacement = b"\\1\t\tws2812_ext_power_disable_if_unused();\n"
    updated = _regex_sub_once(
        updated, startup_pattern, startup_replacement,
        path, "WS2812 startup black-frame block", re.MULTILINE | re.DOTALL
    )

    required = (
        WIDGET_PATCH_MARKER,
        b"ws2812_idle_ext_power_suspend();",
        b"ws2812_idle_ext_power_prepare_wake();",
        b"ws2812_ext_power_disable_if_unused();",
        b"bool ext_power_ready = ws2812_idle_ext_power_prepare_wake();",
    )
    for marker in required:
        if marker not in updated:
            raise RuntimeError(f"{path}: WS2812 V3 validation failed: {marker!r}")

    return Change(path, original, updated, WIDGET_PATCH_MARKER)

def build_changes(paths: list[Path]) -> list[Change]:
    if len(paths) != 3:
        raise ValueError("exactly three source paths are required")

    saadc_path, spim_path, widget_path = paths
    changes: list[Change] = []
    errors: list[str] = []

    for patcher, path in (
        (_patch_saadc, saadc_path),
        (_patch_spim, spim_path),
        (_patch_widget, widget_path),
    ):
        try:
            changes.append(patcher(path))
        except (OSError, RuntimeError) as exc:
            errors.append(str(exc))

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
        print("SAADC, SPIM3 and WS2812 idle-power V3 patches are already applied.")
        return 0

    backups: list[tuple[Path, bytes]] = []
    for change in pending:
        backup = change.path.with_name(change.path.name + ".before_power_patch.bak")
        if backup.exists():
            raise RuntimeError(f"backup already exists: {backup}; no source changed")
        backups.append((backup, change.original))

    try:
        for backup, original in backups:
            backup.write_bytes(original)
        for change in pending:
            mode = stat.S_IMODE(change.path.stat().st_mode)
            atomic_write(change.path, change.updated, mode)
    except Exception:
        for change in pending:
            if change.path.exists() and change.path.read_bytes() != change.original:
                atomic_write(
                    change.path,
                    change.original,
                    stat.S_IMODE(change.path.stat().st_mode),
                )
        raise

    for change in pending:
        print(f"Patched: {change.path}")
        print(f"Backup:  {change.path.name}.before_power_patch.bak")

    print("All firmware power patches applied successfully.")
    print("SAADC IRQ clearing uses NRFX_IRQ_PENDING_CLEAR.")
    print("SPIM3 Anomaly 195 workaround runs after every SPIM3 disable.")
    print("WS2812 V3 removes rail power during idle and restores it before wake redraw.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(f"power patch: {exc}")
