#!/usr/bin/env python3
"""Validate a constrained theme profile and generate the app theme stylesheet.

The tool deliberately has one writable target. Its default/check and preview
modes are read-only so a coding agent can discuss the result before applying it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE = REPO_ROOT / "agent-config" / "theme-profile.example.json"
GENERATED_STYLESHEET = REPO_ROOT / "current" / "app" / "static" / "theme.generated.css"

TOP_LEVEL_KEYS = {
    "schema_version",
    "name",
    "font_preset",
    "density",
    "radius_rem",
    "light",
    "dark",
}
COLOR_KEYS = {
    "primary",
    "on_primary",
    "background",
    "text",
    "surface",
    "surface_alt",
    "border",
    "muted_text",
    "success",
    "danger",
    "warning",
}
FONT_PRESETS = {
    "system": 'system-ui, -apple-system, "Segoe UI", sans-serif',
    "humanist": '"Segoe UI", Candara, Calibri, sans-serif',
    "rounded": '"Avenir Next Rounded", "Arial Rounded MT Bold", system-ui, sans-serif',
    "serif": 'Georgia, "Times New Roman", serif',
}
DENSITY_TOKENS = {
    "comfortable": {"control_y": "0.5rem", "control_x": "0.75rem"},
    "compact": {"control_y": "0.35rem", "control_x": "0.6rem"},
}
HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


class ThemeProfileError(ValueError):
    """Raised when a profile could produce unsafe or invalid CSS."""


def _unknown_keys(mapping: dict[str, Any], expected: set[str], location: str) -> None:
    unknown = set(mapping) - expected
    missing = expected - set(mapping)
    if unknown:
        raise ThemeProfileError(f"Unknown {location} keys: {', '.join(sorted(unknown))}")
    if missing:
        raise ThemeProfileError(f"Missing {location} keys: {', '.join(sorted(missing))}")


def _normalize_hex(value: Any, field: str) -> str:
    if not isinstance(value, str) or not HEX_COLOR.fullmatch(value):
        raise ThemeProfileError(f"{field} must be a six-digit hex color such as #335CFF")
    return value.upper()


def _rgb(hex_color: str) -> tuple[int, int, int]:
    return tuple(int(hex_color[index : index + 2], 16) for index in (1, 3, 5))


def _rgb_css(hex_color: str) -> str:
    return ", ".join(str(channel) for channel in _rgb(hex_color))


def _relative_luminance(hex_color: str) -> float:
    channels = []
    for channel in _rgb(hex_color):
        normalized = channel / 255.0
        channels.append(
            normalized / 12.92
            if normalized <= 0.04045
            else ((normalized + 0.055) / 1.055) ** 2.4
        )
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast(foreground: str, background: str) -> float:
    first = _relative_luminance(foreground)
    second = _relative_luminance(background)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


def _require_contrast(foreground: str, background: str, minimum: float, label: str) -> None:
    ratio = _contrast(foreground, background)
    if ratio + 1e-9 < minimum:
        raise ThemeProfileError(
            f"{label} contrast is {ratio:.2f}:1; at least {minimum:.1f}:1 is required"
        )


def _mix(color: str, target: str, target_weight: float) -> str:
    source_rgb = _rgb(color)
    target_rgb = _rgb(target)
    mixed = tuple(
        round(source * (1.0 - target_weight) + destination * target_weight)
        for source, destination in zip(source_rgb, target_rgb)
    )
    return "#" + "".join(f"{channel:02X}" for channel in mixed)


def _safe_css_comment(value: str) -> str:
    """Keep a display name informative without allowing it to end a CSS comment."""
    return " ".join(value.replace("*/", "* /").split())[:120]


def _normalize_palette(raw: Any, mode: str) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ThemeProfileError(f"{mode} must be an object")
    _unknown_keys(raw, COLOR_KEYS, f"{mode} palette")
    palette = {key: _normalize_hex(raw[key], f"{mode}.{key}") for key in COLOR_KEYS}

    for background_key in ("background", "surface", "surface_alt"):
        _require_contrast(
            palette["text"], palette[background_key], 4.5, f"{mode} text on {background_key}"
        )
        _require_contrast(
            palette["muted_text"],
            palette[background_key],
            4.5,
            f"{mode} muted text on {background_key}",
        )
    _require_contrast(
        palette["on_primary"], palette["primary"], 4.5, f"{mode} on-primary text"
    )
    emphasis_colors = ("primary", "success", "danger", "warning")
    for emphasis in emphasis_colors:
        label = "primary/link" if emphasis == "primary" else emphasis
        for background_key in ("background", "surface", "surface_alt"):
            _require_contrast(
                palette[emphasis],
                palette[background_key],
                4.5,
                f"{mode} {label} color on {background_key}",
            )
    for semantic in ("success", "danger", "warning"):
        _require_contrast(
            palette["on_primary"],
            palette[semantic],
            3.0,
            f"{mode} on-primary text on {semantic}",
        )
    return palette


def load_and_validate(profile_path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(profile_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ThemeProfileError(f"Theme profile does not exist: {profile_path}") from exc
    except json.JSONDecodeError as exc:
        raise ThemeProfileError(f"Invalid JSON in {profile_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ThemeProfileError("Theme profile must be a JSON object")
    _unknown_keys(raw, TOP_LEVEL_KEYS, "top-level")
    if raw["schema_version"] != 1:
        raise ThemeProfileError("schema_version must be 1")
    if not isinstance(raw["name"], str) or not raw["name"].strip():
        raise ThemeProfileError("name must be a non-empty string")
    if raw["font_preset"] not in FONT_PRESETS:
        raise ThemeProfileError(f"font_preset must be one of: {', '.join(FONT_PRESETS)}")
    if raw["density"] not in DENSITY_TOKENS:
        raise ThemeProfileError(f"density must be one of: {', '.join(DENSITY_TOKENS)}")
    if isinstance(raw["radius_rem"], bool) or not isinstance(raw["radius_rem"], (int, float)):
        raise ThemeProfileError("radius_rem must be a number between 0 and 1.5")
    radius = float(raw["radius_rem"])
    if not 0 <= radius <= 1.5:
        raise ThemeProfileError("radius_rem must be between 0 and 1.5")

    return {
        "schema_version": 1,
        "name": raw["name"].strip(),
        "font_preset": raw["font_preset"],
        "density": raw["density"],
        "radius_rem": radius,
        "light": _normalize_palette(raw["light"], "light"),
        "dark": _normalize_palette(raw["dark"], "dark"),
    }


def _palette_css(mode: str, palette: dict[str, str]) -> str:
    primary_hover = _mix(palette["primary"], "#000000" if mode == "light" else "#FFFFFF", 0.14)
    primary_active = _mix(palette["primary"], "#000000" if mode == "light" else "#FFFFFF", 0.22)
    return f'''[data-bs-theme="{mode}"] {{
  --app-primary: {palette["primary"]};
  --app-primary-rgb: {_rgb_css(palette["primary"])};
  --app-on-primary: {palette["on_primary"]};
  --app-primary-hover: {primary_hover};
  --app-primary-active: {primary_active};
  --app-surface: {palette["surface"]};
  --app-surface-alt: {palette["surface_alt"]};
  --bs-primary: {palette["primary"]};
  --bs-primary-rgb: {_rgb_css(palette["primary"])};
  --bs-link-color: {palette["primary"]};
  --bs-link-color-rgb: {_rgb_css(palette["primary"])};
  --bs-link-hover-color: {primary_hover};
  --bs-link-hover-color-rgb: {_rgb_css(primary_hover)};
  --bs-body-bg: {palette["background"]};
  --bs-body-color: {palette["text"]};
  --bs-secondary-bg: {palette["surface"]};
  --bs-tertiary-bg: {palette["surface_alt"]};
  --bs-border-color: {palette["border"]};
  --bs-secondary-color: {palette["muted_text"]};
  --bs-success: {palette["success"]};
  --bs-success-rgb: {_rgb_css(palette["success"])};
  --bs-success-text-emphasis: {palette["success"]};
  --bs-danger: {palette["danger"]};
  --bs-danger-rgb: {_rgb_css(palette["danger"])};
  --bs-danger-text-emphasis: {palette["danger"]};
  --bs-warning: {palette["warning"]};
  --bs-warning-rgb: {_rgb_css(palette["warning"])};
  --bs-warning-text-emphasis: {palette["warning"]};
}}'''


def render_stylesheet(profile: dict[str, Any]) -> str:
    font_stack = FONT_PRESETS[profile["font_preset"]]
    density = DENSITY_TOKENS[profile["density"]]
    radius = f'{profile["radius_rem"]:.3f}'.rstrip("0").rstrip(".")
    theme_name = _safe_css_comment(profile["name"])
    return f'''/* Generated by tools/customize_theme.py from a validated profile.
   Theme: {theme_name}
   Do not edit this file directly; update the profile and run --apply. */

:root {{
  --app-font-family: {font_stack};
  --app-radius: {radius}rem;
  --app-control-padding-y: {density["control_y"]};
  --app-control-padding-x: {density["control_x"]};
}}

{_palette_css("light", profile["light"])}

{_palette_css("dark", profile["dark"])}

body {{
  font-family: var(--app-font-family);
}}

[data-bs-theme] .navbar-brand {{
  color: var(--app-primary);
}}

[data-bs-theme] .btn-primary {{
  --bs-btn-color: var(--app-on-primary);
  --bs-btn-bg: var(--app-primary);
  --bs-btn-border-color: var(--app-primary);
  --bs-btn-hover-color: var(--app-on-primary);
  --bs-btn-hover-bg: var(--app-primary-hover);
  --bs-btn-hover-border-color: var(--app-primary-hover);
  --bs-btn-active-color: var(--app-on-primary);
  --bs-btn-active-bg: var(--app-primary-active);
  --bs-btn-active-border-color: var(--app-primary-active);
}}

[data-bs-theme] .btn-outline-primary {{
  --bs-btn-color: var(--app-primary);
  --bs-btn-border-color: var(--app-primary);
  --bs-btn-hover-color: var(--app-on-primary);
  --bs-btn-hover-bg: var(--app-primary);
  --bs-btn-hover-border-color: var(--app-primary);
  --bs-btn-active-color: var(--app-on-primary);
  --bs-btn-active-bg: var(--app-primary-active);
  --bs-btn-active-border-color: var(--app-primary-active);
}}

[data-bs-theme] .form-control:focus,
[data-bs-theme] .form-select:focus,
[data-bs-theme] .form-check-input:focus {{
  border-color: var(--app-primary);
  box-shadow: 0 0 0 0.2rem rgba(var(--app-primary-rgb), 0.25);
}}

[data-bs-theme] .form-check-input:checked {{
  background-color: var(--app-primary);
  border-color: var(--app-primary);
}}

[data-bs-theme] .card,
[data-bs-theme] .modal-content,
[data-bs-theme] .dropdown-menu,
[data-bs-theme] .list-group-item {{
  background-color: var(--app-surface);
  border-color: var(--bs-border-color);
}}

[data-bs-theme] .card,
[data-bs-theme] .modal-content,
[data-bs-theme] .btn,
[data-bs-theme] .form-control,
[data-bs-theme] .form-select {{
  border-radius: var(--app-radius);
}}

[data-bs-theme] .btn:not(.btn-sm):not(.btn-lg),
[data-bs-theme] .form-control:not(.form-control-sm):not(.form-control-lg),
[data-bs-theme] .form-select:not(.form-select-sm):not(.form-select-lg) {{
  padding: var(--app-control-padding-y) var(--app-control-padding-x);
}}
'''


def _resolve_profile(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path.resolve()
    from_working_directory = (Path.cwd() / path).resolve()
    if from_working_directory.exists():
        return from_working_directory
    return (REPO_ROOT / path).resolve()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate a theme profile and safely generate theme.generated.css."
    )
    parser.add_argument(
        "--profile",
        default=str(DEFAULT_PROFILE),
        help="JSON theme profile; defaults to the tracked synthetic example",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Validate and verify generated CSS is current")
    mode.add_argument("--preview", action="store_true", help="Print CSS without writing")
    mode.add_argument("--apply", action="store_true", help="Replace only the dedicated generated stylesheet")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        profile_path = _resolve_profile(args.profile)
        profile = load_and_validate(profile_path)
        rendered = render_stylesheet(profile)
    except (OSError, ThemeProfileError) as exc:
        print(f"Theme profile error: {exc}", file=sys.stderr)
        return 2

    if args.preview:
        print(rendered, end="")
        return 0

    if args.apply:
        GENERATED_STYLESHEET.parent.mkdir(parents=True, exist_ok=True)
        temporary = GENERATED_STYLESHEET.with_suffix(".css.tmp")
        temporary.write_text(rendered, encoding="utf-8", newline="\n")
        temporary.replace(GENERATED_STYLESHEET)
        print(f"Applied validated theme '{profile['name']}' to {GENERATED_STYLESHEET.relative_to(REPO_ROOT)}")
        return 0

    if not GENERATED_STYLESHEET.exists():
        print(
            f"Theme profile is valid, but {GENERATED_STYLESHEET.relative_to(REPO_ROOT)} is missing. "
            "Run with --apply.",
            file=sys.stderr,
        )
        return 1
    if GENERATED_STYLESHEET.read_text(encoding="utf-8") != rendered:
        print(
            f"Theme profile is valid, but {GENERATED_STYLESHEET.relative_to(REPO_ROOT)} is stale. "
            "Review --preview, then run --apply.",
            file=sys.stderr,
        )
        return 1

    print(
        f"Theme profile '{profile['name']}' is valid and the generated stylesheet is current."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
