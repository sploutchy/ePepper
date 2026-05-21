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

import logging
import sqlite3
import time

log = logging.getLogger(__name__)


# CHF per 1M tokens for each model (input_rate, output_rate). Numbers
# come from Infomaniak's AI Tools pricing page — refresh manually when
# the catalog changes. Models absent from this dict render as "—" on
# the status page and trigger a one-time warning so the discrepancy is
# visible without crashing.
#
# Each price is keyed by every name we might see for the same model:
# the short Infomaniak slug (e.g. "gemma3n", used by the translate
# default) AND the HuggingFace-style path (e.g.
# "mistralai/Ministral-3-14B-Instruct-2512", used by the URL / OCR
# defaults). The Infomaniak endpoint accepts both, but it echoes
# whichever form the request used back into the `usage.model` field —
# so the lookup has to cover both.
_PRICES_CHF: dict[str, tuple[float, float]] = {
    # Short Infomaniak slugs
    "gemma3n":    (0.20, 0.40),
    "mistral3":   (0.30, 0.40),
    "qwen3":      (0.40, 3.20),
    "llama3":     (0.70, 2.50),
    # HuggingFace-style paths (current and recent defaults)
    "mistralai/Ministral-3-14B-Instruct-2512":          (0.30, 0.40),
    "google/gemma-4-31B-it":                            (0.20, 0.40),
    "Qwen/Qwen3.5-122B-A10B-FP8":                       (0.40, 3.20),
    "mistralai/Mistral-Small-4-119B-2603":              (0.20, 0.75),
    "moonshotai/Kimi-K2.6":                             (0.60, 3.00),
    "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-FP8":        (0.05, 0.20),
    "Apertus-70B-Instruct-2509":                        (0.70, 2.50),
}

_warned_models: set[str] = set()


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
        price = _PRICES_CHF.get(model)
        if price is None:
            if model not in _warned_models:
                log.warning(
                    "LLM model %r has no price in _PRICES_CHF — CHF estimate is partial",
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
