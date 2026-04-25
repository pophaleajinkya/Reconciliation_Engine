"""
Thin LLM wrapper (OpenAI-compatible chat completions).

Despite the legacy module name, this client now speaks to **any**
OpenAI-compatible endpoint — OpenRouter by default, Groq as an alternative.
The provider is selected via `SETTINGS.provider` (see `config.py`).

Public surface stays identical so every caller (`extract_json_from_text`,
`extract_json_from_image`, `reason_over_evidence`) continues to work
unchanged.

Design notes
------------
- We use `httpx` directly instead of pulling in the `groq` or `openai` SDKs
  because both endpoints accept the exact same chat-completions JSON body.
- JSON-mode is requested via `response_format={"type": "json_object"}`;
  when a provider ignores it (OpenRouter sometimes does for older models),
  we still parse the response defensively.
- Retries use exponential backoff on transient network errors (rate limits,
  5xx, timeouts) but NEVER retry 4xx auth errors.
- `LLMUnavailable` is raised when no API key is configured; callers catch
  it and return an empty extraction so the pipeline degrades gracefully.
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from reconcile.config import SETTINGS

log = logging.getLogger("reconcile.llm")


class LLMUnavailable(RuntimeError):
    """Raised when an LLM call is required but no API key is configured."""


class _RetryableHTTPError(RuntimeError):
    """Wrapper for HTTP errors that are worth retrying (5xx, 429, timeouts)."""


def _headers() -> dict[str, str]:
    h = {
        "Authorization": f"Bearer {SETTINGS.api_key}",
        "Content-Type": "application/json",
    }
    # OpenRouter asks clients to identify themselves (purely informational —
    # improves their analytics and unlocks some leaderboards). Omit gracefully
    # if the user hasn't configured it.
    if SETTINGS.provider == "openrouter":
        referer = "https://github.com/reconciliation-engine"
        title = "Reconciliation Engine"
        h["HTTP-Referer"] = referer
        h["X-Title"] = title
    return h


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    # Only retry transient errors. Auth errors (401/403) fail fast.
    retry=retry_if_exception_type((_RetryableHTTPError, httpx.TimeoutException, httpx.NetworkError)),
)
def _chat_json(messages: list[dict[str, Any]], model: str, temperature: float = 0.0) -> dict[str, Any]:
    if not SETTINGS.has_llm:
        raise LLMUnavailable(
            f"No API key configured for {SETTINGS.provider_display}. "
            f"Set OPENROUTER_API_KEY (or GROQ_API_KEY) in app/.env."
        )

    url = f"{SETTINGS.base_url.rstrip('/')}/chat/completions"
    body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
        "max_tokens": 4096,
    }

    try:
        resp = httpx.post(url, headers=_headers(), json=body, timeout=120.0)
    except (httpx.TimeoutException, httpx.NetworkError) as e:
        raise _RetryableHTTPError(f"network error: {e}") from e

    if resp.status_code >= 400:
        # Retry on 429 (rate limit) and 5xx; hard-fail on 4xx auth/validation.
        if resp.status_code == 429 or resp.status_code >= 500:
            raise _RetryableHTTPError(
                f"{SETTINGS.provider_display} returned {resp.status_code}: {resp.text[:300]}"
            )
        # Non-retryable (401, 403, 404, 400 bad model id, etc).
        raise RuntimeError(
            f"{SETTINGS.provider_display} rejected request ({resp.status_code}): {resp.text[:500]}"
        )

    try:
        payload = resp.json()
    except ValueError as e:
        raise _RetryableHTTPError(f"non-JSON body from {SETTINGS.provider_display}: {e}") from e

    # OpenRouter/Groq both return choices[0].message.content.
    try:
        content = payload["choices"][0]["message"]["content"] or "{}"
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Unexpected {SETTINGS.provider_display} response shape: {payload}") from e

    return _parse_json_loose(content)


def _parse_json_loose(content: str) -> dict[str, Any]:
    """
    Robust JSON parse tolerant of common LLM output quirks:

      1. Exact JSON — the happy path, `json.loads(content)` succeeds.
      2. Fenced JSON — content is wrapped in ```json ... ``` or ``` ... ```.
      3. Bare object body — model emitted key-value pairs without the
         outer `{...}`. OpenRouter's Llama-4-Scout does this sometimes
         when response_format is requested but not strictly enforced.
      4. Object embedded in prose — e.g. `"Here is the JSON: { ... }"`.
         We extract the first `{...}` span.

    Falls back to `{"_raw": content}` only when nothing else works so
    callers can still log the raw output without crashing.
    """
    if not content:
        return {}

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    # (2) Strip code fences.
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].lstrip()
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass

    # (3) Bare object body — wrap in braces and try.
    if ":" in stripped and not stripped.startswith("{"):
        wrapped = "{" + stripped.rstrip(", \n\r\t") + "}"
        try:
            return json.loads(wrapped)
        except json.JSONDecodeError:
            pass

    # (4) Embedded object — pull out the first balanced {...}.
    first = stripped.find("{")
    last = stripped.rfind("}")
    if first != -1 and last > first:
        candidate = stripped[first:last + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    log.warning("LLM returned non-JSON content (first 200 chars): %s", content[:200])
    return {"_raw": content}


def extract_json_from_text(
    *,
    system_prompt: str,
    user_text: str,
    schema_hint: str,
    model: str | None = None,
) -> dict[str, Any]:
    """Call a text model to produce a JSON object that conforms to `schema_hint`."""
    model = model or SETTINGS.text_model
    messages = [
        {
            "role": "system",
            "content": (
                f"{system_prompt}\n\n"
                "You must respond with a single JSON object matching this schema:\n"
                f"{schema_hint}\n\n"
                "Rules:\n"
                "- Use null for any field you cannot extract confidently.\n"
                "- Never invent values. Prefer null over guessing.\n"
                "- Keep list items aligned 1:1 with what the document shows.\n"
            ),
        },
        {"role": "user", "content": user_text},
    ]
    try:
        return _chat_json(messages, model=model)
    except LLMUnavailable:
        log.warning("LLM unavailable; returning empty extraction payload.")
        return {}
    except Exception as e:
        log.warning("LLM text extraction failed: %s", e)
        return {"_error": str(e)}


def _encode_image(path: Path) -> str:
    data = path.read_bytes()
    return base64.b64encode(data).decode("ascii")


def extract_json_from_image(
    *,
    system_prompt: str,
    schema_hint: str,
    image_paths: list[Path],
    user_hint: str = "",
    model: str | None = None,
) -> dict[str, Any]:
    """Call a multimodal model over one or more page images; return JSON."""
    model = model or SETTINGS.vision_model
    image_contents: list[dict[str, Any]] = []
    for p in image_paths:
        b64 = _encode_image(p)
        image_contents.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            }
        )

    full_system = (
        f"{system_prompt}\n\n"
        "Respond with a single JSON object matching this schema:\n"
        f"{schema_hint}\n\n"
        "Rules: use null for unknown fields; never invent values."
    )

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": full_system + "\n\n" + user_hint},
                *image_contents,
            ],
        }
    ]
    try:
        return _chat_json(messages, model=model)
    except LLMUnavailable:
        log.warning("LLM unavailable; returning empty vision extraction payload.")
        return {}
    except Exception as e:
        log.warning("LLM vision extraction failed: %s", e)
        return {"_error": str(e)}


def reason_over_evidence(
    *,
    system_prompt: str,
    user_text: str,
    schema_hint: str,
    model: str | None = None,
) -> dict[str, Any]:
    """Structured reasoning call used by the decision layer for narrative output."""
    return extract_json_from_text(
        system_prompt=system_prompt,
        user_text=user_text,
        schema_hint=schema_hint,
        model=model,
    )
