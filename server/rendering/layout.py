"""Render structured recipe data to a beautiful 800x430 B/W image."""

import re
import textwrap

from PIL import Image, ImageDraw, ImageFont

from config import (
    DISPLAY_WIDTH,
    RECIPE_HEIGHT,
    MARGIN,
    COLUMN_GAP,
    INGREDIENTS_WIDTH_RATIO,
    FONT_REGULAR,
    FONT_BOLD,
)

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
    if meta_parts:
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
    footer_reserve = 20
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

    # Paginate instructions
    instr_pages: list[list[int]] = []
    current_page = []
    used_h = 0
    for idx, block in enumerate(all_blocks):
        block_h = len(block["lines"]) * block["line_h"] + 6
        if block["type"] == "heading":
            block_h += 4
        # Widow control: a heading must not end a page alone. Reserve room
        # for the next block's first line so we page-break before the heading
        # rather than orphaning it from its content.
        required_h = block_h
        if block["type"] == "heading" and idx + 1 < len(all_blocks):
            required_h += all_blocks[idx + 1]["line_h"]
        if used_h + required_h > available_h and current_page:
            instr_pages.append(current_page)
            current_page = [idx]
            used_h = block_h
        else:
            current_page.append(idx)
            used_h += block_h
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

    if is_notes_page:
        # --- Notes page ---
        notes_idx = page - base_pages - 1
        y = MARGIN
        draw.text((MARGIN, y), strings["notes"], font=font_title, fill=0)
        y += font_title.size + 4 + 4
        draw.line([(MARGIN, y), (DISPLAY_WIDTH - MARGIN, y)], fill=0, width=1)
        y += 10

        for i, para_lines in enumerate(notes_pages[notes_idx]):
            if i > 0:
                y += line_h  # blank line between comments
            for wline in para_lines:
                if y + line_h > RECIPE_HEIGHT - MARGIN - footer_reserve:
                    break
                draw.text((MARGIN, y), wline, font=font_body, fill=0)
                y += line_h
    else:
        # --- Recipe page: draw title, meta, divider, then two columns ---
        y = MARGIN
        for line in title_lines:
            draw.text((MARGIN, y), line, font=font_title, fill=0)
            y += font_title.size + 4
        y += 4

        if meta_parts:
            meta_text = "  ·  ".join(meta_parts)
            draw.text((MARGIN, y), meta_text, font=font_meta, fill=0)
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
                    if y_left + line_h > RECIPE_HEIGHT - MARGIN - footer_reserve:
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
                    if y_right + block["line_h"] > RECIPE_HEIGHT - MARGIN - footer_reserve:
                        break
                    draw.text((col_right_x, y_right), wline, font=block["font"], fill=0)
                    y_right += block["line_h"]
                y_right += 6

    # --- Footer: page indicator ---
    if total_pages > 1:
        page_text = f"{strings['page']} {page} / {total_pages}"
        bbox = draw.textbbox((0, 0), page_text, font=font_meta)
        tw = bbox[2] - bbox[0]
        draw.text((DISPLAY_WIDTH - MARGIN - tw, RECIPE_HEIGHT - MARGIN - 2), page_text, font=font_meta, fill=0)

    return img, total_pages
