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
- instructions: emit a {"type":"heading"} entry before each section's
  steps (e.g. "Sauce", "Dough", "Garniture"). Plain steps are
  {"type":"step"}.
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
  the magazine name. Null if you can't read any source branding."""


def url_user(url: str, cleaned_text: str) -> str:
    """User message for the URL fallback path.

    `cleaned_text` is the output of `html_extract.extract()` — already
    stripped of chrome, capped at ~30 K chars.
    """
    return (
        f"Extract the recipe from this page.\n"
        f"URL: {url}\n\n"
        f"--- page content ---\n"
        f"{cleaned_text}"
    )


OCR_USER = "Extract the recipe from this image."
