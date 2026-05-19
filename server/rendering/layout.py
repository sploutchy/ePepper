"""Render structured recipe data to a beautiful 800x430 B/W image."""

import re
import textwrap

from PIL import Image, ImageDraw, ImageFont

from config import (
    DISPLAY_WIDTH,
    DISPLAY_HEIGHT,
    RECIPE_HEIGHT,
    MARGIN,
    COLUMN_GAP,
    INGREDIENTS_WIDTH_RATIO,
    FONT_REGULAR,
    FONT_BOLD,
)

# 10x10 glyphs marking which physical button does what. Drawn at the very
# top of the panel (Y=2) over the title's top margin. X centers mirror the
# firmware constants in esp32/include/config.h (BTN_GLYPH_*_X) so they sit
# directly above the physical reTerminal keys.
_BTN_GLYPH_Y = 2
_BTN_PREV_X = 450
_BTN_NEXT_X = 490
_BTN_REFRESH_X = 545
_GLYPH_W = 10
_GLYPH_H = 10

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


def _draw_glyph_scaled(draw, x: int, y: int, glyph: tuple[str, ...], scale: int) -> None:
    """Paint a '#'-encoded bitmap at (x, y), each cell expanded to scale × scale px."""
    for row, line in enumerate(glyph):
        for col, ch in enumerate(line):
            if ch == "#":
                draw.rectangle(
                    [x + col * scale, y + row * scale,
                     x + (col + 1) * scale - 1, y + (row + 1) * scale - 1],
                    fill=0,
                )


def render_idle() -> Image.Image:
    """Cleared-display panel: a large refresh icon + hint, plus the usual
    small button glyph so the user can spatially map the icon to the
    physical refresh key.
    """
    img = Image.new("1", (DISPLAY_WIDTH, DISPLAY_HEIGHT), 1)
    draw = ImageDraw.Draw(img)

    # Small glyph above the physical refresh button.
    _draw_glyph(draw, _BTN_REFRESH_X - _GLYPH_W // 2, _BTN_GLYPH_Y, _GLYPH_REFRESH)

    # Big centered refresh icon (8× scale → 80×80 px).
    scale = 8
    big_w = _GLYPH_W * scale
    icon_x = (DISPLAY_WIDTH - big_w) // 2
    icon_y = (DISPLAY_HEIGHT - big_w) // 2 - 30
    _draw_glyph_scaled(draw, icon_x, icon_y, _GLYPH_REFRESH, scale)

    # Hint below the big icon.
    font = ImageFont.truetype(FONT_REGULAR, 28)
    text = "Press refresh to start"
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    draw.text(
        ((DISPLAY_WIDTH - tw) // 2, icon_y + big_w + 24),
        text, font=font, fill=0,
    )
    return img


# Localized column headings and page label
_L10N = {
    "de": {"ingredients": "Zutaten", "instructions": "Zubereitung", "page": "Seite", "servings": "Portionen", "notes": "Notizen"},
    "fr": {"ingredients": "Ingrédients", "instructions": "Préparation", "page": "page", "servings": "portions", "notes": "Notes"},
    "it": {"ingredients": "Ingredienti", "instructions": "Preparazione", "page": "pagina", "servings": "porzioni", "notes": "Note"},
    "es": {"ingredients": "Ingredientes", "instructions": "Preparación", "page": "página", "servings": "porciones", "notes": "Notas"},
    "nl": {"ingredients": "Ingrediënten", "instructions": "Bereiding", "page": "pagina", "servings": "porties", "notes": "Notities"},
    "en": {"ingredients": "Ingredients", "instructions": "Instructions", "page": "page", "servings": "servings", "notes": "Notes"},
}


def render_recipe(
    recipe: dict,
    page: int = 1,
    comments: list[str] | None = None,
    rating: int | None = None,
) -> tuple[Image.Image, int]:
    """Render a recipe dict to a 1-bit image for e-ink display.

    Args:
        recipe: dict with title, total_time, servings, ingredients, instructions
        page: 1-based page number
        comments: optional list of comment strings. When non-empty, an extra
            "Notes" page is appended after the recipe pages.
        rating: optional 1-5 star rating from the library; rendered next to
            time / servings on the meta line.

    Returns:
        (image, total_pages)
    """
    # Load fonts
    try:
        font_title = ImageFont.truetype(FONT_BOLD, 26)
        font_meta = ImageFont.truetype(FONT_REGULAR, 14)
        font_heading = ImageFont.truetype(FONT_BOLD, 16)
        font_body = ImageFont.truetype(FONT_REGULAR, 14)
    except OSError:
        # Fallback to default font if DejaVu not available
        font_title = ImageFont.load_default()
        font_meta = font_title
        font_heading = font_title
        font_body = font_title

    lang = recipe.get("lang", "en")
    strings = _L10N.get(lang, _L10N["en"])

    # --- Compute header height (title + meta + divider) without drawing ---
    title = recipe.get("title", "Untitled Recipe")
    title_lines = textwrap.wrap(title, width=40)
    header_h = MARGIN + len(title_lines) * (font_title.size + 4) + 4

    meta_parts = []
    if recipe.get("total_time"):
        meta_parts.append(f"{recipe['total_time']} min")
    if recipe.get("servings"):
        servings_raw = str(recipe["servings"])
        servings_num = re.sub(r"[^\d]", "", servings_raw)
        if servings_num:
            meta_parts.append(f"{servings_num} {strings['servings']}")
        else:
            meta_parts.append(servings_raw)
    if rating and 1 <= rating <= 5:
        meta_parts.append("★" * rating)  # ★ U+2605 — DejaVu Sans has this glyph
    # Always reserve the meta line — it carries the page indicator on the right
    # even when meta_parts is empty, and the extra ~20 px is negligible.
    header_h += font_meta.size + 6
    header_h += 4 + 10  # space + divider line + space

    # --- Two-column layout ---
    col_left_w = int((DISPLAY_WIDTH - 2 * MARGIN - COLUMN_GAP) * INGREDIENTS_WIDTH_RATIO)
    col_right_x = MARGIN + col_left_w + COLUMN_GAP
    col_right_w = DISPLAY_WIDTH - col_right_x - MARGIN
    col_top = header_h

    # Characters per column based on font size and width
    chars_left = max(10, col_left_w // 8)
    chars_right = max(10, col_right_w // 8)

    line_h = font_body.size + 4
    heading_line_h = font_heading.size + 4
    heading_space = font_heading.size + 8  # space for column heading
    # No footer: the page indicator now sits on the meta line and there is no
    # battery glyph on screen any more.
    footer_reserve = 0
    available_h = RECIPE_HEIGHT - col_top - MARGIN - heading_space - footer_reserve

    # --- Pre-wrap ingredients into line groups ---
    ingredients = recipe.get("ingredients", [])
    ingr_groups: list[list[str]] = []  # each group = wrapped lines for one ingredient
    for item in ingredients:
        ingr_groups.append(textwrap.wrap(f"· {item}", width=chars_left))

    # Paginate ingredients
    ingr_pages: list[list[int]] = []  # each page = list of ingredient group indices
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
    raw_instructions = recipe.get("instructions", [])
    instructions: list[dict] = []
    for item in raw_instructions:
        if isinstance(item, str):
            instructions.append({"type": "step", "text": item})
        else:
            instructions.append(item)

    all_blocks: list[dict] = []
    step_num = 0
    for item in instructions:
        if item["type"] == "heading":
            wrapped = textwrap.wrap(item["text"], width=chars_right)
            all_blocks.append({"type": "heading", "lines": wrapped, "font": font_heading, "line_h": heading_line_h})
        else:
            step_num += 1
            wrapped = textwrap.wrap(f"{step_num}. {item['text']}", width=chars_right)
            all_blocks.append({"type": "step", "lines": wrapped, "font": font_body, "line_h": line_h})

    # Paginate instructions. Group every heading with its following block
    # so they land on the same page — steps don't split mid-block, so the
    # earlier "reserve one line of the next block" widow heuristic still
    # let a heading get orphaned whenever the next step needed more than
    # one line of remaining space.
    def _block_h(b: dict) -> int:
        return len(b["lines"]) * b["line_h"] + (10 if b["type"] == "heading" else 6)

    groups: list[list[int]] = []
    i = 0
    while i < len(all_blocks):
        if all_blocks[i]["type"] == "heading" and i + 1 < len(all_blocks):
            groups.append([i, i + 1])
            i += 2
        else:
            groups.append([i])
            i += 1

    instr_pages: list[list[int]] = []
    current_page: list[int] = []
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

    # Base pages from the recipe columns
    base_pages = max(len(ingr_pages), len(instr_pages), 1)

    # --- Paginate notes (full-width, single column) ---
    notes_pages: list[list[list[str]]] = []  # each page = list of comment paragraphs (each a list of wrapped lines)
    if comments:
        chars_full = max(20, (DISPLAY_WIDTH - 2 * MARGIN) // 8)
        notes_header_h = MARGIN + font_title.size + 4 + 4 + 10  # title + space + divider + space
        notes_available_h = RECIPE_HEIGHT - notes_header_h - MARGIN - footer_reserve

        paragraphs = [textwrap.wrap(c.strip(), width=chars_full) or [""] for c in comments]
        gap_h = line_h  # blank line between comments
        current_para_lines: list[list[str]] = []
        used = 0
        for para in paragraphs:
            need = len(para) * line_h + (gap_h if current_para_lines else 0)
            if used + need > notes_available_h and current_para_lines:
                notes_pages.append(current_para_lines)
                current_para_lines = [para]
                used = len(para) * line_h
            else:
                current_para_lines.append(para)
                used += need
        if current_para_lines:
            notes_pages.append(current_para_lines)

    total_pages = base_pages + len(notes_pages)
    page = max(1, min(page, total_pages))

    # Create the image now that we know which page we're rendering
    img = Image.new("1", (DISPLAY_WIDTH, RECIPE_HEIGHT), 1)
    draw = ImageDraw.Draw(img)

    is_notes_page = page > base_pages

    page_text = (
        f"{strings['page']} {page} / {total_pages}" if total_pages > 1 else ""
    )

    if is_notes_page:
        # --- Notes page ---
        notes_idx = page - base_pages - 1
        y = MARGIN
        draw.text((MARGIN, y), strings["notes"], font=font_title, fill=0)
        # Page indicator on the title row, right-aligned and baseline-matched.
        if page_text:
            bbox = draw.textbbox((0, 0), page_text, font=font_meta)
            tw = bbox[2] - bbox[0]
            page_y = y + (font_title.size - font_meta.size) - 2
            draw.text((DISPLAY_WIDTH - MARGIN - tw, page_y), page_text, font=font_meta, fill=0)
        y += font_title.size + 4 + 4
        draw.line([(MARGIN, y), (DISPLAY_WIDTH - MARGIN, y)], fill=0, width=1)
        y += 10

        for i, para_lines in enumerate(notes_pages[notes_idx]):
            if i > 0:
                y += line_h  # blank line between comments
            for wline in para_lines:
                if y + line_h > RECIPE_HEIGHT - MARGIN:
                    break
                draw.text((MARGIN, y), wline, font=font_body, fill=0)
                y += line_h
    else:
        # --- Recipe page: draw title, meta+page, divider, then two columns ---
        y = MARGIN
        for line in title_lines:
            draw.text((MARGIN, y), line, font=font_title, fill=0)
            y += font_title.size + 4
        y += 4

        meta_text = "  ·  ".join(meta_parts)
        if meta_text:
            draw.text((MARGIN, y), meta_text, font=font_meta, fill=0)
        if page_text:
            bbox = draw.textbbox((0, 0), page_text, font=font_meta)
            tw = bbox[2] - bbox[0]
            draw.text((DISPLAY_WIDTH - MARGIN - tw, y), page_text, font=font_meta, fill=0)
        y += font_meta.size + 6

        y += 4
        draw.line([(MARGIN, y), (DISPLAY_WIDTH - MARGIN, y)], fill=0, width=1)
        y += 10

        # If one column fits on a single page but the other needs multiple,
        # repeat the short column on every page so the cook can see both
        # ingredients and instructions without flipping back.
        repeat_ingr = len(ingr_pages) == 1 and base_pages > 1
        repeat_instr = len(instr_pages) == 1 and base_pages > 1

        # --- Draw left column: Ingredients for this page ---
        y_left = col_top
        draw.text((MARGIN, y_left), strings["ingredients"], font=font_heading, fill=0)
        y_left += heading_space

        if ingr_pages and (repeat_ingr or page <= len(ingr_pages)):
            ingr_src = 0 if repeat_ingr else page - 1
            for grp_idx in ingr_pages[ingr_src]:
                for wline in ingr_groups[grp_idx]:
                    if y_left + line_h > RECIPE_HEIGHT - MARGIN:
                        break
                    draw.text((MARGIN, y_left), wline, font=font_body, fill=0)
                    y_left += line_h

        # --- Vertical divider ---
        div_x = MARGIN + col_left_w + COLUMN_GAP // 2
        draw.line([(div_x, col_top), (div_x, RECIPE_HEIGHT - MARGIN)], fill=0, width=1)

        # --- Draw right column: Instructions for this page ---
        y_right = col_top
        draw.text((col_right_x, y_right), strings["instructions"], font=font_heading, fill=0)
        y_right += heading_space

        if instr_pages and (repeat_instr or page <= len(instr_pages)):
            instr_src = 0 if repeat_instr else page - 1
            for block_idx in instr_pages[instr_src]:
                block = all_blocks[block_idx]
                if block["type"] == "heading":
                    y_right += 4
                for wline in block["lines"]:
                    if y_right + block["line_h"] > RECIPE_HEIGHT - MARGIN:
                        break
                    draw.text((col_right_x, y_right), wline, font=block["font"], fill=0)
                    y_right += block["line_h"]
                y_right += 6

    # --- Top: button glyphs above the physical reTerminal keys ---
    _draw_button_glyphs(draw, page, total_pages)

    return img, total_pages
