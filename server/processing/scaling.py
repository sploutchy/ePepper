"""Scale a recipe's ingredient quantities to a different serving count.

Shared by the web recipe page (the servings / batch slider) and the panel push,
so the numbers a cook reads on the phone are the numbers the e-ink
display shows.

Only the *leading* quantity of each ingredient line is scaled — that's
the shape every ingestion path produces ("<qty unit ingredient>", see the
LLM prompts) and the shape scraped JSON-LD overwhelmingly arrives in.
Numbers further into the line are deliberately left alone: in
"1 Dose (400 g) Tomaten" the 400 g is the can size, not an amount to
multiply. Lines with no leading number ("Salz, Pfeffer") pass through.
"""

import re
from fractions import Fraction

from processing.recipes import servings_count

# Unicode vulgar fractions DejaVu (the panel font) renders, both ways.
_UNICODE_FRACTIONS = {
    "¼": Fraction(1, 4), "½": Fraction(1, 2), "¾": Fraction(3, 4),
    "⅓": Fraction(1, 3), "⅔": Fraction(2, 3),
    "⅛": Fraction(1, 8), "⅜": Fraction(3, 8), "⅝": Fraction(5, 8), "⅞": Fraction(7, 8),
}
_FRACTION_GLYPHS = {v: k for k, v in _UNICODE_FRACTIONS.items()}
# Fractions a scaled amount may snap to. Eighths are left out on purpose:
# "⅜ TL" is precise-looking noise nobody measures.
_SNAP_TARGETS = [Fraction(1, 4), Fraction(1, 3), Fraction(1, 2), Fraction(2, 3), Fraction(3, 4)]
_SNAP_TOLERANCE = 0.02

_UF = "".join(_UNICODE_FRACTIONS)
# One amount. Alternation order matters: the mixed forms ("1 1/2", "1½")
# must win over the bare integer they start with.
_NUM = (
    rf"\d+\s+\d+/\d+"          # 1 1/2
    rf"|\d+/\d+"               # 1/2
    rf"|\d+\s?[{_UF}]"         # 1½, 1 ½
    rf"|[{_UF}]"               # ½
    rf"|\d+(?:[.,]\d+)?"       # 2, 1.5, 1,5
)
_LEADING_QTY_RE = re.compile(
    rf"^(?P<pre>\s*)(?P<a>{_NUM})(?:(?P<sep>\s*[–-]\s*)(?P<b>{_NUM}))?"
)

# Recipe languages that write decimals with a comma.
_COMMA_LANGS = {"de", "fr", "it"}

MIN_SERVINGS = 1
MAX_SERVINGS = 24

# Batch slider for recipes without a serving count: ×½ … ×4 in halves.
MIN_MULTIPLIER = 0.5
MAX_MULTIPLIER = 4.0
MULTIPLIER_STEP = 0.5


def base_servings(raw) -> int | None:
    """The integer serving count a recipe was written for, or None.

    A range ("4-6") anchors on its lower bound — scaling 4-6 to "8" is
    read as doubling the recipe. None when the servings string carries no
    number (nothing to scale against), so callers hide the slider.
    """
    count = servings_count(raw)
    if not count:
        return None
    n = int(re.match(r"\d+", count).group(0))
    return n if n > 0 else None


def slider_max(base: int) -> int:
    """Upper bound for the servings slider: room to triple a small
    recipe, never below 12, capped so the track stays usable."""
    return min(MAX_SERVINGS, max(12, base * 3))


def _parse(token: str) -> Fraction:
    token = token.strip()
    if "/" in token:
        parts = token.split()
        whole = int(parts[0]) if len(parts) == 2 else 0
        num, den = parts[-1].split("/")
        return whole + Fraction(int(num), int(den)) if int(den) else Fraction(whole)
    if token[-1] in _UNICODE_FRACTIONS:
        whole = token[:-1].strip()
        return (int(whole) if whole else 0) + _UNICODE_FRACTIONS[token[-1]]
    return Fraction(token.replace(",", "."))


def _format(value: Fraction, *, comma: bool, ascii_fractions: bool, decimal: bool) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    whole, rest = divmod(value, 1)
    whole = int(whole)
    # Small amounts read best as kitchen fractions ("½ TL", "1¼ Tassen");
    # once you're past 10 of something a fraction is false precision. A
    # source that wrote decimals ("1,5 dl") keeps writing decimals.
    if value < 10 and not decimal:
        for target in _SNAP_TARGETS:
            if abs(float(rest - target)) <= _SNAP_TOLERANCE:
                if ascii_fractions:
                    frac = f"{target.numerator}/{target.denominator}"
                    return f"{whole} {frac}" if whole else frac
                return f"{whole or ''}{_FRACTION_GLYPHS[target]}"
    if value < 0.1:
        # A pinch scaled way down still has to read as *something*.
        text = f"{float(value):.1g}"
    elif value < 10:
        text = f"{float(value):.1f}".rstrip("0").rstrip(".")
    else:
        text = str(round(value))
    return text.replace(".", ",") if comma else text


def scale_ingredient(text: str, factor: Fraction, lang: str = "en") -> str:
    """Multiply the leading quantity (or range) of one ingredient line."""
    if factor == 1:
        return text
    m = _LEADING_QTY_RE.match(text)
    if not m:
        return text
    a, b = m.group("a"), m.group("b")
    original = a + (b or "")
    # Keep the source's notation: a "1/2" recipe stays ASCII, a "1,5 dl"
    # recipe keeps its decimal comma. Plain integers follow the recipe
    # language for any decimals scaling introduces.
    style = {
        "ascii_fractions": "/" in original,
        "decimal": "," in original or "." in original,
        "comma": "," in original or ("." not in original and lang in _COMMA_LANGS),
    }
    try:
        parts = [_format(_parse(a) * factor, **style)]
        if b:
            parts.append(m.group("sep"))
            parts.append(_format(_parse(b) * factor, **style))
    except (ValueError, ZeroDivisionError):
        return text
    return m.group("pre") + "".join(parts) + text[m.end():]


def scale_recipe(
    recipe: dict, servings: int | None = None, multiplier: float | None = None,
) -> dict:
    """Return `recipe` rescaled, or `recipe` itself when there's nothing
    to do. The input is never mutated.

    Two knobs, one per kind of recipe:

      * `servings` — for recipes whose servings carry a count. The result
        gets a bare numeric `servings`, so every downstream formatter
        ("Serves 8", "8 PORTIONEN") renders the new count.
      * `multiplier` — for recipes *without* a count ("une grande
        poêle", or nothing at all), where there's no serving number to
        aim at but doubling the batch still makes sense. The result keeps
        its `servings` text and gains a `scale` label ("×2") the panel
        shows on its meta line.

    Each knob is ignored on the other kind of recipe, so a stale value
    can never scale a recipe twice or by the wrong yardstick.
    """
    base = base_servings(recipe.get("servings"))
    if base is not None:
        if not servings or servings == base:
            return recipe
        factor = Fraction(servings, base)
    else:
        if not multiplier or multiplier == 1:
            return recipe
        factor = Fraction(multiplier).limit_denominator(4)
    lang = recipe.get("lang") or "en"
    out = dict(recipe)
    out["ingredients"] = [
        scale_ingredient(str(i), factor, lang) for i in recipe.get("ingredients") or [] if i
    ]
    if base is not None:
        out["servings"] = str(servings)
    else:
        out["scale"] = format_multiplier(multiplier)
    return out


def format_multiplier(multiplier: float) -> str:
    """The batch label: "×2", "×1½", "×½". Halves as a glyph, matching
    the ingredient lines' kitchen fractions and the slider's readout."""
    whole, half = divmod(Fraction(multiplier).limit_denominator(2), 1)
    digits = str(int(whole)) if whole or not half else ""
    return f"×{digits}{'½' if half else ''}"


def effective_scale(
    recipe: dict, servings: int | None, multiplier: float | None,
) -> tuple[int | None, float | None]:
    """The (servings, multiplier) pair that actually applies to `recipe`:
    at most one is set, and both are None when the recipe would render as
    written. Lets the display state compare and persist a canonical value
    rather than whatever a caller happened to pass."""
    if scale_recipe(recipe, servings, multiplier) is recipe:
        return None, None
    if base_servings(recipe.get("servings")) is not None:
        return servings, None
    return None, multiplier


def clamp_multiplier(raw) -> float | None:
    """Coerce a form/query value to a batch multiplier on the slider's
    grid (×½ to ×4 in halves), or None."""
    try:
        m = float(raw)
    except (TypeError, ValueError):
        return None
    if not MIN_MULTIPLIER <= m <= MAX_MULTIPLIER or (m * 2) % 1:
        return None
    return m


def clamp_servings(raw) -> int | None:
    """Coerce a form/query value to a usable serving count, or None."""
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    return n if MIN_SERVINGS <= n <= MAX_SERVINGS else None
