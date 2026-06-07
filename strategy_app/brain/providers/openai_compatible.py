"""Minimal, dependency-free client for OpenAI-compatible chat endpoints.

Works with any provider that exposes ``POST {base_url}/chat/completions`` with
Bearer auth and the standard request/response shape — Groq, Google Gemini
(OpenAI-compat), DeepSeek, xAI Grok, OpenRouter, Mistral, ...  Swapping
provider is a config change (base_url + key + model), never a code change.

Uses only the standard library (``urllib``) so it adds **no** runtime
dependency to ``strategy_app``.  Transport / HTTP / shape failures raise
:class:`LLMClientError`; the brain layer catches and degrades to no-op.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)


class LLMClientError(RuntimeError):
    """Raised when the chat endpoint cannot be reached or returns a bad response."""


def chat_completion(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    timeout_s: float = 20.0,
    max_tokens: int = 512,
    temperature: float = 0.2,
    json_mode: bool = True,
) -> str:
    """POST a chat completion and return the assistant message content string.

    Args:
        base_url:    OpenAI-compatible base, e.g. ``https://api.groq.com/openai/v1``.
        api_key:     Provider key, sent as ``Authorization: Bearer``.
        model:       Provider-specific model slug.
        messages:    Standard ``[{"role": ..., "content": ...}]`` list.
        timeout_s:   Hard wall-clock cap on the request.
        max_tokens:  Output cap.
        temperature: Sampling temperature.
        json_mode:   When True, request ``response_format={"type":"json_object"}``.
                     Disable for providers that reject it.

    Returns:
        The ``choices[0].message.content`` string.

    Raises:
        LLMClientError: on any transport, HTTP, or response-shape failure.
    """
    url = base_url.rstrip("/") + "/chat/completions"
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:500]
        except Exception:
            pass
        raise LLMClientError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LLMClientError(f"transport error to {url}: {exc}") from exc

    try:
        data = json.loads(raw)
        content = data["choices"][0]["message"]["content"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise LLMClientError(f"unexpected response shape from {url}: {exc}") from exc

    if not isinstance(content, str):
        raise LLMClientError(f"non-string content from {url}: {type(content)!r}")
    return content


def extract_json_object(content: str) -> dict[str, Any]:
    """Best-effort parse of a single JSON object from a model response.

    Tolerates markdown code fences and surrounding prose by falling back to the
    outermost ``{...}`` slice.  Raises :class:`LLMClientError` if no object is
    found — small models occasionally ignore the JSON instruction.
    """
    text = (content or "").strip()

    # 1. Direct parse (the happy path when json_mode works).
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    # 2. Content fenced in ```json ... ``` blocks.
    if "```" in text:
        for chunk in text.split("```"):
            candidate = chunk.strip()
            if candidate.lower().startswith("json"):
                candidate = candidate[4:].strip()
            try:
                obj = json.loads(candidate)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue

    # 3. Outermost brace slice (prose before/after the object).
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    raise LLMClientError("no JSON object found in model response")


__all__ = ["chat_completion", "extract_json_object", "LLMClientError"]
