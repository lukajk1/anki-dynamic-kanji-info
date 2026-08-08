"""Collection-scanning indexes: KanjiReadingIndex (per-reading note counts,
folded in from anki_addon_kanji_readings_in_collection), SimilarKanjiIndex
(the confused-kanji data file, folded in from anki_addon_confused_kanji),
and _KnownKanjiIndex (which kanji appear anywhere in the collection, also
from anki_addon_confused_kanji). See __init__.py's module docstring for the
fuller "why these three were merged into one bar" explanation and the
per-reading-count algorithm/performance notes.

Each index is built on a BACKGROUND thread (see start_reading_index_build/
start_known_build) so neither Anki's startup nor the first card reviewed
ever waits on a collection scan - on_card_will_show in __init__.py checks
`.built` and shows readings/links without counts for a moment rather than
blocking if a build is still in flight.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from collections import defaultdict

from text_utils import extract_kanji, is_kanji, plain, readings_from_brackets, warn_once, word_field


class KanjiReadingIndex:
    """kanji -> reading -> set of note ids using that pair, collection-wide.

    An in-memory index built once per session. Two sources are merged per
    note, in this order:

      1. Bracket notation in the note's own word field ("側[がわ]"), which
         attributes a reading to a single kanji directly.

      2. db_cache/word_data.sqlite3's char_reading_index, for kanji that
         step 1 couldn't isolate. This matters more than it sounds: decks
         routinely bracket a whole compound rather than each kanji
         ("勇気[ゆうき]"), and segmentation alone can't say which half is
         ゆう and which is き. The index knows (勇=ゆう, 気=き), and
         skipping this step lost roughly a quarter of all readings in a
         real collection.

    Performance: measured on a 3,185-note collection, the whole index
    builds in ~0.56s (nearly all of it one sequential pass over
    char_reading_index - querying it per-note instead takes 43 SECONDS,
    since that table's only index is on (kanji, reading), not `word`).
    """

    def __init__(self, word_data_db: str):
        self.word_data_db = word_data_db
        self.index: dict[str, dict[str, set[int]]] = defaultdict(lambda: defaultdict(set))
        self._seen: set[int] = set()
        self._word_cache: dict[str, list[tuple[str, str]]] = {}
        self.built = False

    def _add_note(self, note_id: int, raw: str, word_map: dict) -> None:
        if not raw or note_id in self._seen:
            return
        self._seen.add(note_id)
        found = readings_from_brackets(raw)
        for kanji, reading in word_map.get(plain(raw), ()):
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
            raw = word_field(dict(note.items()))
            if raw:
                raws[note_id] = raw

        word_map = self._load_word_map({plain(r) for r in raws.values()})
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
        if not os.path.exists(self.word_data_db):
            return out
        conn = sqlite3.connect(
            "file:{}?mode=ro".format(self.word_data_db.replace("\\", "/")), uri=True)
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
        raw = word_field(dict(note.items()))
        if not raw:
            return
        self._add_note(note.id, raw, self._load_word_map({plain(raw)}))

    def current_readings(self, raw: str) -> dict[str, set[str]]:
        """kanji -> {reading} for the readings THIS card actually uses -
        bracket notation first, then char_reading_index for kanji the
        brackets couldn't isolate (so a compound-bracketed card like
        勇気[ゆうき] still correctly marks 気 as き)."""
        seen = [ch for ch in plain(raw) if is_kanji(ch)]
        current = readings_from_brackets(raw)
        missing = [k for k in seen if k not in current]
        if missing:
            for kanji, reading in self._word_readings(plain(raw)):
                if kanji in missing:
                    current.setdefault(kanji, set()).add(reading)
        return current

    def counts_for(self, kanji: str, reading: str) -> tuple[int, list[int]]:
        """(count, [note ids]) for one (kanji, reading) pair - 0 and [] if
        the index isn't built yet or the pair has never been seen."""
        note_ids = self.index.get(kanji, {}).get(reading, set())
        return len(note_ids), sorted(note_ids)


class SimilarKanjiIndex:
    """kanji -> [confused-with kanji, ...], parsed once from the flat
    key/neighbor/neighbor/... text file and cached for the process. Folded
    in from anki_addon_confused_kanji - see that add-on's own (unmodified)
    copy for the fuller data-format explanation. The file is 2,902 lines /
    ~150KB - trivial to hold in memory whole, so there's no reason to
    re-parse it per lookup or per card."""

    def __init__(self, similars_path: str):
        self.similars_path = similars_path
        self._map: dict[str, list[str]] | None = None

    def _ensure_loaded(self) -> dict[str, list[str]]:
        if self._map is not None:
            return self._map
        out: dict[str, list[str]] = {}
        try:
            with open(self.similars_path, encoding="utf-8") as f:
                for line in f:
                    parts = [p for p in line.strip().split("/") if p]
                    if len(parts) < 2:
                        continue
                    key, neighbors = parts[0], parts[1:]
                    out[key] = neighbors
        except OSError as e:
            warn_once("could not read {}: {}".format(self.similars_path, e))
        self._map = out
        return out

    def confused_with(self, kanji: str) -> list[str]:
        return self._ensure_loaded().get(kanji, [])


class KnownKanjiIndex:
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
            raw = word_field(dict(note.items()))
            if not raw:
                continue
            for kanji in extract_kanji(raw):
                index.setdefault(kanji, set()).add(note_id)
        self.index = index
        self.built = True

    def add_note_incremental(self, note) -> None:
        if not self.built:
            return
        raw = word_field(dict(note.items()))
        if not raw:
            return
        for kanji in extract_kanji(raw):
            self.index.setdefault(kanji, set()).add(note.id)

    def note_ids_for(self, kanji: str) -> set[int]:
        return self.index.get(kanji, set())


def start_background_build(mw, build_fn, label: str, state: dict) -> None:
    """Shared background-build launcher for both KanjiReadingIndex.build
    and KnownKanjiIndex.build - `state` is a single-key dict ({"building":
    bool}) used as a mutable flag since these are called from __init__.py
    for two independent indexes and each needs its own flag, not a shared
    module-level bool here."""
    if state["building"] or mw is None:
        return
    state["building"] = True

    def done(future):
        state["building"] = False
        try:
            future.result()
        except Exception as e:
            print("[kanjidefs overlay] {} build failed: {}: {}".format(
                label, type(e).__name__, e), file=sys.stderr)

    mw.taskman.run_in_background(build_fn, done)
