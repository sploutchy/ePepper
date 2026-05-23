"""Prompts for the URL → recipe and image → recipe LLM calls.

The model's output target is the **internal** recipe dict that
`process_recipe_url` produces today, NOT schema.org JSON-LD. Skipping
that intermediate format cuts ~25 % off the output token count (no
`@type: HowToStep` wrapper per step) and removes a parsing layer.

Keep the schema description compact — every byte of system prompt is
spent on every call. Move per-site nuance into Python rather than the
prompt.
"""


# Shared schema description. Lists every field the validator in
# `recipes.py:_validate_llm_recipe` accepts; new fields go here and
# there together.
_SCHEMA = """Output schema:
{
  "title": "<recipe name>",
  "total_time": <minutes as integer, or null>,
  "servings": "<e.g. '4 servings', '12 cookies', or null>",
  "ingredients": ["<qty unit ingredient>", ...],
  "instructions": [
    {"type": "step", "text": "<one cooking action>"},
    {"type": "heading", "text": "<section name, e.g. Sauce>"}
  ],
  "lang": "en" | "de" | "fr" | "it",
  "source_name": "<cookbook/magazine/restaurant name, or null>"
}"""

_RULES = """Rules:
- Output ONLY the JSON object — no prose, no markdown fences.
- Never invent values. Omit (null / empty list) if not in the source.
- Preserve ingredient quantities and order exactly as written.
- total_time: total cooking time in minutes (sum prep + cook if both
  given). Integer. Null if not specified.
- lang: detect from the recipe's own text.
- instructions: each step is {"type":"step","text":"…"}; sections
  become {"type":"heading","text":"Sauce"}. Each step text must read
  as a natural cookbook sentence in the recipe's own language.

  Verb form: FR/IT/DE use the infinitive (NOT direct imperative);
  EN keeps the imperative.

  If the source presents ingredients and actions in parallel columns
  or lists (e.g. ingredient names on one side, action verbs on the
  other), reconstruct a natural sentence in the language's correct
  style — do NOT concatenate the raw columns.

  Never re-state ingredient quantities in step text (those live in
  the ingredient list).
- source_name: only populated from image inputs, from visible
  cookbook / magazine branding. Null for webpage inputs."""


URL_SYSTEM = f"""You extract recipes from webpage text into a JSON object.

{_SCHEMA}

{_RULES}
- source_name: null for this URL flow."""


OCR_SYSTEM = f"""You extract recipes from photos of cookbook / magazine pages into a JSON object.

{_SCHEMA}

{_RULES}
- source_name: read the cookbook title from the cover/spine/header, or
  the magazine name. Null if you can't read any source branding.
- If the user message contains a "User context: …" line, treat it as
  ground truth from the recipe owner — typically a cookbook name and /
  or the recipe title (e.g. "Ceviche, Ottolenghi Simple"). Use it to
  fill source_name and / or correct the title; it overrides whatever
  branding you'd otherwise infer from the photo."""


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


# Translation prompt — fed to LLM_TRANSLATE_MODEL (default gemma3n). Job
# is to produce noun-form ingredient names + the recipe title in French
# and German, for indexing into FTS so a user can search a German recipe
# in French and vice versa. Output is intentionally compact (~120 output
# tokens for a typical recipe) so per-recipe cost is well under one
# centime.
TRANSLATE_SYSTEM = """You generate search keywords for a recipe repertoire.

The user gives you a recipe's title and ingredient list in its native
language. You return the same content as NOUN-FORM search keywords in
French and German, suitable for indexing into a full-text search.

Output schema:
{
  "fr": ["<title in French>", "<ingredient noun in French>", ...],
  "de": ["<title in German>", "<ingredient noun in German>", ...]
}

Rules:
- Output ONLY the JSON object — no prose, no markdown fences.
- The first entry of each list is the recipe title translated.
- Subsequent entries are ingredient nouns. Drop quantities and units
  (e.g. "200 g Mehl" → "Mehl" / "farine"). Drop preparation modifiers
  ("hachée fine", "fein gehackt").
- One ingredient → one keyword. If the source ingredient string lists
  several, split them (e.g. "salt and pepper" → "salt", "pepper" in EN
  terms, then translate each).
- Use Swiss orthography in German (Strasse, weiss — no ß).
- If the native language is French, still produce both lists (the
  French list lets you index synonyms / standard cooking nouns even
  when the original ingredients were colloquial). Same the other way.
- Skip non-translatable items (brand names, "à volonté", etc.)."""


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
