"""Light/dark colors for the bar, resolved once per render.

Colors are named by ROLE ("bar background", "muted text") rather than by
hex value, so render.py never carries a bare "#2b2b2b" and this table is
the single place a palette change happens.

Why Python-side resolution rather than CSS variables + media queries:
render.py's output is injected into the reviewer's card webview, so a
CSS-only approach is possible - but it would have to guess which element
Anki hangs its night-mode class on (that has moved between versions), and
prefers-color-scheme is the OS's signal, not Anki's, so honoring it makes
the bar disagree with the rest of Anki whenever the two differ. Asking
Anki directly is both simpler and authoritative. A card re-renders on
every answer shown, so the resolved palette is never meaningfully stale.

is_night_mode() deliberately probes several aqt entry points: the exact
one varies by Anki version, this add-on targets no single version, and
guessing wrong should not be fatal. If every probe fails it reports dark,
which is what this bar looked like before it was themeable at all.
"""

from __future__ import annotations

from typing import NamedTuple


class ThemeTokens(NamedTuple):
    """One color role's light and dark values, as CSS color strings."""
    light: str
    dark: str


TOKENS: dict[str, ThemeTokens] = {
    "bg": ThemeTokens(light="#f2f2f2", dark="#2b2b2b"),
    "text": ThemeTokens(light="#1f1f1f", dark="#dddddd"),
    "text-strong": ThemeTokens(light="#000000", dark="#ffffff"),
    "text-muted": ThemeTokens(light="rgba(0,0,0,0.55)", dark="rgba(255,255,255,0.55)"),
    "border": ThemeTokens(light="rgba(0,0,0,0.18)", dark="rgba(127,127,127,0.3)"),
    "separator": ThemeTokens(light="rgba(0,0,0,0.12)", dark="rgba(255,255,255,0.12)"),
    "tooltip-bg": ThemeTokens(light="#ffffff", dark="#3d3d3d"),
    "tooltip-text": ThemeTokens(light="#1f1f1f", dark="#eeeeee"),
    "tooltip-border": ThemeTokens(light="rgba(0,0,0,0.15)", dark="rgba(255,255,255,0.18)"),
    "tooltip-shadow": ThemeTokens(light="rgba(0,0,0,0.18)", dark="rgba(0,0,0,0.4)"),
}


def is_night_mode() -> bool:
    """Whether Anki is currently in night mode. Falls back to True (dark)
    if no probe succeeds - see the module docstring."""
    try:
        from aqt.theme import theme_manager
    except Exception:
        theme_manager = None

    if theme_manager is not None:
        # night_mode is a plain attribute on current versions; older ones
        # exposed the same idea through get_night_mode(). Attribute first,
        # since it's the form that exists today.
        for probe in ("night_mode", "get_night_mode"):
            value = getattr(theme_manager, probe, None)
            if value is None:
                continue
            try:
                return bool(value() if callable(value) else value)
            except Exception:
                continue

    # Profile-level setting, for versions where theme_manager isn't
    # importable or doesn't answer.
    try:
        from aqt import mw
        night_mode = getattr(mw.pm, "night_mode", None)
        if night_mode is not None:
            return bool(night_mode() if callable(night_mode) else night_mode)
    except Exception:
        pass

    return True


def palette(night: bool | None = None) -> dict[str, str]:
    """Every token resolved to one concrete color for the active theme.
    Pass `night` to override detection (tests, previews)."""
    if night is None:
        night = is_night_mode()
    return {name: (t.dark if night else t.light) for name, t in TOKENS.items()}
