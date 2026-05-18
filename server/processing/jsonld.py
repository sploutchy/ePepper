"""Convert schema.org Recipe JSON-LD into ePepper's internal recipe dict.

LLM-generated JSON-LD is the supported ingest path when a website isn't
covered by recipe-scrapers or when the source is a photo (OCR via an LLM).
The Telegram bot accepts a .json file; this module locates the first
Recipe object inside it and maps it onto the same shape that
`process_recipe_url` produces.
"""

import logging
import re
from typing import Any

from processing.recipes import _detect_language

log = logging.getLogger(__name__)


def parse_recipe_jsonld(data: Any) -> tuple[dict, str] | None:
    """Find the first schema.org Recipe in `data` and map it to ePepper's shape.

    Accepts a top-level Recipe object, a JSON-LD document with `@graph`,
    a list, or any nested mix.

    Returns `(recipe_dict, source_url)` where `source_url` is the Recipe's
    own `url` field (empty string if absent). Returns None when no Recipe
    is found or when the minimum required fields (title + ingredients OR
    instructions) are missing.
    """
    recipe_node = _find_recipe(data)
    if recipe_node is None:
        log.info("No schema.org Recipe object found in JSON-LD")
        return None

    title = _str(recipe_node.get("name"))
    ingredients = _strings(recipe_node.get("recipeIngredient"))
    instructions = _flatten_instructions(recipe_node.get("recipeInstructions"))
    total_time = _parse_duration(recipe_node.get("totalTime"))
    servings = _str(recipe_node.get("recipeYield"))
    lang = _parse_lang(recipe_node.get("inLanguage")) or _guess_lang(instructions)
    source_url = _str(recipe_node.get("url"))

    if not title or (not ingredients and not instructions):
        log.warning(
            "JSON-LD Recipe missing required fields: title=%r ingredients=%d instructions=%d",
            title, len(ingredients), len(instructions),
        )
        return None

    recipe = {
        "title": title,
        "total_time": total_time,
        "servings": servings,
        "ingredients": ingredients,
        "instructions": instructions,
        "lang": lang,
    }
    return recipe, source_url


def _is_recipe(node: Any) -> bool:
    if not isinstance(node, dict):
        return False
    types = node.get("@type")
    if isinstance(types, str):
        return types == "Recipe"
    if isinstance(types, list):
        return "Recipe" in types
    return False


def _find_recipe(node: Any) -> dict | None:
    """DFS for the first Recipe-typed object. `@graph` is checked first."""
    if isinstance(node, dict):
        if _is_recipe(node):
            return node
        graph = node.get("@graph")
        if graph is not None:
            found = _find_recipe(graph)
            if found is not None:
                return found
        for v in node.values():
            found = _find_recipe(v)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_recipe(item)
            if found is not None:
                return found
    return None


def _str(value: Any) -> str:
    """Best-effort coercion to a single trimmed string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        for item in value:
            s = _str(item)
            if s:
                return s
        return ""
    if isinstance(value, dict):
        for key in ("@value", "name", "text"):
            if key in value:
                return _str(value[key])
        return ""
    return str(value).strip()


def _strings(value: Any) -> list[str]:
    """Coerce to a clean list of non-empty strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [s.strip() for s in value.split("\n") if s.strip()]
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            s = _str(item)
            if s:
                out.append(s)
        return out
    s = _str(value)
    return [s] if s else []


_ISO_DURATION_RE = re.compile(
    r"^P"
    r"(?:(?P<days>\d+)D)?"
    r"(?:T"
    r"(?:(?P<hours>\d+)H)?"
    r"(?:(?P<minutes>\d+)M)?"
    r"(?:(?P<seconds>\d+)S)?"
    r")?$"
)


def _parse_duration(value: Any) -> int | None:
    """Parse an ISO 8601 duration (e.g. PT1H30M) to minutes. None if unparseable."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value) if value > 0 else None
    s = _str(value).upper()
    if not s:
        return None
    m = _ISO_DURATION_RE.match(s)
    if not m:
        return None
    days = int(m.group("days") or 0)
    hours = int(m.group("hours") or 0)
    minutes = int(m.group("minutes") or 0)
    seconds = int(m.group("seconds") or 0)
    total = days * 24 * 60 + hours * 60 + minutes + (seconds // 60)
    return total or None


def _parse_lang(value: Any) -> str:
    """Extract primary subtag from an `inLanguage` value (e.g. 'de-CH' → 'de').

    Accepts a string ('de'), a BCP-47 tag ('de-CH'), or a schema.org Language
    object (where `alternateName` holds the BCP-47 code).
    """
    if isinstance(value, dict):
        s = _str(value.get("alternateName")) or _str(value.get("@value"))
    else:
        s = _str(value)
    s = s.lower()
    if not s:
        return ""
    primary = s.split("-")[0]
    if primary in ("de", "fr", "it", "en", "es", "nl", "pt"):
        return primary
    return ""


def _flatten_instructions(value: Any) -> list[dict]:
    """Convert recipeInstructions to ePepper's [{type, text}, ...] format.

    Accepts: a single string (newline-split), a HowToStep/HowToSection dict,
    or a list of strings / HowToStep / HowToSection. HowToSection.name
    becomes a {type:'heading'} entry followed by its itemListElement steps.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [
            {"type": "step", "text": line.strip()}
            for line in value.split("\n")
            if line.strip()
        ]
    if isinstance(value, dict):
        return _flatten_instructions([value])
    if isinstance(value, list):
        out: list[dict] = []
        for item in value:
            if isinstance(item, str):
                txt = item.strip()
                if txt:
                    out.append({"type": "step", "text": txt})
                continue
            if not isinstance(item, dict):
                continue
            item_type = item.get("@type")
            is_section = (
                item_type == "HowToSection"
                or (isinstance(item_type, list) and "HowToSection" in item_type)
            )
            if is_section:
                name = _str(item.get("name"))
                if name:
                    out.append({"type": "heading", "text": name})
                out.extend(_flatten_instructions(item.get("itemListElement")))
            else:
                txt = _str(item.get("text")) or _str(item.get("name"))
                if txt:
                    out.append({"type": "step", "text": txt})
        return out
    return []


def _guess_lang(instructions: list[dict]) -> str:
    """Fall back to recipe-scrapers' content-based language detection."""
    text = "\n".join(i["text"] for i in instructions)
    return _detect_language("", text, "")
