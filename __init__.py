"""Anki add-on: a bar along the bottom of the reviewer, shown only on the
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
LOWEST order number - see STACK_JS below for why lower means higher up).
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
     WORD_FIELDS order - jp-word for this project's own note types, then
     Kaishi 1.5k's "Word Furigana", which uses the identical notation.

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

Install: copy this folder into Anki's addons21 folder, then restart Anki.
"""

from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict

from aqt import dialogs, gui_hooks, mw

# jp-services/paths.py is the single place that knows where every project's
# folders actually live - see the module docstring for the longer
# explanation.
sys.path.insert(0, r"C:\FILESC\cs3\jp-services")
import paths  # noqa: E402

KANJI_DEFS_DB = str(paths.KANJI_DEFS / "kanji_defs.sqlite3")
WORD_DATA_DB = str(paths.DB_CACHE / "word_data.sqlite3")
SIMILARS_PATH = str(paths.SIMILAR_KANJI_DATA)

# Identical list, identical priority order, to what anki_addon_confused_
# kanji and anki_addon_kanji_readings_in_collection use - see module
# docstring for why these must not drift apart.
WORD_FIELDS = ["jp-word", "Word Furigana"]

# Same caps/separator as anki_addon_kanjidefs's field renderer, so a card
# that also has a baked kanji-defs field (jp-mining-kanjidefs notes) shows
# the same numbers live as it stored at add-time.
MAX_MEANINGS = 5
MAX_ON_READINGS = 3
MAX_KUN_READINGS = 3
READING_SEP = "・"

# Cap on similar-kanji links shown per kanji - same limits anki_addon_
# confused_kanji uses for its own (now-superseded) bar.
MAX_SIMILAR_PER_KANJI = 8
MAX_SIMILAR_LINKS = 24

THIS_BAR_ID = "kanjidefs-overlay-bar"
THIS_SPACER_ID = "kanjidefs-overlay-spacer"
THIS_TOOLTIP_ID = "kanjidefs-overlay-tooltip"
THIS_TOOLTIP_KANJI_ID = "kanjidefs-overlay-tooltip-kanji"
THIS_TOOLTIP_TEXT_ID = "kanjidefs-overlay-tooltip-text"
THIS_STACK_ORDER = 0  # top-most - see module docstring

# Gap between the bottom of the window and the reviewer's own answer
# buttons, that every bar in the stack applies independently (not just the
# bottom-most one) - see STACK_JS's own comment for why doing it this way
# doesn't double the gap the way including it in a bar's measured height
# would.
BOTTOM_OFFSET_PX = 20

# Identical copy of the snippet in anki_addon_kanji_readings_in_collection -
# see that module's own comment for the full rationale. Duplicated rather
# than imported since these add-ons are independent by design.
#
# LOWER order number = HIGHER up the screen. Each bar sums the height of
# every OTHER present bar with a HIGHER order number (i.e. everything
# stacked below it) - that sum, plus this bar's own BOTTOM_OFFSET_PX, is
# its `bottom` offset. Every bar adds the gap independently rather than
# only the bottom-most one: the gap is a fixed constant, not part of any
# bar's measured height, so there's nothing to double-count - a bar three
# deep gets (sum of two bars below it) + 20px, exactly like the bottom-most
# bar gets 0 + 20px.
STACK_JS = (
    'function stackBelow(self){'
    'var bars=document.querySelectorAll("[data-stack-order]"),total=0,'
    'mine=parseInt(self.getAttribute("data-stack-order"),10);'
    'for(var i=0;i<bars.length;i++){'
    'var el=bars[i];'
    'if(el===self)continue;'
    'var order=parseInt(el.getAttribute("data-stack-order"),10);'
    'if(order>mine)total+=el.getBoundingClientRect().height;'
    '}'
    'return total;'
    '}'
)

BRACKET_RE = re.compile(r"\[[^\]]*\]")
TAG_RE = re.compile(r"<[^>]+>")

_KANJI_RANGES = ((0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0xF900, 0xFAFF))


def _is_kanji(ch: str) -> bool:
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in _KANJI_RANGES)


def _word_field(note_fields: dict) -> str:
    for name in WORD_FIELDS:
        value = note_fields.get(name, "")
        if value and value.strip():
            return TAG_RE.sub("", value)
    return ""


def _extract_kanji(raw: str) -> list[str]:
    """Distinct kanji in `raw`, first-occurrence order - brackets stripped
    first so a reading like "側[がわ]" doesn't get its kana mistaken for
    part of the word, and duplicates (人々) collapse to one entry."""
    plain = re.sub(r"\s+", "", BRACKET_RE.sub("", raw))
    seen = []
    for ch in plain:
        if _is_kanji(ch) and ch not in seen:
            seen.append(ch)
    return seen


_warned: set[str] = set()


def _warn_once(message: str) -> None:
    if message in _warned:
        return
    _warned.add(message)
    try:
        print("[kanjidefs overlay] " + message, file=sys.stderr)
    except Exception:
        pass


def _is_kana(ch: str) -> bool:
    return "ぁ" <= ch <= "ゟ" or "ァ" <= ch <= "ヿ"


# Same fixed Unicode-block offset reading_pair_grades/pair_grade_reader.py's
# _hiragana_to_katakana uses, inverted. kanji_defs.sqlite3's on-readings are
# stored in KATAKANA (e.g. カン for 感), but KanjiReadingIndex is keyed on
# whatever case appears in bracket notation / char_reading_index, which is
# always HIRAGANA (かん) - without converting, every on-reading's count/
# current-card highlight silently fails to match (confirmed: counts_for
# ("感","カン") returns 0 even when the index has {"かん": {...}}, while
# counts_for("感","かん") returns the real count). Kun-readings are already
# hiragana in kanji_defs.sqlite3, so this is a no-op for those.
_HIRAGANA_START, _HIRAGANA_END = 0x3041, 0x3096
_KATAKANA_START, _KATAKANA_END = 0x30A1, 0x30F6
_KATAKANA_OFFSET = _KATAKANA_START - _HIRAGANA_START


def _katakana_to_hiragana(text: str) -> str:
    return "".join(
        chr(ord(ch) - _KATAKANA_OFFSET) if _KATAKANA_START <= ord(ch) <= _KATAKANA_END else ch
        for ch in text
    )


def _plain(raw: str) -> str:
    return re.sub(r"\s+", "", BRACKET_RE.sub("", raw))


def _readings_from_brackets(raw: str) -> dict[str, set[str]]:
    """kanji -> {reading} for every SINGLE kanji isolated by a bracket.

    A bracket annotates the trailing kanji run before it; only a run of
    exactly one kanji attributes unambiguously ("側[がわ]" yes,
    "勇気[ゆうき]" no - that one is left to char_reading_index).
    """
    out: dict[str, set[str]] = {}
    pos = 0
    for m in BRACKET_RE.finditer(raw):
        base = re.sub(r"\s+", "", raw[pos:m.start()])
        tail = len(base)
        while tail and not _is_kana(base[tail - 1]):
            tail -= 1
        run = base[tail:]
        if len(run) == 1 and _is_kanji(run):
            out.setdefault(run, set()).add(m.group(0)[1:-1])
        pos = m.end()
    return out


class KanjiReadingIndex:
    """kanji -> reading -> set of note ids using that pair, collection-wide.

    Folded in from anki_addon_kanji_readings_in_collection - see that
    add-on's own (unmodified) copy for the fuller performance rationale;
    this is the identical implementation, just living here so the counts it
    produces can be attached directly to the on/kun readings this bar
    already renders instead of duplicated in a second bar below it.
    """

    def __init__(self):
        self.index: dict[str, dict[str, set[int]]] = defaultdict(lambda: defaultdict(set))
        self._seen: set[int] = set()
        self._word_cache: dict[str, list[tuple[str, str]]] = {}
        self.built = False

    def _add_note(self, note_id: int, raw: str, word_map: dict) -> None:
        if not raw or note_id in self._seen:
            return
        self._seen.add(note_id)
        found = _readings_from_brackets(raw)
        for kanji, reading in word_map.get(_plain(raw), ()):
            if kanji not in found:
                found.setdefault(kanji, set()).add(reading)
        for kanji, readings in found.items():
            for reading in readings:
                self.index[kanji][reading].add(note_id)

    def build(self, col) -> None:
        """Full rebuild from the collection. Excludes suspended cards
        (Anki's own "-is:suspended" search) - a suspended card is
        deliberately out of rotation, and its reading showing up in a count
        here would read as still-active data."""
        self.index = defaultdict(lambda: defaultdict(set))
        self._seen = set()

        raws: dict[int, str] = {}
        for note_id in col.find_notes("-is:suspended"):
            note = col.get_note(note_id)
            raw = _word_field(dict(note.items()))
            if raw:
                raws[note_id] = raw

        word_map = self._load_word_map({_plain(r) for r in raws.values()})
        for note_id, raw in raws.items():
            self._add_note(note_id, raw, word_map)

        self._word_cache = dict(word_map)
        self.built = True

    def _load_word_map(self, wanted: set[str]) -> dict[str, list[tuple[str, str]]]:
        """word -> [(kanji, reading)] from char_reading_index, via ONE
        sequential pass - that table has no index on `word`, so querying it
        per word is ~46ms each; reading it once into a dict is what makes
        this viable at all."""
        out: dict[str, list[tuple[str, str]]] = defaultdict(list)
        if not os.path.exists(WORD_DATA_DB):
            return out
        conn = sqlite3.connect(
            "file:{}?mode=ro".format(WORD_DATA_DB.replace("\\", "/")), uri=True)
        try:
            for word, kanji, reading in conn.execute(
                    "SELECT word, kanji, reading FROM char_reading_index"):
                if word in wanted:
                    out[word].append((kanji, reading))
        except sqlite3.Error:
            return out
        finally:
            conn.close()
        return out

    def _word_readings(self, word: str) -> list[tuple[str, str]]:
        if word in self._word_cache:
            return self._word_cache[word]
        found = self._load_word_map({word}).get(word, [])
        self._word_cache[word] = found
        return found

    def add_note_incremental(self, note) -> None:
        if not self.built:
            return
        raw = _word_field(dict(note.items()))
        if not raw:
            return
        self._add_note(note.id, raw, self._load_word_map({_plain(raw)}))

    def current_readings(self, raw: str) -> dict[str, set[str]]:
        """kanji -> {reading} for the readings THIS card actually uses -
        bracket notation first, then char_reading_index for kanji the
        brackets couldn't isolate (so a compound-bracketed card like
        勇気[ゆうき] still correctly marks 気 as き)."""
        seen = [ch for ch in _plain(raw) if _is_kanji(ch)]
        current = _readings_from_brackets(raw)
        missing = [k for k in seen if k not in current]
        if missing:
            for kanji, reading in self._word_readings(_plain(raw)):
                if kanji in missing:
                    current.setdefault(kanji, set()).add(reading)
        return current

    def counts_for(self, kanji: str, reading: str) -> tuple[int, list[int]]:
        """(count, [note ids]) for one (kanji, reading) pair - 0 and [] if
        the index isn't built yet or the pair has never been seen."""
        note_ids = self.index.get(kanji, {}).get(reading, set())
        return len(note_ids), sorted(note_ids)


_reading_index = KanjiReadingIndex()

# Set while a background build is in flight, so a second trigger doesn't
# start a duplicate scan.
_reading_index_building = False


def _start_reading_index_build() -> None:
    global _reading_index_building
    if _reading_index_building or mw is None:
        return
    _reading_index_building = True

    def task():
        _reading_index.build(mw.col)

    def done(future):
        global _reading_index_building
        _reading_index_building = False
        try:
            future.result()
        except Exception as e:
            print("[kanjidefs overlay] reading index build failed: {}: {}".format(
                type(e).__name__, e), file=sys.stderr)

    mw.taskman.run_in_background(task, done)


class SimilarKanjiIndex:
    """kanji -> [confused-with kanji, ...], parsed once from the flat
    key/neighbor/neighbor/... text file and cached for the process. Folded
    in from anki_addon_confused_kanji - see that add-on's own (unmodified)
    copy for the fuller data-format explanation. The file is 2,902 lines /
    ~150KB - trivial to hold in memory whole, so there's no reason to
    re-parse it per lookup or per card."""

    def __init__(self):
        self._map: dict[str, list[str]] | None = None

    def _ensure_loaded(self) -> dict[str, list[str]]:
        if self._map is not None:
            return self._map
        out: dict[str, list[str]] = {}
        try:
            with open(SIMILARS_PATH, encoding="utf-8") as f:
                for line in f:
                    parts = [p for p in line.strip().split("/") if p]
                    if len(parts) < 2:
                        continue
                    key, neighbors = parts[0], parts[1:]
                    out[key] = neighbors
        except OSError as e:
            _warn_once("could not read {}: {}".format(SIMILARS_PATH, e))
        self._map = out
        return out

    def confused_with(self, kanji: str) -> list[str]:
        return self._ensure_loaded().get(kanji, [])


_similar_index = SimilarKanjiIndex()


class _KnownKanjiIndex:
    """kanji -> set of note ids using that character anywhere in the
    collection - folded in from anki_addon_confused_kanji, a simpler
    version of KanjiReadingIndex above (no reading resolution needed here,
    just "does this character appear anywhere"), used to decide which
    similar-kanji neighbors get a clickable/green link vs. plain text.
    Same "-is:suspended" exclusion and background-thread build as
    KanjiReadingIndex, for the same reasons."""

    def __init__(self):
        self.index: dict[str, set[int]] = {}
        self.built = False

    def build(self, col) -> None:
        index: dict[str, set[int]] = {}
        for note_id in col.find_notes("-is:suspended"):
            note = col.get_note(note_id)
            raw = _word_field(dict(note.items()))
            if not raw:
                continue
            for kanji in _extract_kanji(raw):
                index.setdefault(kanji, set()).add(note_id)
        self.index = index
        self.built = True

    def add_note_incremental(self, note) -> None:
        if not self.built:
            return
        raw = _word_field(dict(note.items()))
        if not raw:
            return
        for kanji in _extract_kanji(raw):
            self.index.setdefault(kanji, set()).add(note.id)

    def note_ids_for(self, kanji: str) -> set[int]:
        return self.index.get(kanji, set())


_known = _KnownKanjiIndex()

# Set while a background build is in flight, so a second trigger doesn't
# start a duplicate scan - same guard _reading_index's build uses.
_known_building = False


def _start_known_build() -> None:
    global _known_building
    if _known_building or mw is None:
        return
    _known_building = True

    def task():
        _known.build(mw.col)

    def done(future):
        global _known_building
        _known_building = False
        try:
            future.result()
        except Exception as e:
            print("[kanjidefs overlay] known-kanji index build failed: {}: {}".format(
                type(e).__name__, e), file=sys.stderr)

    mw.taskman.run_in_background(task, done)


def on_profile_did_open() -> None:
    _reading_index.built = False
    _start_reading_index_build()
    _known.built = False
    _start_known_build()


def on_add_cards_did_add_note(note) -> None:
    try:
        _reading_index.add_note_incremental(note)
    except Exception:
        pass
    try:
        _known.add_note_incremental(note)
    except Exception:
        pass


def _lookup(kanji_list: list[str]) -> list[dict]:
    """[{"kanji":, "meanings":, "on":, "kun":}] for each kanji that has an
    entry, skipping any that don't (same rule kanji_defs/reader.py and
    anki_addon_kanjidefs use - no blank row for a kanji with nothing to
    say). One connection per call, opened and closed immediately: this
    fires on every answer shown, but a sqlite point-lookup on a 640KB file
    is microseconds, and holding the connection open for the session would
    block kanji_defs/build_kanji_defs.py from ever replacing this file
    (Windows won't unlink/replace an open file) for as long as Anki runs -
    the same tradeoff anki_addon_kanjidefs.add_note makes.
    """
    if not kanji_list or not os.path.exists(KANJI_DEFS_DB):
        return []
    conn = sqlite3.connect(
        "file:{}?mode=ro".format(KANJI_DEFS_DB.replace("\\", "/")), uri=True)
    try:
        out = []
        for ch in kanji_list:
            row = conn.execute(
                "SELECT meanings, on_readings, kun_readings FROM kanji_defs "
                "WHERE kanji = ?", (ch,)).fetchone()
            if row is None:
                continue
            out.append({
                "kanji": ch,
                "meanings": json.loads(row[0])[:MAX_MEANINGS],
                "on": json.loads(row[1])[:MAX_ON_READINGS],
                "kun": json.loads(row[2])[:MAX_KUN_READINGS],
            })
        return out
    finally:
        conn.close()


def _tooltip_text(entry: dict | None) -> str:
    """Plain-text hover-tooltip content for one similar-kanji neighbor:
    meanings, then on/kun readings on a second line - "" (no tooltip shown)
    if kanji_defs.sqlite3 has no entry for it. Folded in from
    anki_addon_confused_kanji, same format."""
    if not entry:
        return ""
    meanings = ", ".join(entry["meanings"])
    on = READING_SEP.join(entry["on"])
    kun = READING_SEP.join(entry["kun"])
    reading = READING_SEP.join(r for r in (on, kun) if r)
    return meanings + ("\n" + reading if reading else "")


def _similar_kanji_html(word_kanji: list[str]) -> str:
    """"Similar: 末(2), 味(5), 沫" per source kanji that has any confusable
    neighbors - folded in from anki_addon_confused_kanji, same rules:

    - every neighbor is shown, none filtered out
    - a neighbor with >=1 note anywhere in the collection is a green,
      clickable, dotted-underline link to those exact notes (nid:1,2,3),
      labeled with the note count in parens - same "(count)" convention
      _reading_span uses for on/kun readings; one with zero notes is plain
      non-clickable text with no count (no dead-end search)
    - hovering any neighbor shows its meaning/readings via the tooltip this
      bar already renders (see _tooltip_text and the shared JS in
      _bar_html's returned HTML)
    """
    all_neighbors: list[str] = []
    for kanji in word_kanji:
        all_neighbors.extend(_similar_index.confused_with(kanji)[:MAX_SIMILAR_PER_KANJI])
    defs_by_kanji = {e["kanji"]: e for e in _lookup(list(dict.fromkeys(all_neighbors)))}

    rows = []
    link_count = 0
    for kanji in word_kanji:
        neighbors = _similar_index.confused_with(kanji)[:MAX_SIMILAR_PER_KANJI]
        if not neighbors:
            continue
        links = []
        for neighbor in neighbors:
            if link_count >= MAX_SIMILAR_LINKS:
                break
            tooltip = _tooltip_text(defs_by_kanji.get(neighbor))
            # data-kanji carries the BARE kanji for the tooltip's glyph
            # column - el.textContent would include the "(count)" suffix
            # the link itself displays, which isn't what the tooltip's
            # large-glyph side should show.
            tooltip_attr = (' data-tooltip="{}" data-kanji="{}"'.format(
                                html.escape(tooltip), html.escape(neighbor))
                             if tooltip else "")
            note_ids = _known.note_ids_for(neighbor) if _known.built else set()
            if note_ids:
                target = "nid:" + ",".join(str(i) for i in sorted(note_ids))
                links.append(
                    '<span onclick="pycmd(\'kanjidefs-confused:{}\'); return false;"{} '
                    'style="cursor:pointer; text-decoration:underline dotted; '
                    'text-underline-offset:2px; color:#4caf50;">{}({})</span>'.format(
                        html.escape(target), tooltip_attr, html.escape(neighbor), len(note_ids))
                )
            else:
                links.append(
                    '<span{} style="color:inherit;">{}</span>'.format(
                        tooltip_attr, html.escape(neighbor))
                )
            link_count += 1
        if not links:
            continue
        rows.append(
            '<div style="padding:1px 0; font-size:18px; opacity:0.85;">'
            '<span style="opacity:0.65;">Similar:</span> '
            '{}'
            '</div>'.format(", ".join(links))
        )
        if link_count >= MAX_SIMILAR_LINKS:
            break
    return "".join(rows)


def _reading_span(kanji: str, reading: str, current: set[str]) -> str:
    """One on/kun reading as a span, with a collection-count link if the
    (kanji, reading) pair has ever been seen, plain non-clickable text if
    not - same "don't link to an empty search" rule anki_addon_confused_
    kanji applies to its neighbor kanji. Current-card readings are green;
    unindexed/index-still-building shows the reading with no count/link
    rather than blocking the bar.

    `reading` is displayed exactly as kanji_defs.sqlite3 stores it
    (katakana for on-readings, hiragana for kun) - see the module-level
    _katakana_to_hiragana comment for why the LOOKUP key must be converted
    to hiragana first, even though the on-screen label stays katakana.
    """
    hira = _katakana_to_hiragana(reading)
    count, note_ids = ((0, []) if not _reading_index.built
                        else _reading_index.counts_for(kanji, hira))
    is_current = hira in current
    if count:
        nids = ",".join(str(i) for i in note_ids)
        color = "#4caf50" if is_current else "inherit"
        weight = "600" if is_current else "inherit"
        return (
            '<span onclick="pycmd(\'kanjidefs-reading:{}\'); return false;" '
            'style="cursor:pointer; text-decoration:underline dotted; '
            'text-underline-offset:2px; color:{}; font-weight:{};">{}({})</span>'.format(
                nids, color, weight, html.escape(reading), count)
        )
    color = "#4caf50" if is_current else "inherit"
    weight = "600" if is_current else "inherit"
    return '<span style="color:{}; font-weight:{};">{}</span>'.format(
        color, weight, html.escape(reading))


def _bar_html(entries: list[dict], current: dict[str, set[str]]) -> str:
    if not entries:
        return ""

    # One row per kanji, each a left-anchored flex line rather than a
    # centered inline span - a fixed-width kanji column (2em) then
    # meanings then readings, so every row's meanings start at the same x
    # regardless of how wide the preceding kanji glyph renders, and a long
    # meanings list wraps within its own row instead of being centered as
    # one unbreakable chunk (which is what clipped 公's meanings off the
    # edge in practice - centering has no left edge to wrap back to).
    similar_html_by_kanji = {}
    for e in entries:
        row_html = _similar_kanji_html([e["kanji"]])
        if row_html:
            similar_html_by_kanji[e["kanji"]] = row_html

    rows = []
    for i, e in enumerate(entries):
        meanings = ", ".join(e["meanings"])
        kanji_current = current.get(e["kanji"], set())
        on_spans = [_reading_span(e["kanji"], r, kanji_current) for r in e["on"]]
        kun_spans = [_reading_span(e["kanji"], r, kanji_current) for r in e["kun"]]
        reading_html = READING_SEP.join(on_spans + kun_spans)
        # "X similar: ..." on its own line directly under this kanji's
        # meaning/reading row (folded in from anki_addon_confused_kanji) -
        # "" (no extra line) if this kanji has no confusable neighbors, so
        # a kanji_defs entry with nothing to say about similars doesn't
        # leave a blank gap.
        similar_row = similar_html_by_kanji.get(e["kanji"], "")
        # A subtle top border between kanji entries (not on the first one,
        # so there's no stray line flush against the bar's own top edge) -
        # a thin hairline rather than a real <hr>, which would need its own
        # margin/color handling to read as "subtle" rather than a hard rule.
        border = ("border-top:1px solid rgba(255,255,255,0.12); margin-top:4px; "
                  "padding-top:4px; " if i > 0 else "")
        # One grid per kanji entry: 3 columns (glyph, meanings, readings) x
        # 2 rows (meanings/readings line, then the Similar line). The glyph
        # spans BOTH rows (grid-row:1/3) so it's vertically centered across
        # the entry's full height and the Similar line's own column starts
        # at column 2 - the same x position meanings starts at - rather
        # than being a separate full-width block indented to fake that
        # alignment. grid-template-columns uses the same 2em/1fr/1fr split
        # as before: fixed glyph column, meanings and readings sharing the
        # remaining space evenly regardless of either one's own content
        # length.
        similar_cell = (
            '<div style="grid-column:2 / 4; grid-row:2; text-align:left;">{}</div>'.format(similar_row)
            if similar_row else ""
        )
        rows.append(
            # Explicit light text color, not inherited - see the bar
            # background comment below: an opaque bar can no longer borrow
            # contrast from whatever page color shows through it. Readings
            # column pins its own base color too (each _reading_span sets
            # "inherit" for the non-current case, which resolves against
            # THIS span's color, not the row's #dddddd, unless this span
            # itself pins the same light color).
            '<div style="padding:1px 0; {border}">'
            '<div style="display:grid; grid-template-columns:2.4em 1fr 1fr; '
            'column-gap:0.6em; text-align:left; color:#dddddd;">'
            '<span style="grid-column:1; grid-row:1 / 3; font-size:46px; '
            'color:#ffffff; align-self:center; margin-right:0.6em;">{}</span>'
            '<span style="grid-column:2; grid-row:1; font-size:18px; '
            'align-self:center;">{}</span>'
            '<span style="grid-column:3; grid-row:1; font-size:16px; '
            'color:#dddddd; align-self:start;">{}</span>'
            '{}'
            '</div>'
            '</div>'.format(
                html.escape(e["kanji"]), html.escape(meanings), reading_html, similar_cell,
                border=border)
        )

    return (
        # `bottom` is set to 0 here and corrected by the script below once
        # every other stacking bar's real height is known - not computed
        # inline, because at the point this HTML is generated (Python,
        # before the page has laid out) no other add-on's bar height is
        # knowable. Starting at 0 and correcting via rAF means a one-frame
        # flash of overlap is possible on a slow render; accepted as
        # simpler than coordinating independent add-ons' Python any other
        # way.
        # `left:0; right:0` on the OUTER wrapper keeps the full-width
        # background/backdrop-blur band (matching the other bars' look);
        # an INNER div then caps the actual row width and centers as a
        # block, so long meanings wrap within a readable column instead of
        # stretching edge-to-edge, while each row's own text stays left-
        # aligned inside that column. Each row above is its own full-width
        # flex line, not another flex/wrap container - centering used to
        # apply to the whole entry as one unbreakable inline chunk, which
        # is what clipped 公 rather than wrapping it.
        # Opaque, matching the other stacking bars (same #2b2b2b) - no
        # backdrop-filter, since blur only makes sense over something
        # translucent, and a consistent fixed color across every stacked
        # bar reads as one system rather than several.
        #
        # data-stack-order="0": top-most (lowest order number) - see
        # module docstring and STACK_JS.
        '<div id="{bar_id}" data-stack-order="{order}" style="position:fixed; '
        'left:0; right:0; bottom:0; '
        'z-index:9998; padding:5px 6px; '
        'background:#2b2b2b; '
        'border-top:1px solid rgba(127,127,127,0.3);">'
        '<div style="max-width:520px; margin:0 auto;">{rows}</div>'
        '</div>'
        '<div id="{spacer_id}"></div>'
        # Shared hover tooltip for similar-kanji links (folded in from
        # anki_addon_confused_kanji) - one element, positioned above
        # whichever span is hovered, filled from that span's data-tooltip
        # attribute (already in the page from _similar_kanji_html/_lookup,
        # no per-hover query) plus its own text content (the hovered kanji
        # itself). Flex row: the kanji glyph on the left, sized/colored
        # like the bar's own big kanji column, then the meanings/readings
        # text - align-items:stretch (the flex default) makes the glyph
        # column's height track the text column's, so the glyph area grows
        # vertically with the tooltip rather than needing a fixed height;
        # the glyph's own line-height:1 plus the row's align-items:center
        # keeps the character centered in whatever height that ends up
        # being, with room to breathe via the row gap. white-space:pre-line
        # on the text column renders _tooltip_text's embedded "\n" as a
        # real line break without HTML in the attribute.
        '<div id="{tooltip_id}" style="position:fixed; display:none; '
        'z-index:10000; max-width:300px; padding:10px 14px; '
        'background:#2596be; color:#eeeeee; '
        'border-radius:6px; border:1px solid rgba(255,255,255,0.18); '
        'box-shadow:0 2px 10px rgba(0,0,0,0.4); pointer-events:none; '
        'align-items:center; gap:12px;">'
        '<span id="{tooltip_kanji_id}" style="font-size:34px; line-height:1; '
        'color:#ffffff; flex:0 0 auto;"></span>'
        '<span id="{tooltip_text_id}" style="font-size:16px; line-height:1.4; '
        'white-space:pre-line; flex:1 1 auto;"></span>'
        '</div>'
        '<script>(function(){{'
        'var b=document.getElementById("{bar_id}"),'
        's=document.getElementById("{spacer_id}"),'
        't=document.getElementById("{tooltip_id}"),'
        'tk=document.getElementById("{tooltip_kanji_id}"),'
        'tx=document.getElementById("{tooltip_text_id}");'
        'if(!b||!s)return;'
        '{stack_js}'
        'function fit(){{'
        'var stack=stackBelow(b)+{offset};'
        'b.style.bottom=stack+"px";'
        's.style.height=(b.offsetHeight+stack)+"px";'
        '}}'
        'requestAnimationFrame(fit);'
        'window.addEventListener("resize",fit);'
        'if(window.ResizeObserver){{'
        'new ResizeObserver(fit).observe(b);'
        'document.querySelectorAll("[data-stack-order]").forEach(function(el){{'
        'if(el!==b)new ResizeObserver(fit).observe(el);'
        '}});'
        '}}'
        # Event delegation on the bar itself (one listener, not one per
        # span) - mouseover/mouseout bubble, so this catches hover on any
        # similar-kanji span.
        'if(t&&tk&&tx){{'
        'b.addEventListener("mouseover",function(e){{'
        'var el=e.target.closest?e.target.closest("[data-tooltip]"):null;'
        'if(!el)return;'
        'tk.textContent=el.getAttribute("data-kanji")||el.textContent;'
        'tx.textContent=el.getAttribute("data-tooltip");'
        't.style.display="flex";'
        'var r=el.getBoundingClientRect();'
        't.style.left=Math.max(4,r.left)+"px";'
        't.style.bottom=(window.innerHeight-r.top+6)+"px";'
        '}});'
        'b.addEventListener("mouseout",function(e){{'
        'var el=e.target.closest?e.target.closest("[data-tooltip]"):null;'
        'if(!el)return;'
        't.style.display="none";'
        '}});'
        '}}'
        '}})();</script>'.format(
            bar_id=THIS_BAR_ID, spacer_id=THIS_SPACER_ID, tooltip_id=THIS_TOOLTIP_ID,
            tooltip_kanji_id=THIS_TOOLTIP_KANJI_ID, tooltip_text_id=THIS_TOOLTIP_TEXT_ID,
            order=THIS_STACK_ORDER, offset=BOTTOM_OFFSET_PX, rows="".join(rows),
            stack_js=STACK_JS)
    )


def on_card_will_show(text: str, card, kind: str) -> str:
    if "answer" not in kind.lower():
        return text
    try:
        raw = _word_field(dict(card.note().items()))
        if not raw:
            return text
        entries = _lookup(_extract_kanji(raw))
        if not _reading_index.built and not _reading_index_building:
            _start_reading_index_build()
        current = _reading_index.current_readings(raw) if _reading_index.built else {}
        return text + _bar_html(entries, current)
    except Exception as e:
        _warn_once("failed to render ({}: {}) - showing card without it."
                   .format(type(e).__name__, e))
        return text


def on_js_message(handled, message: str, context):
    if message.startswith("kanjidefs-reading:"):
        nids = message[len("kanjidefs-reading:"):]
        if nids:
            browser = dialogs.open("Browser", mw)
            browser.search_for("nid:" + nids)
            browser.activateWindow()
        return (True, None)
    if message.startswith("kanjidefs-confused:"):
        # Always "nid:1,2,3" - a similar-kanji neighbor with zero notes
        # isn't clickable at all (see _similar_kanji_html), so this never
        # receives a bare kanji to fall back to a text search on.
        target = message[len("kanjidefs-confused:"):]
        if target:
            browser = dialogs.open("Browser", mw)
            browser.search_for(target)
            browser.activateWindow()
        return (True, None)
    return handled


gui_hooks.card_will_show.append(on_card_will_show)
gui_hooks.webview_did_receive_js_message.append(on_js_message)
gui_hooks.add_cards_did_add_note.append(on_add_cards_did_add_note)
gui_hooks.profile_did_open.append(on_profile_did_open)
