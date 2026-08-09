"""Builds the bar's HTML/CSS/JS: kanji_defs.sqlite3 lookups (_lookup),
hover-tooltip content (similar_tooltip_html), per-reading link rendering
(reading_span), the similar-kanji line (similar_kanji_html), and the full
bar assembly (bar_html). See __init__.py's module docstring for the
overall feature description and the visual layout rationale.
"""

from __future__ import annotations

import html
import json
import os
import re
import sqlite3

import theme
from text_utils import BRACKET_RE, is_kana, katakana_to_hiragana

# A comma+space, not "・" - a real space IS whitespace, so it's already a
# valid line-break point on its own (no zero-width-space trick needed the
# way "・" required). Each reading_span itself is nowrap/inline-block so it
# can't split mid-unit; the browser wraps at this space when the column
# overflows, landing the break right after the comma.
READING_SEP = ", "

# Same caps/separator as anki_addon_kanjidefs's field renderer, so a card
# that also has a baked kanji-defs field (jp-mining-kanjidefs notes) shows
# the same numbers live as it stored at add-time.
MAX_MEANINGS = 5
MAX_ON_READINGS = 3
MAX_KUN_READINGS = 3

# Cap on similar-kanji links shown per kanji - same limits anki_addon_
# confused_kanji uses for its own (now-superseded) bar.
MAX_SIMILAR_PER_KANJI = 8
MAX_SIMILAR_LINKS = 24

# Cap on words listed in a reading's hover tooltip before it collapses to
# "+N more" - a common reading can match hundreds of notes, and a tooltip
# taller than the window isn't readable. Clicking still opens all of them.
MAX_TOOLTIP_WORDS = 8

# Every font size inside a hover tooltip, in px, kept together so a resize
# is one edit rather than a hunt through the markup. These do NOT cover the
# bar itself - only what the tooltips render.
TOOLTIP_TEXT_PX = 15       # meanings/readings, and the "+N more" line
TOOLTIP_WORD_PX = 24       # the ruby word list
TOOLTIP_GLYPH_PX = 32      # the big kanji beside a neighbor's meanings

THIS_BAR_ID = "kanjidefs-overlay-bar"
THIS_SPACER_ID = "kanjidefs-overlay-spacer"
THIS_TOOLTIP_ID = "kanjidefs-overlay-tooltip"
THIS_TOOLTIP_TEXT_ID = "kanjidefs-overlay-tooltip-text"
THIS_TOGGLE_ID = "kanjidefs-overlay-toggle"
THIS_STACK_ORDER = 0  # top-most - see __init__.py's module docstring

# Open-eye / eye-with-slash icons for the show/hide toggle button, as
# inline SVG rather than a Unicode glyph (an eye emoji renders wildly
# differently across platforms/fonts, sometimes not as an eye at all;
# these are drawn once and never change appearance). stroke="currentColor"
# so each just inherits the button's own text color rather than needing
# its own color to track theme changes.
_EYE_SVG = (
    '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round">'
    '<path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/>'
    '<circle cx="12" cy="12" r="3"/>'
    '</svg>'
)
_EYE_OFF_SVG = (
    '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round">'
    '<path d="M17.94 17.94A10.94 10.94 0 0 1 12 19c-7 0-11-7-11-7a21.3 21.3 0 0 1 5.06-5.94"/>'
    '<path d="M9.9 4.24A9.12 9.12 0 0 1 12 5c7 0 11 7 11 7a21.3 21.3 0 0 1-2.44 3.44"/>'
    '<path d="M14.12 14.12a3 3 0 1 1-4.24-4.24"/>'
    '<path d="M1 1l22 22"/>'
    '</svg>'
)

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


def lookup(kanji_defs_db: str, kanji_list: list[str]) -> list[dict]:
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
    if not kanji_list or not os.path.exists(kanji_defs_db):
        return []
    conn = sqlite3.connect(
        "file:{}?mode=ro".format(kanji_defs_db.replace("\\", "/")), uri=True)
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


def similar_tooltip_html(entry: dict | None, words: list[str], kanji: str = "",
                          glyph_color: str = "#ffffff",
                          cap: int = MAX_TOOLTIP_WORDS) -> str:
    """Hover-tooltip content for one similar-kanji neighbor: the collection
    words containing that kanji (same ruby-furigana list the readings
    tooltip shows, same cap) spanning the tooltip's full width, a rule,
    then a row of the big kanji glyph beside its meanings and on/kun
    readings.

    The word list is deliberately NOT in a column beside the glyph - it's a
    full-width block above the glyph row, so words start flush at the
    tooltip's left edge instead of being indented past a 34px character
    they aren't related to. Only the meanings/readings the glyph actually
    labels sit next to it.

    The glyph is emitted HERE rather than set separately by the hover JS
    from data-kanji: it belongs inside the lower row, which only this
    function knows the shape of, and one source for the whole tooltip beats
    two that have to agree.

    HTML rather than plain text, because the word list needs <ruby> markup
    - see reading_span's own comment on why that means data-tooltip-html
    instead of data-tooltip. (This replaced a plain-text tooltip_text()
    that returned just the meanings/readings half.)

    Either half can be missing: a neighbor with no notes yet still shows
    its meanings, and a neighbor absent from kanji_defs.sqlite3 still shows
    its words. The rule is only drawn when both halves are actually there,
    so a tooltip never opens or closes with a stray line.
    """
    words_part = reading_tooltip_html(words, cap)

    defs_lines = []
    if entry:
        meanings = ", ".join(entry["meanings"])
        on = READING_SEP.join(entry["on"])
        kun = READING_SEP.join(entry["kun"])
        reading = READING_SEP.join(r for r in (on, kun) if r)
        if meanings:
            defs_lines.append(
                '<div style="text-align:left;">{}</div>'.format(html.escape(meanings)))
        if reading:
            defs_lines.append(
                '<div style="text-align:left;">{}</div>'.format(html.escape(reading)))

    # The glyph row: glyph on the left, meanings/readings stacked to its
    # right, centered against that block as a whole. Bottom-aligning (what
    # this was) only looks right while the meanings fit one line - as soon
    # as they wrap, the glyph sinks to the last reading line instead of
    # sitting beside the block it labels.
    #
    # Gated on defs_lines, NOT on `kanji`: the glyph exists to label the
    # meanings/readings beside it, so a neighbor absent from
    # kanji_defs.sqlite3 gets no row at all rather than a rule followed by
    # a lone character with an empty column next to it.
    defs_part = ""
    if defs_lines:
        glyph = (
            '<span style="font-size:{}px; line-height:1; color:{}; '
            'flex:0 0 auto;">{}</span>'.format(
                TOOLTIP_GLYPH_PX, glyph_color, html.escape(kanji))
            if kanji else ""
        )
        defs_part = (
            '<div style="display:flex; align-items:center; gap:12px;">'
            '{}'
            '<span style="flex:1 1 auto; min-width:0; text-align:left;">{}</span>'
            '</div>'.format(glyph, "".join(defs_lines))
        )

    if not words_part and not defs_part:
        return ""
    if words_part and defs_part:
        # currentColor so the rule tracks the tooltip's own text color in
        # either theme, rather than needing its own light/dark token.
        rule = ('<hr style="border:none; border-top:1px solid currentColor; '
                'opacity:0.25; margin:8px 0;">')
        return words_part + rule + defs_part
    return words_part or defs_part


def ruby_html(raw: str) -> str:
    """One word's bracket notation ("感心[かんしん]", "お 前[まえ]") as real
    <ruby> markup, so the tooltip shows furigana ABOVE the kanji the way a
    card does, rather than in brackets beside it.

    A bracket annotates the kanji run immediately before it - the same rule
    text_utils.readings_from_brackets parses for its own purposes, applied
    here to the whole run at once (that function deliberately only accepts
    single-kanji runs, since it has to attribute a reading to ONE kanji;
    for display there's no such constraint, so 感心[かんしん] renders as one
    ruby pair rather than being skipped).
    """
    out = []
    pos = 0
    for m in BRACKET_RE.finditer(raw):
        base = re.sub(r"\s+", "", raw[pos:m.start()])
        tail = len(base)
        while tail and not is_kana(base[tail - 1]):
            tail -= 1
        # base[:tail] is leading text this bracket does NOT annotate (kana
        # like the お in お 前[まえ]); base[tail:] is the kanji run it does.
        out.append(html.escape(base[:tail]))
        run = base[tail:]
        if run:
            out.append("<ruby>{}<rt>{}</rt></ruby>".format(
                html.escape(run), html.escape(m.group(0)[1:-1])))
        pos = m.end()
    out.append(html.escape(re.sub(r"\s+", "", raw[pos:])))
    return "".join(out)


def reading_tooltip_html(words: list[str], cap: int = MAX_TOOLTIP_WORDS) -> str:
    """Every word using a (kanji, reading) pair, one per line, each with
    ruby furigana - the hover half of a reading link, whose click half
    opens those same notes in the browser.

    Capped: a very common reading matches a lot of notes, and a tooltip
    taller than the screen can't be read anyway. The click-through still
    reaches all of them, so nothing is lost by trimming the preview.
    """
    if not words:
        return ""
    # Sized here rather than on the shared tooltip's text column, which the
    # similar-kanji tooltip also uses at its own smaller size. line-height
    # has to be roomy enough for the <rt> furigana to sit above each word
    # without the lines colliding.
    # text-align:left is explicit, not inherited: this markup is injected
    # into the reviewer's own card HTML, and most card templates center
    # their body text - without pinning it here, the word list picks that
    # up and renders ragged-centered instead of as a left-aligned column.
    lines = [
        '<div style="font-size:{}px; line-height:1.5; padding:2px 0; '
        'text-align:left;">{}</div>'.format(TOOLTIP_WORD_PX, ruby_html(w))
        for w in words[:cap]
    ]
    if len(words) > cap:
        lines.append(
            '<div style="font-size:{}px; padding:4px 0 0; opacity:0.6; '
            'text-align:left;">+{} more</div>'.format(
                TOOLTIP_TEXT_PX, len(words) - cap))
    return "".join(lines)


def similar_kanji_html(word_kanji: list[str], similar_index, known_index,
                        kanji_defs_db: str, highlight_color: str = "#ff007b",
                        glyph_color: str = "#ffffff") -> str:
    """"Similar: 末(2), 味(5), 沫" per source kanji that has any confusable
    neighbors - folded in from anki_addon_confused_kanji, same rules:

    - every neighbor is shown, none filtered out
    - a neighbor with >=1 note anywhere in the collection is a green,
      clickable, dotted-underline link to those exact notes (nid:1,2,3),
      labeled with the note count in parens - same "(count)" convention
      reading_span uses for on/kun readings; one with zero notes is plain
      non-clickable text with no count (no dead-end search)
    - hovering any neighbor shows the collection words containing it, a
      rule, then its own meanings/readings, via the tooltip this bar
      already renders (see similar_tooltip_html and the shared JS in
      bar_html's returned HTML)
    """
    all_neighbors: list[str] = []
    for kanji in word_kanji:
        all_neighbors.extend(similar_index.confused_with(kanji)[:MAX_SIMILAR_PER_KANJI])
    defs_by_kanji = {e["kanji"]: e for e in lookup(kanji_defs_db, list(dict.fromkeys(all_neighbors)))}

    rows = []
    link_count = 0
    for kanji in word_kanji:
        neighbors = similar_index.confused_with(kanji)[:MAX_SIMILAR_PER_KANJI]
        if not neighbors:
            continue
        links = []
        for neighbor in neighbors:
            if link_count >= MAX_SIMILAR_LINKS:
                break
            note_ids = known_index.note_ids_for(neighbor) if known_index.built else set()
            # Words containing this neighbor, listed above its meanings -
            # same ruby list and cap the readings tooltip uses. Only
            # available once the known-kanji index has finished building;
            # until then the tooltip is just the meanings half, as before.
            neighbor_words = known_index.words_for(neighbor) if known_index.built else []
            tooltip = similar_tooltip_html(
                defs_by_kanji.get(neighbor), neighbor_words, neighbor, glyph_color)
            # data-tooltip-html, not data-tooltip: this content carries
            # <ruby> markup (see similar_tooltip_html). No data-kanji any
            # more - the glyph is baked into that HTML now, since it lives
            # inside the meanings row rather than in a column of its own.
            tooltip_attr = (' data-tooltip-html="{}"'.format(
                                html.escape(tooltip, quote=True))
                             if tooltip else "")
            if note_ids:
                target = "nid:" + ",".join(str(i) for i in sorted(note_ids))
                links.append(
                    '<span onclick="pycmd(\'kanjidefs-confused:{}\'); return false;"{} '
                    'style="cursor:pointer; text-decoration:underline dotted; '
                    'text-underline-offset:2px; color:{};">{}({})</span>'.format(
                        html.escape(target), tooltip_attr, highlight_color,
                        html.escape(neighbor), len(note_ids))
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


def reading_span(kanji: str, reading: str, current: set[str], reading_index,
                  highlight_color: str = "#ff007b") -> str:
    """One on/kun reading as a span, with a collection-count link if the
    (kanji, reading) pair has ever been seen, plain non-clickable text if
    not - same "don't link to an empty search" rule anki_addon_confused_
    kanji applies to its neighbor kanji. Current-card readings are green;
    unindexed/index-still-building shows the reading with no count/link
    rather than blocking the bar.

    `reading` is displayed exactly as kanji_defs.sqlite3 stores it
    (katakana for on-readings, hiragana for kun) - see text_utils.
    katakana_to_hiragana's comment for why the LOOKUP key must be converted
    to hiragana first, even though the on-screen label stays katakana.
    """
    hira = katakana_to_hiragana(reading)
    count, note_ids = ((0, []) if not reading_index.built
                        else reading_index.counts_for(kanji, hira))
    is_current = hira in current
    if count:
        nids = ",".join(str(i) for i in note_ids)
        color = highlight_color if is_current else "inherit"
        weight = "600" if is_current else "inherit"
        # data-tooltip-html (not data-tooltip): the similar-kanji tooltip
        # is plain text set via textContent, while this one is real <ruby>
        # markup and has to go through innerHTML. Two attributes rather
        # than one keeps the similar-kanji path escaping-safe as-is, with
        # the shared JS below picking whichever the hovered span carries.
        # No data-kanji here, unlike the similar-kanji spans: this tooltip
        # is a list of whole WORDS, and a single kanji looming beside them
        # adds nothing the row it was hovered from doesn't already show.
        # The shared tooltip's glyph column hides itself when the attribute
        # is absent (see the hover handler below).
        tooltip = reading_tooltip_html(reading_index.words_for(kanji, hira))
        tooltip_attr = (' data-tooltip-html="{}"'.format(
                            html.escape(tooltip, quote=True))
                         if tooltip else "")
        return (
            '<span onclick="pycmd(\'kanjidefs-reading:{}\'); return false;"{} '
            'style="display:inline-block; white-space:nowrap; cursor:pointer; '
            'text-decoration:underline dotted; '
            'text-underline-offset:2px; color:{}; font-weight:{};">{}({})</span>'.format(
                nids, tooltip_attr, color, weight, html.escape(reading), count)
        )
    color = highlight_color if is_current else "inherit"
    weight = "600" if is_current else "inherit"
    return '<span style="display:inline-block; white-space:nowrap; color:{}; ' \
        'font-weight:{};">{}</span>'.format(
        color, weight, html.escape(reading))


def bar_html(entries: list[dict], current: dict[str, set[str]],
             similar_index, known_index, reading_index, kanji_defs_db: str,
             visible: bool = True, highlight_color: str = "#ff007b",
             night: bool | None = None, notice: str = "") -> str:
    """The bar for one card. `notice` replaces the per-kanji rows with a
    single message (used to tell a fresh install it still needs a note
    field configured) - it reuses the whole bar shell, chrome and toggle
    included, rather than being a second thing to position and theme. With
    a notice there are no `entries` to require.

    `notice` is inserted as HTML, not escaped - it's add-on-authored
    markup, so it can carry <strong> and the like."""
    if not entries and not notice:
        return ""

    # Resolved once for the whole render rather than per token lookup -
    # is_night_mode() probes aqt, and there's no reason to repeat that a
    # dozen times while building one bar. `night` overrides detection so
    # tests/previews can render either theme on demand.
    colors = theme.palette(night)

    # A notice replaces the kanji rows entirely - blanking `entries` lets
    # the per-kanji loops below run over nothing rather than needing the
    # whole row-building block wrapped in an else:.
    if notice:
        entries = []

    # One row per kanji, each a left-anchored flex line rather than a
    # centered inline span - a fixed-width kanji column (2em) then
    # meanings then readings, so every row's meanings start at the same x
    # regardless of how wide the preceding kanji glyph renders, and a long
    # meanings list wraps within its own row instead of being centered as
    # one unbreakable chunk (which is what clipped 公's meanings off the
    # edge in practice - centering has no left edge to wrap back to).
    similar_html_by_kanji = {}
    for e in entries:
        row_html = similar_kanji_html(
            [e["kanji"]], similar_index, known_index, kanji_defs_db, highlight_color,
            colors["text-strong"])
        if row_html:
            similar_html_by_kanji[e["kanji"]] = row_html

    rows = []
    for i, e in enumerate(entries):
        meanings = ", ".join(e["meanings"])
        kanji_current = current.get(e["kanji"], set())
        on_spans = [reading_span(e["kanji"], r, kanji_current, reading_index, highlight_color)
                    for r in e["on"]]
        kun_spans = [reading_span(e["kanji"], r, kanji_current, reading_index, highlight_color)
                     for r in e["kun"]]
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
        border = ("border-top:1px solid {}; margin-top:4px; "
                  "padding-top:4px; ".format(colors["separator"]) if i > 0 else "")
        # One grid per kanji entry: 3 columns (glyph, meanings, readings) x
        # 2 rows (meanings/readings line, then the Similar line). The glyph
        # spans BOTH rows (grid-row:1/3) so it's vertically centered across
        # the entry's full height and the Similar line's own column starts
        # at column 2 - the same x position meanings starts at - rather
        # than being a separate full-width block indented to fake that
        # alignment. grid-template-columns: fixed glyph column, meanings
        # and readings sharing the remaining space evenly regardless of
        # either one's own content length.
        similar_cell = (
            '<div style="grid-column:2 / 4; grid-row:2; text-align:left;">{}</div>'.format(similar_row)
            if similar_row else ""
        )
        rows.append(
            # Explicit text color, not inherited - see the bar background
            # comment below: an opaque bar can no longer borrow contrast
            # from whatever page color shows through it. Readings column
            # pins its own base color too (each reading_span sets
            # "inherit" for the non-current case, which resolves against
            # THIS span's color, not the row's var(--kd-text), unless this
            # span itself pins the same color).
            '<div style="padding:6px 0; {border}">'
            '<div style="display:grid; grid-template-columns:2.4em 1fr 1fr; '
            'column-gap:0.6em; text-align:left; color:{text};">'
            # margin-right in PX, not em: em here resolves against this
            # span's own font-size (46px), so the previous "0.6em" was
            # really 27.6px - three times the grid's own 0.6em column-gap,
            # and it silently rescaled whenever the glyph size changed.
            '<span style="grid-column:1; grid-row:1 / 3; font-size:46px; '
            'color:{text_strong}; align-self:center; margin-right:9px;">{}</span>'
            '<span style="grid-column:2; grid-row:1; font-size:18px; '
            'align-self:center;">{}</span>'
            '<span style="grid-column:3; grid-row:1; font-size:16px; '
            'color:{text}; align-self:start; text-align:right;">{}</span>'
            '{}'
            '</div>'
            '</div>'.format(
                html.escape(e["kanji"]), html.escape(meanings), reading_html, similar_cell,
                border=border, text=colors["text"], text_strong=colors["text-strong"])
        )

    if notice:
        # NOT escaped: the notice is add-on-authored markup (it bolds the
        # menu path), never user or note content, so there's nothing here
        # to escape against. Callers must escape anything they interpolate.
        rows.append(
            '<div style="padding:6px 2px; font-size:15px; line-height:1.5; '
            'text-align:left; color:{};">{}</div>'.format(colors["text"], notice)
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
        # Opaque, matching the other stacking bars' color (theme.TOKENS
        # "bg") - no backdrop-filter, since blur only makes sense over
        # something translucent, and a consistent fixed color across every
        # stacked bar reads as one system rather than several.
        #
        # data-stack-order="0": top-most (lowest order number) - see
        # __init__.py's module docstring and STACK_JS.
        # `display:none` when hidden (rather than not emitting the div at
        # all) - the toggle button's JS needs a stable element to flip, and
        # the stacking/spacer script below needs the element to exist even
        # while hidden so heights recompute correctly the moment it's shown
        # again.
        '<div id="{bar_id}" data-stack-order="{order}" style="position:fixed; '
        'left:0; right:0; bottom:0; {bar_display}'
        'z-index:9998; padding:10px 6px 5px; '
        'background:{bg}; '
        'border-top:1px solid {border};">'
        '<div style="max-width:520px; margin:0 auto;">{rows}</div>'
        '</div>'
        '<div id="{spacer_id}"></div>'
        # Fixed show/hide toggle, bottom-right of the SCREEN (not the bar) -
        # deliberately outside the stacking bar div and not itself
        # data-stack-order'd, so it neither contributes to nor is pushed by
        # any bar's height, and stays put in the same corner whether the
        # bar (or any other stacked add-on's bar) is shown, hidden, or
        # wrapped to several lines. Higher z-index than the bar/tooltip so
        # it is always clickable on top of them.
        '<div id="{toggle_id}" data-visible="{visible_attr}" '
        'data-eye="{eye_svg_js}" data-eye-off="{eye_off_svg_js}" '
        'onclick="var b=document.getElementById(\'{bar_id}\'),'
        's=document.getElementById(\'{spacer_id}\'),'
        'vis=this.getAttribute(\'data-visible\')===\'1\',nv=!vis;'
        'this.setAttribute(\'data-visible\',nv?\'1\':\'0\');'
        'this.innerHTML=nv?this.getAttribute(\'data-eye\'):this.getAttribute(\'data-eye-off\');'
        'b.style.display=nv?\'block\':\'none\';'
        's.style.display=nv?\'block\':\'none\';'
        'pycmd(\'kanjidefs-toggle:\'+(nv?1:0));" '
        # left:calc(100vw - Npx), not right:Npx - a right-anchored fixed
        # element is measured from the edge of the VISIBLE viewport, which
        # narrows the instant a scrollbar appears (e.g. the answer side
        # growing taller than the question side), visibly shifting the
        # button sideways between cards. 100vw always includes the
        # scrollbar's own width regardless of whether one is showing, so
        # anchoring from the left with 100vw stays put either way.
        'style="position:fixed; left:90vw; bottom:6px; '
        'z-index:9999; width:30px; height:30px; display:flex; '
        'align-items:center; justify-content:center; border-radius:4px; '
        'background:{toggle_bg}; color:{toggle_text}; '
        'border:1px solid {toggle_border}; '
        'cursor:pointer; user-select:none;">{toggle_glyph}</div>'
        # Shared hover tooltip for similar-kanji links (folded in from
        # anki_addon_confused_kanji) - one element, positioned above
        # whichever span is hovered, filled from that span's data-tooltip
        # attribute (already in the page from similar_kanji_html/lookup,
        # no per-hover query) plus its own text content (the hovered kanji
        # itself). Flex row: the kanji glyph on the left, sized/colored
        # like the bar's own big kanji column, then the meanings/readings
        # text - align-items:stretch (the flex default) makes the glyph
        # column's height track the text column's, so the glyph area grows
        # vertically with the tooltip rather than needing a fixed height;
        # the glyph's own line-height:1 plus the row's align-items:center
        # keeps the character centered in whatever height that ends up
        # being, with room to breathe via the row gap. white-space:pre-line
        # on the text column renders tooltip_text's embedded "\n" as a
        # real line break without HTML in the attribute.
        # A plain vertical block, NOT a glyph column + text column: the
        # similar-kanji tooltip's word list spans the full width and its
        # glyph now sits inside the meanings row that similar_tooltip_html
        # builds, so there's nothing left for an outer flex row to arrange.
        # (The glyph used to be a sibling span here, filled by the hover JS
        # from data-kanji - that forced the word list into a column beside
        # it, indented past a character it has nothing to do with.)
        # max-height/overflow are set by the hover handler, which is the
        # only thing that knows how much room is left above the hovered
        # span.
        '<div id="{tooltip_id}" style="position:fixed; display:none; '
        'z-index:10000; max-width:420px; padding:10px 14px; '
        'background:{tooltip_bg}; color:{tooltip_text}; '
        'border-radius:6px; border:1px solid {tooltip_border}; '
        'box-shadow:0 2px 10px {tooltip_shadow}; pointer-events:none;">'
        '<span id="{tooltip_text_id}" style="display:block; '
        'font-size:{tooltip_text_px}px; '
        'line-height:1.4; white-space:pre-line; min-width:0; '
        'text-align:left;"></span>'
        '</div>'
        '<script>(function(){{'
        'var b=document.getElementById("{bar_id}"),'
        's=document.getElementById("{spacer_id}"),'
        't=document.getElementById("{tooltip_id}"),'
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
        'if(t&&tx){{'
        'b.addEventListener("mouseover",function(e){{'
        'var el=e.target.closest?e.target.closest("[data-tooltip],[data-tooltip-html]"):null;'
        'if(!el)return;'
        # Both tooltips now build their own markup (glyph included, for the
        # similar-kanji one) - this just drops it in. The data-tooltip
        # branch is a plain-text fallback nothing currently emits, kept
        # because it costs two lines and keeps this element reusable.
        'var htm=el.getAttribute("data-tooltip-html");'
        'if(htm){{tx.innerHTML=htm;}}else{{tx.textContent=el.getAttribute("data-tooltip")||"";}}'
        't.style.display="block";'
        'var r=el.getBoundingClientRect();'
        't.style.left=Math.max(4,r.left)+"px";'
        # Pin the tooltip's BOTTOM just above the hovered span so it grows
        # upward as the word list gets longer, and cap its height against
        # the space actually available above the span - a reading matching
        # 20 words is far taller than the old two-line similar-kanji
        # tooltip this element was sized for, and without a cap the top of
        # a long list would run off the top of the window.
        'var gap=window.innerHeight-r.top+6;'
        't.style.bottom=gap+"px";'
        't.style.maxHeight=Math.max(80,window.innerHeight-gap-8)+"px";'
        't.style.overflowY="auto";'
        '}});'
        'b.addEventListener("mouseout",function(e){{'
        'var el=e.target.closest?e.target.closest("[data-tooltip],[data-tooltip-html]"):null;'
        'if(!el)return;'
        't.style.display="none";'
        '}});'
        '}}'
        '}})();</script>'.format(
            bar_id=THIS_BAR_ID, spacer_id=THIS_SPACER_ID, tooltip_id=THIS_TOOLTIP_ID,
            tooltip_text_id=THIS_TOOLTIP_TEXT_ID,
            toggle_id=THIS_TOGGLE_ID, order=THIS_STACK_ORDER, offset=BOTTOM_OFFSET_PX,
            rows="".join(rows), stack_js=STACK_JS,
            bar_display="" if visible else "display:none; ",
            visible_attr="1" if visible else "0",
            # data-eye/data-eye-off carry the raw SVG markup for the click
            # handler's innerHTML swap, HTML-escaped since they sit inside
            # an HTML attribute (the SVGs themselves have no literal " or <
            # that would need escaping, but html.escape is cheap insurance
            # against that changing later without anyone noticing here).
            eye_svg_js=html.escape(_EYE_SVG), eye_off_svg_js=html.escape(_EYE_OFF_SVG),
            toggle_glyph=_EYE_SVG if visible else _EYE_OFF_SVG,
            bg=colors["bg"], border=colors["border"], text=colors["text"],
            toggle_bg=colors["bg"], toggle_text=colors["text"],
            toggle_border=colors["border"],
            tooltip_bg=colors["tooltip-bg"], tooltip_text=colors["tooltip-text"],
            tooltip_border=colors["tooltip-border"], tooltip_shadow=colors["tooltip-shadow"],
            tooltip_text_px=TOOLTIP_TEXT_PX)
    )
