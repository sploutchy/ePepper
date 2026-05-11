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
    "de": {"ingredients": "Zutaten", "instructions": "Zubereitung", "page": "Seite", "servings": "Portionen"},
    "fr": {"ingredients": "Ingrédients", "instructions": "Préparation", "page": "page", "servings": "portions"},
    "it": {"ingredients": "Ingredienti", "instructions": "Preparazione", "page": "pagina", "servings": "porzioni"},
    "es": {"ingredients": "Ingredientes", "instructions": "Preparación", "page": "página", "servings": "porciones"},
    "nl": {"ingredients": "Ingrediënten", "instructions": "Bereiding", "page": "pagina", "servings": "porties"},
    "en": {"ingredients": "Ingredients", "instructions": "Instructions", "page": "page", "servings": "servings"},
}


def render_recipe(recipe: dict, page: int = 1) -> tuple[Image.Image, int]:
    """Render a recipe dict to a 1-bit image for e-ink display.

    Args:
        recipe: dict with title, total_time, servings, ingredients, instructions
        page: 1-based page number

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

    img = Image.new("1", (DISPLAY_WIDTH, RECIPE_HEIGHT), 1)  # 1 = white
    draw = ImageDraw.Draw(img)

    y = MARGIN

    # --- Title ---
    title = recipe.get("title", "Untitled Recipe")
    title_lines = textwrap.wrap(title, width=40)
    for line in title_lines:
        draw.text((MARGIN, y), line, font=font_title, fill=0)
        y += font_title.size + 4
    y += 4

    # --- Meta line (time + servings) ---
    meta_parts = []
    if recipe.get("total_time"):
        meta_parts.append(f"{recipe['total_time']} min")
    if recipe.get("servings"):
        servings_raw = str(recipe["servings"])
        # Extract just the number, replace the label with the localized version
        servings_num = re.sub(r"[^\d]", "", servings_raw)
        if servings_num:
            meta_parts.append(f"{servings_num} {strings['servings']}")
        else:
            meta_parts.append(servings_raw)
    if meta_parts:
        meta_text = "  ·  ".join(meta_parts)
        draw.text((MARGIN, y), meta_text, font=font_meta, fill=0)
        y += font_meta.size + 6

    # --- Divider ---
    y += 4
    draw.line([(MARGIN, y), (DISPLAY_WIDTH - MARGIN, y)], fill=0, width=1)
    y += 10

    # --- Two-column layout ---
    col_left_w = int((DISPLAY_WIDTH - 2 * MARGIN - COLUMN_GAP) * INGREDIENTS_WIDTH_RATIO)
    col_right_x = MARGIN + col_left_w + COLUMN_GAP
    col_right_w = DISPLAY_WIDTH - col_right_x - MARGIN
    col_top = y

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

    # Total pages = whichever column needs more
    total_pages = max(len(ingr_pages), len(instr_pages), 1)
    page = max(1, min(page, total_pages))

    # --- Draw left column: Ingredients for this page ---
    y_left = col_top
    draw.text((MARGIN, y_left), strings["ingredients"], font=font_heading, fill=0)
    y_left += heading_space

    if page <= len(ingr_pages):
        for grp_idx in ingr_pages[page - 1]:
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

    if page <= len(instr_pages):
        for block_idx in instr_pages[page - 1]:
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
