#!/usr/bin/env python3
"""Apply only the two user-config changes needed by the Eyelash RGB16 build.

1) Move persistent Caps indication to logical column 0 (T/G/B):
     PERSISTENT_LAYER_CAPS_START = 0
     PERSISTENT_LAYER_CAPS_COUNT = 1
2) Put RGB hue/saturation controls into the four free service-layer slots before OUT_USB:
     RGB_HUD RGB_HUI RGB_SAD RGB_SAI

The script intentionally fails instead of guessing when the expected context is absent.
It is safe to run repeatedly (idempotent).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def fail(message: str) -> "None":
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(2)


def find_keymap(root: Path) -> Path:
    # Only patch the active user config. west.yml may also fetch an `eyelash_corne`
    # project containing its own example keymap; touching that would be wrong.
    config_root = root / "config"
    if not config_root.is_dir():
        fail(f"Config directory not found: {config_root}")

    matches = []
    for path in config_root.rglob("*.keymap"):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if "service_layer" in text and "RGB_EFF" in text and "BT_CLR_ALL" in text:
            matches.append(path)

    if not matches:
        fail("Could not find the service-layer keymap containing RGB_EFF and BT_CLR_ALL")
    if len(matches) > 1:
        fail("More than one matching keymap was found: " + ", ".join(map(str, matches)))
    return matches[0]


def patch_keymap(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    block_match = re.search(r"service_layer\s*\{.*?\n\s*\};", text, flags=re.S)
    if not block_match:
        fail(f"service_layer block was not found in {path}")

    block = block_match.group(0)
    wanted = ("RGB_HUD", "RGB_HUI", "RGB_SAD", "RGB_SAI")
    if all(token in block for token in wanted):
        print(f"OK: RGB hue/saturation controls are already present in {path}")
        return

    pattern = re.compile(
        r"(&bt\s+BT_CLR_ALL\s+)"
        r"&none\s+&none\s+&none\s+&none"
        r"(\s+&out\s+OUT_USB)"
    )

    replacement = (
        r"\1"
        r"&rgb_ug RGB_HUD   &rgb_ug RGB_HUI   "
        r"&rgb_ug RGB_SAD   &rgb_ug RGB_SAI"
        r"\2"
    )

    new_block, count = pattern.subn(replacement, block, count=1)
    if count != 1:
        fail(
            f"Expected four free &none slots between BT_CLR_ALL and OUT_USB in {path}; "
            "nothing was changed"
        )

    new_text = text[: block_match.start()] + new_block + text[block_match.end() :]
    path.write_text(new_text, encoding="utf-8")
    print(f"PATCHED: service-layer RGB_HUD/HUI/SAD/SAI -> {path}")


def find_widget(root: Path) -> Path:
    matches = []
    for path in root.rglob("widget.c"):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if "PERSISTENT_LAYER_CAPS_START" in text and "PERSISTENT_LAYER_CAPS_COUNT" in text:
            matches.append(path)

    if not matches:
        fail("Could not find widget.c containing PERSISTENT_LAYER_CAPS_START/COUNT")
    if len(matches) > 1:
        fail("More than one persistent-Caps widget.c was found: " + ", ".join(map(str, matches)))
    return matches[0]


def replace_define(text: str, name: str, value: int) -> tuple[str, int, str | None]:
    pattern = re.compile(rf"(^\s*#define\s+{re.escape(name)}\s+)(\d+)(\s*(?://.*)?$)", re.M)
    match = pattern.search(text)
    if not match:
        return text, 0, None
    old = match.group(2)
    new_text = text[: match.start(2)] + str(value) + text[match.end(2) :]
    return new_text, 1, old


def patch_widget(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    text, n_start, old_start = replace_define(text, "PERSISTENT_LAYER_CAPS_START", 0)
    if n_start != 1:
        fail(f"PERSISTENT_LAYER_CAPS_START define not found exactly once in {path}")

    text, n_count, old_count = replace_define(text, "PERSISTENT_LAYER_CAPS_COUNT", 1)
    if n_count != 1:
        fail(f"PERSISTENT_LAYER_CAPS_COUNT define not found exactly once in {path}")

    path.write_text(text, encoding="utf-8")
    print(
        "PATCHED: persistent Caps range "
        f"START {old_start}->0, COUNT {old_count}->1 -> {path}"
    )


def main() -> None:
    if len(sys.argv) != 2:
        fail("Usage: apply_user_config_patches.py <west-workspace-root>")

    root = Path(sys.argv[1]).expanduser().resolve()
    if not root.is_dir():
        fail(f"Workspace root does not exist: {root}")

    keymap = find_keymap(root)
    widget = find_widget(root)

    patch_keymap(keymap)
    patch_widget(widget)

    print("OK: user keymap/widget patches completed safely")


if __name__ == "__main__":
    main()
