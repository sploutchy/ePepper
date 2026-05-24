"""Per-call LLM token + cost ledger.

A 5-column table next to the recipe library:

    llm_calls(ts, kind, model, input_tokens, output_tokens)

`kind` is "url" (recipe-scrapers fallback) or "ocr" (image extraction)
so the status page can split the two surfaces. Failed LLM calls are not
recorded — nothing was billed.

The status page reads `month_stats()` to render the headline LLM line.
The pricing table is the per-million-token CHF rates Infomaniak charges
on the AI Tools catalog; keep it in sync when the catalog moves.
"""

import json
import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger(__name__)


# CHF per 1M tokens for each model (input_rate, output_rate). Numbers
# come from Infomaniak's AI Tools pricing page — refresh manually when
# the catalog changes by editing `server/data/llm_prices.json`. Models
# absent from that file render as "—" on the status page and trigger a
# one-time warning so the discrepancy is visible without crashing.
#
# Each price is keyed by every name we might see for the same model:
# the short Infomaniak slug (e.g. "gemma3n", used by the translate
# default) AND the HuggingFace-style path (e.g.
# "mistralai/Ministral-3-14B-Instruct-2512", used by the URL / OCR
# defaults). The Infomaniak endpoint accepts both, but it echoes
# whichever form the request used back into the `usage.model` field —
# so the lookup has to cover both.
_PRICES_PATH = Path(__file__).parent.parent / "data" / "llm_prices.json"

# Lazily populated cache. None until first `_prices()` call, then the
# loaded dict — empty dict counts as "loaded and empty", not "reload".
_prices_cache: dict[str, tuple[float, float]] | None = None

_warned_models: set[str] = set()


def _prices() -> dict[str, tuple[float, float]]:
    """Return the model → (in_rate, out_rate) price table, loading once.

    A missing or malformed JSON file logs and degrades to an empty
    table — the per-call warning path below already handles "model not
    in table" gracefully (CHF estimate flagged partial, no crash).
    """
    global _prices_cache
    if _prices_cache is not None:
        return _prices_cache
    try:
        raw = json.loads(_PRICES_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.warning("LLM price table not found at %s — CHF estimates disabled", _PRICES_PATH)
        _prices_cache = {}
        return _prices_cache
    except (OSError, json.JSONDecodeError):
        log.exception("Failed to load LLM price table from %s", _PRICES_PATH)
        _prices_cache = {}
        return _prices_cache
    table: dict[str, tuple[float, float]] = {}
    for model, rates in raw.items():
        if isinstance(rates, (list, tuple)) and len(rates) == 2:
            table[model] = (float(rates[0]), float(rates[1]))
    _prices_cache = table
    return _prices_cache


def record(
    conn_factory,
    *,
    kind: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> None:
    """Append one row. `conn_factory` is a zero-arg connection opener.

    Failure to record is logged and swallowed — it must never break the
    user-facing path (we already got the recipe; losing one accounting
    row isn't worth surfacing).
    """
    try:
        with conn_factory() as conn:
            conn.execute(
                "INSERT INTO llm_calls(ts, kind, model, input_tokens, output_tokens) "
                "VALUES (?, ?, ?, ?, ?)",
                (int(time.time()), kind, model, int(input_tokens), int(output_tokens)),
            )
    except sqlite3.Error:
        log.exception("Failed to record LLM call")


def month_stats(conn_factory, since_ts: int) -> dict:
    """Aggregate calls + tokens + estimated CHF since `since_ts`.

    Returns a dict with the headline fields the status page needs.
    When the table is empty for the window, returns zeros — the
    template branches on `calls == 0` for the "no calls yet" copy.
    """
    try:
        with conn_factory() as conn:
            rows = conn.execute(
                "SELECT kind, model, input_tokens, output_tokens "
                "FROM llm_calls WHERE ts >= ?",
                (since_ts,),
            ).fetchall()
    except sqlite3.Error:
        log.exception("Failed to read llm_calls")
        rows = []

    calls = len(rows)
    url_calls = 0
    ocr_calls = 0
    translate_calls = 0
    chf = 0.0
    chf_known = True
    for row in rows:
        kind, model, in_tok, out_tok = (
            row["kind"], row["model"], row["input_tokens"], row["output_tokens"]
        )
        if kind == "url":
            url_calls += 1
        elif kind == "ocr":
            ocr_calls += 1
        elif kind == "translate":
            translate_calls += 1
        price = _prices().get(model)
        if price is None:
            if model not in _warned_models:
                log.warning(
                    "LLM model %r has no price in llm_prices.json — CHF estimate is partial",
                    model,
                )
                _warned_models.add(model)
            chf_known = False
            continue
        in_rate, out_rate = price
        chf += (in_tok / 1_000_000) * in_rate + (out_tok / 1_000_000) * out_rate

    return {
        "calls": calls,
        "url_calls": url_calls,
        "ocr_calls": ocr_calls,
        "translate_calls": translate_calls,
        "chf": chf,
        # When at least one row used a model we don't have pricing for,
        # the CHF total is a lower bound, not the exact figure.
        "chf_partial": calls > 0 and not chf_known,
    }
