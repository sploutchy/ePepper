"""OpenAI-compatible LLM client for the Infomaniak AI Tools API.

Single async entry point `complete_json` that POSTs to
`{LLM_API_URL}/chat/completions` with a Bearer token and parses the
returned JSON object. Used by the URL fallback (cleaned HTML → recipe)
and the OCR path (image → recipe). The same shape works for any
OpenAI-compatible endpoint, so swapping providers is an env-var change.

`LLM_API_URL` is expected to be the OpenAI base, e.g.
`https://api.infomaniak.com/2/ai/<product_id>/openai/v1`.
"""

import asyncio
import base64
import json
import logging
import re
from typing import Any

import aiohttp

from config import LLM_API_KEY, LLM_API_URL

log = logging.getLogger(__name__)


class LLMError(Exception):
    """Raised when the LLM call or its output can't be salvaged."""


_session: aiohttp.ClientSession | None = None


def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


def is_enabled() -> bool:
    return bool(LLM_API_URL and LLM_API_KEY)


async def complete_json(
    *,
    kind: str,
    model: str,
    system: str,
    user: str,
    image_jpeg: bytes | None = None,
    max_tokens: int = 2048,
    timeout_s: float = 60.0,
) -> dict[str, Any]:
    """Call chat-completions and return the JSON object the model produced.

    `kind` is "url" or "ocr" and tags the call in the on-disk ledger
    that backs the status page.

    The system + user prompts are responsible for instructing the model to
    output ONLY a JSON object — we strip a leading/trailing ```json fence
    if it slips through. One retry on malformed JSON with a stricter nudge;
    raises LLMError on a second failure or on transport errors.
    """
    if not is_enabled():
        raise LLMError("LLM not configured (LLM_API_URL / LLM_API_KEY unset)")

    user_content: Any = user
    if image_jpeg is not None:
        # OpenAI-compatible multimodal message: a content array mixing text
        # and image_url parts. Infomaniak / Mistral / Qwen-VL all accept
        # base64 data URLs here.
        b64 = base64.b64encode(image_jpeg).decode("ascii")
        user_content = [
            {"type": "text", "text": user},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
            },
        ]

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]

    raw = await _chat(kind, model, messages, max_tokens, timeout_s)
    try:
        return _parse_json(raw)
    except ValueError as e:
        log.warning("LLM returned non-JSON, retrying once: %s", e)

    messages.append({"role": "assistant", "content": raw})
    messages.append({
        "role": "user",
        "content": (
            "Your previous output wasn't valid JSON. Reply with ONLY the "
            "JSON object, no prose, no markdown fences."
        ),
    })
    raw = await _chat(kind, model, messages, max_tokens, timeout_s)
    try:
        return _parse_json(raw)
    except ValueError as e:
        raise LLMError(f"LLM produced unparseable JSON after retry: {e}") from None


async def _chat(
    kind: str,
    model: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    timeout_s: float,
) -> str:
    """POST to /chat/completions, return the assistant message content.

    Records the call in the on-disk ledger and logs token usage so
    per-call CHF cost is traceable in the container logs (grep `LLM call`).
    """
    url = f"{LLM_API_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.0,
    }
    session = _get_session()
    try:
        async with session.post(
            url,
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=timeout_s),
        ) as resp:
            body = await resp.text()
            if resp.status >= 400:
                raise LLMError(
                    f"LLM HTTP {resp.status} from {url}: {body[:500]}"
                )
            try:
                data = json.loads(body)
            except ValueError as e:
                raise LLMError(f"LLM returned non-JSON envelope: {e}") from None
    except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError) as e:
        raise LLMError(f"LLM transport error: {e}") from None

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError(f"LLM response missing choices/message: {e}") from None

    usage = data.get("usage") or {}
    prompt_tokens = usage.get("prompt_tokens") or 0
    completion_tokens = usage.get("completion_tokens") or 0
    log.info(
        "LLM call: kind=%s model=%s prompt_tokens=%s completion_tokens=%s total=%s",
        kind,
        model,
        prompt_tokens,
        completion_tokens,
        usage.get("total_tokens"),
    )
    # Imported lazily so processing.llm has no import-time dependency on
    # the library package (avoids a cycle if the library ever grows
    # something that imports processing.*).
    from library import record_llm_call
    record_llm_call(
        kind=kind,
        model=model,
        input_tokens=int(prompt_tokens),
        output_tokens=int(completion_tokens),
    )
    return content or ""


_JSON_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)


def _parse_json(raw: str) -> dict[str, Any]:
    """Parse the assistant message, tolerating a single ```json fence."""
    if not raw or not raw.strip():
        raise ValueError("empty content")
    m = _JSON_FENCE_RE.match(raw)
    text = m.group(1) if m else raw.strip()
    obj = json.loads(text)
    if not isinstance(obj, dict):
        raise ValueError(f"expected JSON object, got {type(obj).__name__}")
    return obj
