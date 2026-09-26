"""Shared LLM helper (PRD §14 rule 5). Role B owns it; Roles C/D import it — don't copy it.

    from app.llm import complete_json, complete_text, llm_available

- Model comes from REIGNS_MODEL.
- JSON calls: temperature 0 by default, JSON-only instruction, code fences stripped,
  one retry on parse failure. Raises LLMError on failure — callers catch it (detectors
  must turn it into status "error").
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

log = logging.getLogger("reigns.llm")

DEFAULT_MODEL = "claude-sonnet-5"
_client = None


class LLMError(RuntimeError):
    pass


def llm_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def model_name() -> str:
    return os.environ.get("REIGNS_MODEL") or DEFAULT_MODEL


def _get_client():
    global _client
    if _client is None:
        if not llm_available():
            raise LLMError("ANTHROPIC_API_KEY not set")
        from anthropic import AsyncAnthropic

        _client = AsyncAnthropic()
    return _client


async def complete_text(
    system: str, user: str, temperature: float = 0.0, max_tokens: int = 1024
) -> str:
    try:
        resp = await _get_client().messages.create(
            model=model_name(),
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except LLMError:
        raise
    except Exception as exc:  # network, rate limit, auth …
        raise LLMError(f"LLM call failed: {exc}") from exc
    return "".join(getattr(b, "text", "") for b in resp.content)


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_json(text: str) -> Any:
    """Defensive JSON parse: strip fences, fall back to the first {...} or [...] block."""
    cleaned = _FENCE.sub("", text.strip()).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"(\{.*\}|\[.*\])", cleaned, re.DOTALL)
        if match:
            return json.loads(match.group(1))
        raise


async def complete_json(
    system: str, user: str, temperature: float = 0.0, max_tokens: int = 2048
) -> Any:
    system = system.rstrip() + "\n\nRespond with JSON only. No prose, no code fences."
    last_err: Exception | None = None
    for attempt in range(2):  # retry once on parse failure
        text = await complete_text(system, user, temperature, max_tokens)
        try:
            return parse_json(text)
        except (json.JSONDecodeError, ValueError) as exc:
            last_err = exc
            log.warning("llm json parse failed (attempt %d): %s", attempt + 1, exc)
    raise LLMError(f"could not parse JSON from model: {last_err}")
