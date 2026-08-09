"""Settings dialog: lets the user set the highlight color and the list of
word fields to scan, without hand-editing config.json. Opened from Anki's
Tools menu (see __init__.py's gui_hooks.main_window_did_init hook, which
adds the menu item).

Kept as its own module rather than folded into __init__.py so the Qt
widget-building code (a real chunk of it) doesn't clutter the entry point -
same rationale as the render.py / collection_data.py / text_utils.py split.
"""

from __future__ import annotations

import re

from aqt.qt import (
    QColor,
    QColorDialog,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    Qt,
)

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class SettingsDialog(QDialog):
    """Two fields: highlight color (hex text + swatch + native picker
    button) and word fields (comma-separated). Saves straight to
    config.json via addonManager on OK - the caller (__init__.py) re-reads
    config and rebuilds its indexes/state after the dialog closes, since
    this dialog has no reference to the live _reading_index/_known/etc.
    singletons and shouldn't need one just to persist a setting."""

    def __init__(self, mw, addon_name: str, current_color: str, current_fields: list[str]):
        super().__init__(mw)
        self.mw = mw
        self.addon_name = addon_name
        self.setWindowTitle("Dynamic Kanji Companion Settings")
        self.setMinimumWidth(420)

        self._color = current_color

        layout = QVBoxLayout(self)
        form = QFormLayout()

        # --- Highlight color: hex field + live swatch + native picker ---
        color_row = QHBoxLayout()
        self.color_edit = QLineEdit(current_color)
        self.color_edit.setMaxLength(7)
        self.color_edit.textChanged.connect(self._on_hex_typed)
        self.swatch = QLabel()
        self.swatch.setFixedSize(22, 22)
        self._update_swatch(current_color)
        pick_btn = QPushButton("Choose…")
        pick_btn.clicked.connect(self._open_color_picker)
        color_row.addWidget(self.color_edit)
        color_row.addWidget(self.swatch)
        color_row.addWidget(pick_btn)
        form.addRow("Highlight color:", color_row)

        # --- Word fields: comma-separated, with a short explanation ---
        self.fields_edit = QLineEdit(", ".join(current_fields))
        form.addRow("Word fields:", self.fields_edit)

        explanation = QLabel(
            "Comma-separated list of note field names to scan for kanji, in "
            "priority order - the first field on a note that isn't empty is "
            "used, and the rest are ignored for that note. A field's text "
            "can use 「漢字[かな]」 bracket notation "
            "to give an exact reading for a single kanji; whole words work "
            "too, just without that per-kanji precision."
        )
        explanation.setWordWrap(True)
        explanation.setStyleSheet("color: palette(mid); font-size: 11px;")
        form.addRow("", explanation)

        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _update_swatch(self, hex_color: str) -> None:
        if _HEX_COLOR_RE.match(hex_color):
            self.swatch.setStyleSheet(
                "background:{}; border:1px solid rgba(127,127,127,0.5); "
                "border-radius:3px;".format(hex_color))
        else:
            self.swatch.setStyleSheet(
                "background:transparent; border:1px solid rgba(127,127,127,0.5); "
                "border-radius:3px;")

    def _on_hex_typed(self, text: str) -> None:
        self._update_swatch(text)

    def _open_color_picker(self) -> None:
        start = QColor(self._color) if _HEX_COLOR_RE.match(self.color_edit.text()) \
            else QColor("#ff007b")
        chosen = QColorDialog.getColor(start, self, "Choose highlight color")
        if chosen.isValid():
            hex_color = chosen.name()
            self.color_edit.setText(hex_color)
            self._update_swatch(hex_color)

    def _on_accept(self) -> None:
        hex_color = self.color_edit.text().strip()
        if not _HEX_COLOR_RE.match(hex_color):
            hex_color = "#ff007b"

        fields = [f.strip() for f in self.fields_edit.text().split(",")]
        fields = [f for f in fields if f]
        if not fields:
            fields = ["jp-word", "Word Furigana"]

        cfg = self.mw.addonManager.getConfig(self.addon_name) or {}
        cfg["highlight_color"] = hex_color
        cfg["word_fields"] = fields
        self.mw.addonManager.writeConfig(self.addon_name, cfg)

        self.accept()
