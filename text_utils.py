"""Pure string/text helpers shared across this add-on's modules - no Anki
API, no file I/O, no module-level state. Split out of __init__.py so
collection_data.py and render.py don't need to import from the add-on's
entry-point module (Anki add-ons are just Python packages once __init__.py
does its own path setup, so plain sibling-module imports work normally)."""

from __future__ import annotations

import re

# Default word fields to read, in priority order - the first non-empty one
# wins. Both use the same "漢字[かな]" bracket notation. This is only the
# fallback if the user's config.json doesn't set "word_fields" (see
# __init__.py) - kept here too so text_utils has a sane default when used
# standalone (e.g. from tests) without going through config.
WORD_FIELDS = ["jp-word", "Word Furigana"]

BRACKET_RE = re.compile(r"\[[^\]]*\]")
TAG_RE = re.compile(r"<[^>]+>")

_KANJI_RANGES = ((0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0xF900, 0xFAFF))


def is_kanji(ch: str) -> bool:
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in _KANJI_RANGES)


def is_kana(ch: str) -> bool:
    return "ぁ" <= ch <= "ゟ" or "ァ" <= ch <= "ヿ"


def word_field(note_fields: dict, field_names: list[str] = WORD_FIELDS) -> str:
    for name in field_names:
        value = note_fields.get(name, "")
        if value and value.strip():
            # Kaishi's furigana fields carry <b> tags around the target
            # word; strip markup before parsing brackets.
            return TAG_RE.sub("", value)
    return ""


def extract_kanji(raw: str) -> list[str]:
    """Distinct kanji in `raw`, first-occurrence order - brackets stripped
    first so a reading like "側[がわ]" doesn't get its kana mistaken for
    part of the word, and duplicates (人々) collapse to one entry."""
    plain = re.sub(r"\s+", "", BRACKET_RE.sub("", raw))
    seen = []
    for ch in plain:
        if is_kanji(ch) and ch not in seen:
            seen.append(ch)
    return seen


def plain(raw: str) -> str:
    return re.sub(r"\s+", "", BRACKET_RE.sub("", raw))


def readings_from_brackets(raw: str) -> dict[str, set[str]]:
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
        while tail and not is_kana(base[tail - 1]):
            tail -= 1
        run = base[tail:]
        if len(run) == 1 and is_kanji(run):
            out.setdefault(run, set()).add(m.group(0)[1:-1])
        pos = m.end()
    return out


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


def katakana_to_hiragana(text: str) -> str:
    return "".join(
        chr(ord(ch) - _KATAKANA_OFFSET) if _KATAKANA_START <= ord(ch) <= _KATAKANA_END else ch
        for ch in text
    )


_warned: set[str] = set()


def warn_once(message: str) -> None:
    if message in _warned:
        return
    _warned.add(message)
    try:
        import sys
        print("[kanjidefs overlay] " + message, file=sys.stderr)
    except Exception:
        pass
