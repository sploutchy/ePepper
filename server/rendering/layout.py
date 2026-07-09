"""Render structured recipe data to a 800x480 1-bit image for the e-ink panel.

The visual language follows the "Two-up with hung folio" proposal (`D` in
the e-ink design exploration) — editorial cousin of the web app's recipe
page, translated for 1-bit rendering:

  - Title in DejaVu Serif Bold 28 px (vs. today's sans 26 px).
  - Source tucked inline after the title in DejaVu Sans 13 px.
  - Page indicator as a hung folio in the top-right corner: DejaVu
    Serif Bold Italic 22 px, baseline-aligned with the title row.
  - Meta line (time / servings) in tracked uppercase Sans Bold 11 px.
  - Section labels ("INGREDIENTS", "INSTRUCTIONS") in tracked uppercase
    Sans Bold 11 px — same family as meta, smaller than today's 16 px
    so the columns get vertical space back.
  - Sub-headings (e.g. "Banchan prep") in DejaVu Serif Bold Italic 16
    px with a 1-px rule under them — translates the web app's
    sub-section serif treatment.
  - Step numerals stay inline ("1.", "2." …) in Sans Bold 14 px — the
    title carries the editorial flourish, the body stays utilitarian
    for legibility at 1-bit.
  - Body in DejaVu Sans 14 px.

The API surface (`render_recipe(recipe, page, source) -> (img,
total_pages)` and `render_idle()`) is unchanged. The pagination + short-
column repeat heuristic + the L10n table are also preserved — only the
visual treatment moves.
"""

import re
import textwrap

from PIL import Image, ImageDraw, ImageFont

from processing.recipes import normalize_recipe_for_render
from config import (
    DISPLAY_WIDTH,
    DISPLAY_HEIGHT,
    MARGIN,
    COLUMN_GAP,
    INGREDIENTS_WIDTH_RATIO,
    FONT_REGULAR,
    FONT_BOLD,
    FONT_SERIF_BOLD,
    FONT_SERIF_BOLD_ITALIC,
)

# 10x10 glyphs marking which physical button does what. Drawn at the very
# top of the panel (Y=2) over the title's top margin. These X centers are
# server-side only — the firmware draws no on-screen UI — and are tuned to
# sit directly above the physical reTerminal keys.
_BTN_GLYPH_Y = 2
_BTN_PREV_X = 450
_BTN_NEXT_X = 490
_BTN_REFRESH_X = 545
_GLYPH_W = 10

_GLYPH_PREV = (
    "....#.....",
    "...##.....",
    "..###.....",
    ".####.....",
    "#####.....",
    "#####.....",
    ".####.....",
    "..###.....",
    "...##.....",
    "....#.....",
)
_GLYPH_NEXT = (
    "....#.....",
    "....##....",
    "....###...",
    "....####..",
    "....#####.",
    "....#####.",
    "....####..",
    "....###...",
    "....##....",
    "....#.....",
)
# Refresh icon: 180°-rotationally-symmetric two-arrow loop. Each filled
# cell at (r, c) has its mirror at (9-r, 9-c), so the icon reads the same
# upside-down — two chasing chevron tails curling around the centre.
_GLYPH_REFRESH = (
    ".....###..",
    "####....#.",
    ".###.....#",
    ".###.....#",
    "#..#.....#",
    "#.....#..#",
    "#.....###.",
    "#.....###.",
    ".#....####",
    "..###.....",
)


def _draw_glyph(draw, x: int, y: int, glyph: tuple[str, ...]) -> None:
    """Paint a '#'-encoded bitmap at (x, y)."""
    for row, line in enumerate(glyph):
        for col, ch in enumerate(line):
            if ch == "#":
                draw.point((x + col, y + row), fill=0)


def _draw_button_glyphs(draw, page: int, total_pages: int) -> None:
    """PREV / NEXT only when a page exists in that direction; REFRESH always."""
    if page > 1:
        _draw_glyph(draw, _BTN_PREV_X - _GLYPH_W // 2, _BTN_GLYPH_Y, _GLYPH_PREV)
    if page < total_pages:
        _draw_glyph(draw, _BTN_NEXT_X - _GLYPH_W // 2, _BTN_GLYPH_Y, _GLYPH_NEXT)
    _draw_glyph(draw, _BTN_REFRESH_X - _GLYPH_W // 2, _BTN_GLYPH_Y, _GLYPH_REFRESH)


def render_idle() -> Image.Image:
    """Cleared-display panel: blank, with only the small button-position
    glyph above the physical refresh key so it's clear which button wakes
    content back up.
    """
    img = Image.new("1", (DISPLAY_WIDTH, DISPLAY_HEIGHT), 1)
    draw = ImageDraw.Draw(img)
    _draw_glyph(draw, _BTN_REFRESH_X - _GLYPH_W // 2, _BTN_GLYPH_Y, _GLYPH_REFRESH)
    return img


# Localized column headings and page label. Section labels are rendered
# uppercase + tracked (the editorial small-caps treatment), so the strings
# below stay in their natural case and the renderer uppercases on draw.
_L10N = {
    "de": {"ingredients": "Zutaten", "instructions": "Zubereitung", "page": "Seite", "servings": "Portionen", "from": "aus"},
    "fr": {"ingredients": "Ingrédients", "instructions": "Préparation", "page": "page", "servings": "portions", "from": "de"},
    "it": {"ingredients": "Ingredienti", "instructions": "Preparazione", "page": "pagina", "servings": "porzioni", "from": "da"},
    "en": {"ingredients": "Ingredients", "instructions": "Instructions", "page": "page", "servings": "servings", "from": "from"},
}


# Tracking (in pixels) added between characters for the small-caps labels.
# 1-bit rendering benefits from extra space at small sizes — the eye reads
# the silhouette of each glyph instead of the run-on cluster.
_TRACK_PX = 2


def _tracked(draw, xy, text: str, font, fill: int = 0, tracking: int = _TRACK_PX) -> int:
    """Draw `text` letter-by-letter with `tracking` extra px between glyphs.
    Returns the total width drawn so callers can right-align or measure.
    """
    x, y = xy
    start_x = x
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += int(round(font.getlength(ch))) + tracking
    # Last char shouldn't trail tracking.
    return max(0, x - tracking - start_x)


def _tracked_width(text: str, font, tracking: int = _TRACK_PX) -> int:
    total = 0
    for ch in text:
        total += int(round(font.getlength(ch))) + tracking
    return max(0, total - tracking)


def render_recipe(
    recipe: dict,
    page: int = 1,
    source: str | None = None,
) -> tuple[Image.Image, int]:
    """Render a recipe dict to a 1-bit image for e-ink display.

    Args:
        recipe: dict with title, total_time, servings, ingredients, instructions
        page: 1-based page number
        source: optional humanized source name ("Fooby", "BBC", …) drawn
            in the meta font just below the title.

    Returns:
        (image, total_pages)
    """
    # Load fonts. The DejaVu Serif Bold Italic variant only ships with the
    # `fonts-dejavu` apt package (not -core); the Dockerfile installs the
    # full package. The OSError fallback keeps the renderer alive if it
    # ever runs in an environment that's missing fonts entirely.
    try:
        font_title = ImageFont.truetype(FONT_SERIF_BOLD, 28)
        font_source = ImageFont.truetype(FONT_REGULAR, 13)
        font_folio = ImageFont.truetype(FONT_SERIF_BOLD_ITALIC, 22)
        font_meta = ImageFont.truetype(FONT_BOLD, 11)
        font_section = ImageFont.truetype(FONT_BOLD, 11)
        font_heading = ImageFont.truetype(FONT_SERIF_BOLD_ITALIC, 16)
        font_body = ImageFont.truetype(FONT_REGULAR, 14)
    except OSError:
        font_title = ImageFont.load_default()
        font_source = font_meta = font_section = font_heading = font_body = font_folio = font_title

    # Defense in depth: legacy `parsed_json` rows persisted before the
    # upsert path started normalizing on write still need the dedupe
    # pass. New rows come in already canonical, so this is a no-op for
    # the modern path.
    recipe = normalize_recipe_for_render(recipe)

    lang = recipe.get("lang", "en")
    strings = _L10N.get(lang, _L10N["en"])

    # --- Compute header height (title + source + meta + divider) ---
    title = recipe.get("title", "Untitled Recipe")
    title_lines = textwrap.wrap(title, width=40)

    # Inline the source onto the last title line when it fits, otherwise
    # bump it to its own line below.
    source_inline = False
    if source and title_lines:
        try:
            last_w = font_title.getlength(title_lines[-1])
            src_w = font_source.getlength(f"  {strings['from']} {source}")
            usable = DISPLAY_WIDTH - 2 * MARGIN
            source_inline = last_w + src_w <= usable
        except AttributeError:
            source_inline = False

    title_line_h = font_title.size + 4
    header_h = MARGIN + len(title_lines) * title_line_h + 4
    if source and not source_inline:
        header_h += font_source.size + 4
    # Meta line — always reserved (carries no info on its own but holds
    # vertical rhythm; the hung folio sits to the right of the title).
    header_h += font_meta.size + 6
    header_h += 4 + 10  # space + divider line + space

    # --- Layout columns ---
    col_left_w = int((DISPLAY_WIDTH - 2 * MARGIN - COLUMN_GAP) * INGREDIENTS_WIDTH_RATIO)
    col_right_x = MARGIN + col_left_w + COLUMN_GAP
    col_right_w = DISPLAY_WIDTH - col_right_x - MARGIN
    col_top = header_h

    # Characters per column — rough heuristic for textwrap fallback. The
    # final wrapping below the section label uses width-measured wrapping
    # via _wrap_to_width.
    line_h = font_body.size + 4
    heading_line_h = font_heading.size + 4
    # Section label sits as a tracked small-cap; reserve its height +
    # bottom space. Smaller than the previous bold-sans h2 — gives the
    # columns ~30 px more vertical room.
    section_h = font_section.size + 12
    footer_reserve = 0
    available_h = DISPLAY_HEIGHT - col_top - MARGIN - section_h - footer_reserve

    # --- Pre-wrap ingredients into line groups (measured width) ---
    ingredients = recipe.get("ingredients", [])
    ingr_groups: list[list[str]] = [
        _wrap_to_width(f"·  {item}", font_body, col_left_w) for item in ingredients
    ]

    # Paginate ingredients
    ingr_pages: list[list[int]] = []
    current_page: list[int] = []
    used_h = 0
    for idx, lines in enumerate(ingr_groups):
        block_h = len(lines) * line_h
        if used_h + block_h > available_h and current_page:
            ingr_pages.append(current_page)
            current_page = [idx]
            used_h = block_h
        else:
            current_page.append(idx)
            used_h += block_h
    if current_page:
        ingr_pages.append(current_page)

    # --- Pre-wrap instructions into blocks ---
    # normalize_recipe_for_render above has already canonicalized the
    # instruction list (typed dicts, dedup'd headings); the only fixup
    # left here is the legacy "bare string step" case which the
    # normalizer also coerces — left for paranoia.
    raw_instructions = recipe.get("instructions", [])
    instructions: list[dict] = []
    for item in raw_instructions:
        if isinstance(item, str):
            instructions.append({"type": "step", "text": item})
        else:
            instructions.append(item)

    all_blocks: list[dict] = []
    # Sub-headings restart the step counter (matches the web app and the
    # rendering proposal D in /tmp/eink-proposals/render_D.py).
    #
    # Single-step sections drop the "N." prefix and render as plain
    # body text — mirrors the web's .step-solo treatment. The inline
    # numeral marks a sequence; a section of one doesn't have one
    # to mark, so the prefix would just be visual noise next to a
    # solitary instruction.
    section_step_counts: list[int] = [0] * len(instructions)
    current_section_indices: list[int] = []
    for idx, item in enumerate(instructions):
        if item["type"] == "heading":
            n = len(current_section_indices)
            for i in current_section_indices:
                section_step_counts[i] = n
            current_section_indices = []
        else:
            current_section_indices.append(idx)
    n = len(current_section_indices)
    for i in current_section_indices:
        section_step_counts[i] = n

    step_num = 0
    for idx, item in enumerate(instructions):
        if item["type"] == "heading":
            step_num = 0
            wrapped = _wrap_to_width(item["text"], font_heading, col_right_w)
            all_blocks.append({"type": "heading", "lines": wrapped, "font": font_heading, "line_h": heading_line_h})
        else:
            step_num += 1
            if section_step_counts[idx] == 1:
                text = item["text"]
            else:
                text = f"{step_num}. {item['text']}"
            wrapped = _wrap_to_width(text, font_body, col_right_w)
            all_blocks.append({"type": "step", "lines": wrapped, "font": font_body, "line_h": line_h})

    # Paginate instructions. Bundle each heading with the first non-heading
    # block that follows it so a heading + first step land on the same page.
    def _block_h(b: dict) -> int:
        extra = 14 if b["type"] == "heading" else 6  # heading gets a rule + breathing room
        return len(b["lines"]) * b["line_h"] + extra

    groups: list[list[int]] = []
    i = 0
    while i < len(all_blocks):
        if all_blocks[i]["type"] == "heading":
            j = i + 1
            while j < len(all_blocks) and all_blocks[j]["type"] == "heading":
                j += 1
            if j < len(all_blocks):
                groups.append(list(range(i, j + 1)))
                i = j + 1
            else:
                groups.append(list(range(i, j)))
                i = j
        else:
            groups.append([i])
            i += 1

    instr_pages: list[list[int]] = []
    current_page = []
    used_h = 0
    for g in groups:
        gh = sum(_block_h(all_blocks[k]) for k in g)
        if used_h + gh > available_h and current_page:
            instr_pages.append(current_page)
            current_page = list(g)
            used_h = gh
        else:
            current_page.extend(g)
            used_h += gh
    if current_page:
        instr_pages.append(current_page)

    base_pages = max(len(ingr_pages), len(instr_pages), 1)
    total_pages = base_pages
    page = max(1, min(page, total_pages))

    img = Image.new("1", (DISPLAY_WIDTH, DISPLAY_HEIGHT), 1)
    draw = ImageDraw.Draw(img)

    # Hung folio — page indicator drawn in italic-serif at the top-right,
    # baseline-aligned with the title. Only shown for multi-page recipes.
    folio_text = f"{page} / {total_pages}" if total_pages > 1 else ""

    # --- Recipe page: serif title + inline source + hung folio + meta + rule + 2 cols ---
    y = MARGIN
    for i, line in enumerate(title_lines):
        draw.text((MARGIN, y), line, font=font_title, fill=0)
        is_last = i == len(title_lines) - 1
        if is_last and source and source_inline:
            last_w = font_title.getlength(line)
            src_y = y + (font_title.size - font_source.size) - 2
            draw.text(
                (MARGIN + last_w + 8, src_y),
                f"{strings['from']} {source}",
                font=font_source, fill=0,
            )
        # Hung folio rides the top-right corner on the first title line.
        if i == 0 and folio_text:
            fw = int(font_folio.getlength(folio_text))
            folio_y = y + (font_title.size - font_folio.size) - 2
            draw.text(
                (DISPLAY_WIDTH - MARGIN - fw, folio_y),
                folio_text,
                font=font_folio, fill=0,
            )
        y += title_line_h
    if source and not source_inline:
        draw.text((MARGIN, y), f"{strings['from']} {source}", font=font_source, fill=0)
        y += font_source.size + 4
    y += 4

    # Meta — tracked uppercase. Carries time + servings; if neither, line
    # collapses to empty but the vertical space is preserved for rhythm.
    meta_parts: list[str] = []
    if recipe.get("total_time"):
        meta_parts.append(f"{recipe['total_time']} MIN")
    if recipe.get("servings"):
        servings_raw = str(recipe["servings"])
        servings_num = re.sub(r"[^\d]", "", servings_raw)
        label = strings["servings"].upper()
        if servings_num:
            meta_parts.append(f"{servings_num} {label}")
        else:
            meta_parts.append(servings_raw.upper())
    if meta_parts:
        _tracked(draw, (MARGIN, y), "  ·  ".join(meta_parts), font_meta)
    y += font_meta.size + 6

    y += 4
    draw.line([(MARGIN, y), (DISPLAY_WIDTH - MARGIN, y)], fill=0, width=1)
    y += 10

    repeat_ingr = len(ingr_pages) == 1 and base_pages > 1
    repeat_instr = len(instr_pages) == 1 and base_pages > 1

    # --- Section labels (tracked small-caps) ---
    _tracked(draw, (MARGIN, col_top), strings["ingredients"].upper(), font_section)
    _tracked(draw, (col_right_x, col_top), strings["instructions"].upper(), font_section)

    # --- Vertical hairline divider ---
    div_x = MARGIN + col_left_w + COLUMN_GAP // 2
    draw.line([(div_x, col_top), (div_x, DISPLAY_HEIGHT - MARGIN)], fill=0, width=1)

    col_body_top = col_top + section_h

    # --- Left column: ingredients for this page ---
    y_left = col_body_top
    if ingr_pages and (repeat_ingr or page <= len(ingr_pages)):
        ingr_src = 0 if repeat_ingr else page - 1
        for grp_idx in ingr_pages[ingr_src]:
            for wline in ingr_groups[grp_idx]:
                if y_left + line_h > DISPLAY_HEIGHT - MARGIN:
                    break
                draw.text((MARGIN, y_left), wline, font=font_body, fill=0)
                y_left += line_h

    # --- Right column: instructions for this page ---
    y_right = col_body_top
    if instr_pages and (repeat_instr or page <= len(instr_pages)):
        instr_src = 0 if repeat_instr else page - 1
        for block_idx in instr_pages[instr_src]:
            block = all_blocks[block_idx]
            if block["type"] == "heading":
                y_right += 4
                if block["lines"]:
                    draw.text((col_right_x, y_right), block["lines"][0], font=font_heading, fill=0)
                    rule_y = y_right + font_heading.size + 2
                    draw.line([(col_right_x, rule_y), (DISPLAY_WIDTH - MARGIN, rule_y)], fill=0, width=1)
                    y_right = rule_y + 6
                continue
            for wline in block["lines"]:
                if y_right + block["line_h"] > DISPLAY_HEIGHT - MARGIN:
                    break
                draw.text((col_right_x, y_right), wline, font=block["font"], fill=0)
                y_right += block["line_h"]
            y_right += 6

    # --- Top: button glyphs above the physical reTerminal keys ---
    _draw_button_glyphs(draw, page, total_pages)

    return img, total_pages


def _wrap_to_width(text: str, font, max_w: int) -> list[str]:
    """Greedy word-wrap by measured pixel width.

    Falls back to a hard split inside a word if a single token exceeds
    `max_w`. Returns at least one (possibly empty) line so callers can
    treat the result as a non-empty list without guarding.
    """
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    cur = ""
    for w in words:
        candidate = w if not cur else cur + " " + w
        if font.getlength(candidate) <= max_w:
            cur = candidate
        else:
            if cur:
                lines.append(cur)
            if font.getlength(w) > max_w:
                buf = ""
                for ch in w:
                    if font.getlength(buf + ch) > max_w and buf:
                        lines.append(buf)
                        buf = ch
                    else:
                        buf += ch
                cur = buf
            else:
                cur = w
    if cur:
        lines.append(cur)
    return lines
