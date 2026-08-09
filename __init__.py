"""Dynamic Kanji Companion

Anki add-on: a bar along the bottom of the reviewer, shown only on the
answer side, listing each kanji in the current card's word with its English
meanings and on/kun readings, EACH READING a clickable link showing how many
collection notes use that exact (kanji, reading) pair - e.g. for 内側:

    内  inside, within, home        ナイ・うち(3)
    側  side, lean, oppose          ソク・がわ(12)

A reading with a collection count is a dotted-underline link (green if it's
the reading THIS card actually uses, the bar's default reading color
otherwise); a reading with no notes at all is plain non-clickable text -
same "don't link to an empty search" rule anki_addon_confused_kanji applies
to its neighbor kanji.

Under each kanji's meaning/reading row, a second line lists kanji commonly
CONFUSED (visually similar, easy to mix up) with it - e.g. under 未's row:

    Similar: 末, 味, 沫

Same rules as the readings above: a neighbor with >=1 note anywhere in the
collection is a green clickable link to those exact notes; one with zero
notes is plain non-clickable text (no dead-end search). Hovering ANY
similar-kanji neighbor (known or not) shows its own meaning/readings in a
small popup, via the same tooltip mechanism the on/kun readings could
reuse but don't currently need (only similar-kanji links carry a tooltip,
since the readings' own meanings are already the row they sit in).

This bar used to be THREE separate bars: this one (meanings/readings, no
collection data), anki_addon_kanji_readings_in_collection (per-reading note
counts as its own row of buttons), and anki_addon_confused_kanji (the
similar-kanji row, its own bar below). Folded into one here since all
three were already keyed on "the kanji in this card's word" and stacking
three near-identical bars was more separate moving parts than the
information warranted. Both superseded add-ons' own bars can be
uninstalled; their modules are left in place, unmodified, rather than
deleted, in case anything else ever wants their standalone
KanjiReadingIndex / SimilarKanjiIndex / _KnownKanjiIndex.

Unlike anki_addon_kanjidefs (which bakes the same kind of data into a
kanji-defs FIELD on jp-mining-kanjidefs notes only, at note-add time), this
reads straight from kanji_defs.sqlite3 and renders live on whatever card is
on screen, for whatever note type it happens to be. It works on cards that
were never touched by that add-on - existing jp-mining-v2 notes, Kaishi
1.5k notes, anything - and needs no field of its own.

Top-most bar in the shared stacking convention (data-stack-order="0", the
LOWEST order number - see render.STACK_JS for why lower means higher up).
Sits above anki_addon_confused_kanji's bar (order 2) whenever it's also
installed. These are independent add-ons that don't import each other; each
tags its own bar with data-stack-order and, at render time, sums the live
height of every OTHER present bar with a HIGHER order number (the ones
stacked below it) to compute its own offset - so any bar works standalone,
and stacking still lands correctly regardless of which subset is installed,
how many rows any of them wraps to, or how many more bars get added later
(a new bar just picks an unused, larger order number and naturally lands at
the bottom of the stack, with every existing add-on needing zero changes).

Where the per-reading counts come from
---------------------------------------
An in-memory index built once per session: kanji -> reading -> [note ids].
Two sources are merged per note, in this order:

  1. Bracket notation in the note's own word field ("側[がわ]"), which
     attributes a reading to a single kanji directly. Fields are tried in
     config.json's word_fields order (default: jp-word for this project's
     own note types, then Kaishi 1.5k's "Word Furigana", which uses the
     identical notation).

  2. db_cache/word_data.sqlite3's char_reading_index, for kanji that step 1
     couldn't isolate. This matters more than it sounds: decks routinely
     bracket a whole compound rather than each kanji ("勇気[ゆうき]"), and
     segmentation alone can't say which half is ゆう and which is き. The
     index knows (勇=ゆう, 気=き), and skipping this step lost roughly a
     quarter of all readings in a real collection.

Performance: measured on a 3,185-note collection, the whole index builds in
~0.56s (nearly all of it one sequential pass over char_reading_index -
querying it per-note instead takes 43 SECONDS, since that table's only
index is on (kanji, reading), not `word`), and built on a BACKGROUND thread
as soon as the profile opens so neither Anki's startup nor the first card
ever waits on it. Reviewing before it finishes just shows readings without
counts/links for a moment; it's then updated incrementally as notes are
added, and rebuilt if the profile changes.

File layout (split out of one 885-line file for readability):
  text_utils.py      - pure string helpers, no Anki API, no state
  collection_data.py - KanjiReadingIndex / SimilarKanjiIndex / KnownKanjiIndex
  render.py          - kanji_defs.sqlite3 lookups + all HTML/CSS/JS building
  __init__.py         - this file: path setup, singletons, Anki hooks

Install: copy this whole folder into Anki's addons21 folder, then restart
Anki.
"""

from __future__ import annotations

import sys
from pathlib import Path

from aqt import dialogs, gui_hooks, mw
from aqt.qt import QAction

# This add-on's own folder needs to be on sys.path, so the sibling modules
# below (text_utils, collection_data, render) can import each other by
# plain name the way __init__.py imports them here - Anki loads add-ons as
# packages, but doesn't put an add-on's own directory on sys.path itself.
sys.path.insert(0, str(Path(__file__).parent))

import re  # noqa: E402

from collection_data import (  # noqa: E402
    KanjiReadingIndex,
    KnownKanjiIndex,
    SimilarKanjiIndex,
    start_background_build,
)
from render import bar_html, lookup
from settings_dialog import SettingsDialog  # noqa: E402
from text_utils import WORD_FIELDS, extract_kanji, warn_once, word_field  # noqa: E402

# Bundled into this add-on's own data/ folder (see sync_kanjidefs_overlay_
# data.py, which lives outside the add-on and refreshes these from the
# jp-assets/jp-services checkout on this dev machine) rather than resolved
# through jp-services/paths.py - a published add-on can't assume anyone
# else's machine has C:\FILESC\cs3\... at all, so these need to travel
# with the add-on itself. word_data.sqlite3 is 336MB and this add-on only
# ever needs its char_reading_index table, so data/reading_index.sqlite3
# is a standalone ~27MB extraction of just that one table, not a copy of
# the whole database.
DATA_DIR = Path(__file__).parent / "data"
KANJI_DEFS_DB = str(DATA_DIR / "kanji_defs.sqlite3")
WORD_DATA_DB = str(DATA_DIR / "reading_index.sqlite3")
SIMILARS_PATH = str(DATA_DIR / "kanji.tgz_similars.ut8")

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _load_config() -> tuple[bool, str, list[str]]:
    """(visible, highlight_color, word_fields) from config.json, each
    falling back to its default (with a one-time warning) if the stored
    value is missing or malformed - a hand-edited config.json shouldn't be
    able to crash the add-on, just silently use sane defaults instead."""
    cfg = mw.addonManager.getConfig(__name__) or {}

    visible = bool(cfg.get("visible", True))

    highlight_color = cfg.get("highlight_color", "#4caf50")
    if not isinstance(highlight_color, str) or not _HEX_COLOR_RE.match(highlight_color):
        warn_once("config's highlight_color ({!r}) isn't a #rrggbb hex code - "
                  "using the default.".format(highlight_color))
        highlight_color = "#4caf50"

    word_fields = cfg.get("word_fields", WORD_FIELDS)
    if not isinstance(word_fields, list) or not all(isinstance(f, str) for f in word_fields) \
            or not word_fields:
        warn_once("config's word_fields ({!r}) isn't a non-empty list of strings - "
                  "using the default.".format(word_fields))
        word_fields = WORD_FIELDS

    return visible, highlight_color, word_fields


# All user-facing settings live in config.json (Anki's own add-on config
# mechanism, editable via the Tools menu's Settings dialog, Tools > Add-ons
# > Config, or by hand) so they survive across Anki restarts - plain
# module-level values would reset every time Anki starts. Read once at
# import time; on_js_message's toggle handler and on_settings_saved below
# are the only things that change any of these after startup, and both
# update the in-memory values and the config file together so they never
# drift within a session.
_visible, _highlight_color, _word_fields = _load_config()

_reading_index = KanjiReadingIndex(WORD_DATA_DB, _word_fields)
_similar_index = SimilarKanjiIndex(SIMILARS_PATH)
_known = KnownKanjiIndex(_word_fields)

# One background-build state dict per index - see collection_data.
# start_background_build's docstring for why each needs its own.
_reading_index_state = {"building": False}
_known_state = {"building": False}


def on_profile_did_open() -> None:
    """Kick both collection scans as soon as a collection is available.
    Also resets .built first: switching profiles means a different
    collection, and the old profile's note ids are meaningless in the new
    one."""
    _reading_index.built = False
    start_background_build(
        mw, lambda: _reading_index.build(mw.col), "reading index", _reading_index_state)
    _known.built = False
    start_background_build(
        mw, lambda: _known.build(mw.col), "known-kanji index", _known_state)


def on_add_cards_did_add_note(note) -> None:
    """Fold a newly-added note into both indexes so counts/links stay live
    without a full rebuild. Deliberately NOT note_will_be_added: that hook
    fires before the note is written and therefore before it has an id,
    and both indexes are keyed on note ids."""
    try:
        _reading_index.add_note_incremental(note)
    except Exception:
        pass
    try:
        _known.add_note_incremental(note)
    except Exception:
        pass


def on_card_will_show(text: str, card, kind: str) -> str:
    # Answer side only - the question side would give away the reading.
    #
    # Case-INSENSITIVE: Anki passes "reviewAnswer"/"reviewQuestion" (capital
    # A/Q), so a lowercase `"answer" in kind` silently never matches and the
    # bar never appears anywhere. Matching case-insensitively also covers
    # the other surfaces that reuse this hook with their own kind strings
    # (e.g. the card layout previewer's "previewAnswer").
    if "answer" not in kind.lower():
        return text
    try:
        raw = word_field(dict(card.note().items()), _word_fields)
        if not raw:
            return text
        entries = lookup(KANJI_DEFS_DB, extract_kanji(raw))
        if not _reading_index.built and not _reading_index_state["building"]:
            start_background_build(
                mw, lambda: _reading_index.build(mw.col), "reading index", _reading_index_state)
        current = _reading_index.current_readings(raw) if _reading_index.built else {}
        return text + bar_html(
            entries, current, _similar_index, _known, _reading_index, KANJI_DEFS_DB,
            visible=_visible, highlight_color=_highlight_color)
    except Exception as e:
        warn_once("failed to render ({}: {}) - showing card without it."
                  .format(type(e).__name__, e))
        return text


def on_js_message(handled, message: str, context):
    if message.startswith("kanjidefs-toggle:"):
        # The click already toggled the DOM itself (see render.bar_html's
        # onclick) - this only persists the new state for future renders,
        # so no reason to touch _visible before the config write succeeds.
        # Merges into the CURRENT config rather than writing {"visible":...}
        # alone - the config also holds highlight_color/word_fields, and a
        # bare overwrite here would silently wipe out anything the user set
        # for those via Tools > Add-ons > Config every time they click the
        # eye icon.
        global _visible
        _visible = message[len("kanjidefs-toggle:"):] == "1"
        cfg = mw.addonManager.getConfig(__name__) or {}
        cfg["visible"] = _visible
        mw.addonManager.writeConfig(__name__, cfg)
        return (True, None)
    if message.startswith("kanjidefs-reading:"):
        nids = message[len("kanjidefs-reading:"):]
        if nids:
            browser = dialogs.open("Browser", mw)
            browser.search_for("nid:" + nids)
            browser.activateWindow()
        return (True, None)
    if message.startswith("kanjidefs-confused:"):
        # Always "nid:1,2,3" - a similar-kanji neighbor with zero notes
        # isn't clickable at all (see render.similar_kanji_html), so this
        # never receives a bare kanji to fall back to a text search on.
        target = message[len("kanjidefs-confused:"):]
        if target:
            browser = dialogs.open("Browser", mw)
            browser.search_for(target)
            browser.activateWindow()
        return (True, None)
    return handled


def open_settings_dialog() -> None:
    """Tools menu entry point. Reloads _visible/_highlight_color/_word_fields
    and rebuilds the two collection indexes with the new field list after
    the dialog is accepted, so a changed word_fields list takes effect on
    the very next card shown rather than needing an Anki restart - the
    color/visibility globals are simple re-reads, but word_fields is baked
    into KanjiReadingIndex/KnownKanjiIndex at construction time (see
    collection_data.py), so those two singletons need fresh instances
    followed by a fresh background build to actually pick up the change."""
    global _visible, _highlight_color, _word_fields, _reading_index, _known

    dialog = SettingsDialog(mw, __name__, _highlight_color, _word_fields)
    if dialog.exec() != SettingsDialog.DialogCode.Accepted:
        return

    _visible, _highlight_color, _word_fields = _load_config()

    _reading_index = KanjiReadingIndex(WORD_DATA_DB, _word_fields)
    _known = KnownKanjiIndex(_word_fields)
    # Reset the "building" flags too, not just the indexes - these are the
    # same state dicts the OLD index objects' in-flight builds (if any)
    # still hold a closure over, and leaving "building" stuck True here
    # would make start_background_build silently refuse to launch a build
    # for the new objects.
    _reading_index_state["building"] = False
    _known_state["building"] = False
    start_background_build(
        mw, lambda: _reading_index.build(mw.col), "reading index", _reading_index_state)
    start_background_build(
        mw, lambda: _known.build(mw.col), "known-kanji index", _known_state)


def on_main_window_did_init() -> None:
    action = QAction("Dynamic Kanji Companion Settings…", mw)
    action.triggered.connect(open_settings_dialog)
    mw.form.menuTools.addAction(action)


gui_hooks.card_will_show.append(on_card_will_show)
gui_hooks.webview_did_receive_js_message.append(on_js_message)
gui_hooks.add_cards_did_add_note.append(on_add_cards_did_add_note)
gui_hooks.profile_did_open.append(on_profile_did_open)
gui_hooks.main_window_did_init.append(on_main_window_did_init)
