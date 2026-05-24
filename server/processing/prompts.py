"""Prompts for the URL → recipe and image → recipe LLM calls.

The model's output target is the **internal** recipe dict that
`process_recipe_url` produces today, NOT schema.org JSON-LD. Skipping
that intermediate format cuts ~25 % off the output token count (no
`@type: HowToStep` wrapper per step) and removes a parsing layer.

System prompts live as plain .txt files under `server/data/prompts/`
and are loaded lazily on first attribute access. Editing a prompt no
longer requires a code change — drop a new revision into the data
directory and restart. The user-message builders (which interpolate
runtime values) stay in code.
"""

from pathlib import Path


# Resolved at import time so the lookup is one syscall, not a string
# build per access. `__file__` is server/processing/prompts.py, so two
# `parent` hops land at server/, then into data/prompts/.
_PROMPTS_DIR = Path(__file__).parent.parent / "data" / "prompts"

# File-content cache. Populated lazily by `_load` on first access for a
# given filename, then reused for the lifetime of the process.
_cache: dict[str, str] = {}


def _load(name: str) -> str:
    """Read `<name>.txt` from the prompts dir, cached after first hit.

    Raises FileNotFoundError with the full expected path so a typo in
    a prompt filename surfaces a clear error instead of an empty string
    silently shipping to the LLM.
    """
    if name in _cache:
        return _cache[name]
    path = _PROMPTS_DIR / f"{name}.txt"
    if not path.is_file():
        raise FileNotFoundError(
            f"Prompt file not found: {path} "
            f"(expected one .txt per prompt under server/data/prompts/)"
        )
    text = path.read_text(encoding="utf-8").strip()
    _cache[name] = text
    return text


# Module-level attribute access (e.g. `from processing.prompts import
# URL_SYSTEM`) goes through `__getattr__`, which delegates to `_load`.
# That preserves the existing public API — callers in
# `processing/recipes.py` import the same names and get strings back —
# while keeping the file read off the import path.
_PROMPT_FILES = {
    "URL_SYSTEM": "url_system",
    "OCR_SYSTEM": "ocr_system",
    "TRANSLATE_SYSTEM": "translate_system",
}


def __getattr__(name: str) -> str:
    file_stem = _PROMPT_FILES.get(name)
    if file_stem is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return _load(file_stem)


def url_user(url: str, cleaned_text: str) -> str:
    """User message for the URL fallback path.

    `cleaned_text` is the output of `html_extract.to_text()` — already
    stripped of chrome, capped at ~30 K chars.
    """
    return (
        f"Extract the recipe from this page.\n"
        f"URL: {url}\n\n"
        f"--- page content ---\n"
        f"{cleaned_text}"
    )


def ocr_user(hint: str | None) -> str:
    """User message for OCR. Folds in an optional sender-supplied hint.

    The hint comes from one of two places:
      - Telegram: the caption typed alongside the photo
      - Web: a cleaned form of the upload's filename
    Either way the LLM treats it as ground truth that overrides whatever
    branding it does (or doesn't) read from the photo itself — the user
    almost always knows the source better than the OCR can read it.
    """
    base = "Extract the recipe from this image."
    cleaned = (hint or "").strip()
    if not cleaned:
        return base
    return f"{base}\nUser context: {cleaned}"


def translate_user(title: str, ingredients: list[str], native_lang: str) -> str:
    """User message for the translation pass.

    Lang is informational only — the LLM should detect it from the text,
    but passing it explicitly helps small models stay on track.
    """
    joined = "\n".join(f"- {ing}" for ing in ingredients)
    return (
        f"Native language: {native_lang}\n"
        f"Title: {title}\n"
        f"Ingredients:\n{joined}"
    )
