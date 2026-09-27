"""Shared LLM helper (PRD §14 rule 5). Role B owns it; Roles C/D import it — don't copy it.

    from app.llm import complete_json, complete_text, llm_available

Provider: REIGNS_LLM_PROVIDER = "anthropic" (default) or "gemini".
- anthropic: ANTHROPIC_API_KEY; models REIGNS_MODEL / REIGNS_FAST_MODEL.
- gemini:    GEMINI_API_KEY (or GOOGLE_API_KEY); models REIGNS_GEMINI_MODEL / REIGNS_GEMINI_FAST_MODEL.
Fallback (REIGNS_LLM_FALLBACK=1, default): if the chosen provider fails and the other one has a
key, the call is retried there once, so a bad key or rate limit never silences the pet.

- JSON calls: temperature 0 by default, JSON-only instruction, code fences stripped,
  one retry on parse failure. Raises LLMError on failure — callers catch it (detectors
  must turn it into status "error").
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional

import httpx

log = logging.getLogger("reigns.llm")

PROVIDERS = ("anthropic", "gemini")
DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_FAST_MODEL = "claude-haiku-4-5"
DEFAULT_GEMINI_MODEL = "gemini-2.5-pro"
DEFAULT_GEMINI_FAST_MODEL = "gemini-2.5-flash"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_TIMEOUT_S = 30.0
_client = None
_transport: Optional[httpx.AsyncBaseTransport] = None  # tests inject a MockTransport


class LLMError(RuntimeError):
    pass


# ---------------------------------------------------------------------------- configuration
def provider() -> str:
    v = (os.environ.get("REIGNS_LLM_PROVIDER") or "anthropic").strip().lower()
    return v if v in PROVIDERS else "anthropic"


def _other(p: str) -> str:
    return "gemini" if p == "anthropic" else "anthropic"


def _gemini_key() -> str:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""


def _has_key(p: str) -> bool:
    return bool(_gemini_key()) if p == "gemini" else bool(os.environ.get("ANTHROPIC_API_KEY"))


def fallback_enabled() -> bool:
    return (os.environ.get("REIGNS_LLM_FALLBACK") or "1").strip().lower() not in ("0", "false", "no")


def llm_available() -> bool:
    p = provider()
    return _has_key(p) or (fallback_enabled() and _has_key(_other(p)))


def _main_model(p: str) -> str:
    if p == "gemini":
        return os.environ.get("REIGNS_GEMINI_MODEL") or DEFAULT_GEMINI_MODEL
    return os.environ.get("REIGNS_MODEL") or DEFAULT_MODEL


def _fast_model(p: str) -> str:
    if p == "gemini":
        return os.environ.get("REIGNS_GEMINI_FAST_MODEL") or DEFAULT_GEMINI_FAST_MODEL
    return os.environ.get("REIGNS_FAST_MODEL", DEFAULT_FAST_MODEL) or _main_model(p)


def model_name() -> str:
    return _main_model(provider())


def fast_model_name() -> str:
    """Smaller, faster model for simple high-volume steps like claim extraction (FR-B3, G1
    latency). Anthropic: falls back to the main model if REIGNS_FAST_MODEL is set to ""."""
    return _fast_model(provider())


def _is_fast(model: Optional[str]) -> bool:
    if not model:
        return False
    m = model.lower()
    return model in (_fast_model("anthropic"), _fast_model("gemini")) or any(
        k in m for k in ("haiku", "flash")
    )


def _model_for(p: str, requested: Optional[str]) -> str:
    """The requested model if it belongs to provider p, else p's fast/main equivalent."""
    if requested:
        is_claude = requested.lower().startswith("claude")
        is_gemini = requested.lower().startswith("gemini")
        if (p == "anthropic" and is_claude) or (p == "gemini" and is_gemini) or not (
            is_claude or is_gemini
        ):
            return requested
    return _fast_model(p) if _is_fast(requested) else _main_model(p)


# ---------------------------------------------------------------------------- providers
def _get_client():
    global _client
    if _client is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise LLMError("ANTHROPIC_API_KEY not set")
        from anthropic import AsyncAnthropic

        _client = AsyncAnthropic()
    return _client


async def _anthropic_text(system: str, user: str, temperature: float, max_tokens: int,
                          model: str) -> str:
    try:
        resp = await _get_client().messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except LLMError:
        raise
    except Exception as exc:  # network, rate limit, auth …
        raise LLMError(f"LLM call failed: {exc}") from exc
    return "".join(getattr(b, "text", "") for b in resp.content)


def _gemini_body(system: str, user: str, temperature: float, max_tokens: int, model: str,
                 thinking: bool) -> dict:
    cfg: dict[str, Any] = {"temperature": temperature, "maxOutputTokens": max_tokens}
    if thinking:
        # Flash models: no hidden "thinking" (it would eat the token budget and add latency).
        # Pro models always think, so give them headroom on top of the visible answer instead.
        if "flash" in model.lower():
            cfg["thinkingConfig"] = {"thinkingBudget": 0}
        else:
            cfg["maxOutputTokens"] = max_tokens + 2048
    return {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": cfg,
    }


async def _gemini_text(system: str, user: str, temperature: float, max_tokens: int,
                       model: str) -> str:
    key = _gemini_key()
    if not key:
        raise LLMError("GEMINI_API_KEY not set")
    base = (os.environ.get("REIGNS_GEMINI_BASE_URL") or GEMINI_BASE_URL).rstrip("/")
    url = f"{base}/models/{model}:generateContent"
    try:
        async with httpx.AsyncClient(timeout=GEMINI_TIMEOUT_S, transport=_transport) as client:
            resp = await client.post(url, headers={"x-goog-api-key": key},
                                     json=_gemini_body(system, user, temperature, max_tokens,
                                                       model, thinking=True))
            if resp.status_code == 400 and "thinking" in resp.text.lower():
                # a model that doesn't take thinking settings: ask again without them
                resp = await client.post(url, headers={"x-goog-api-key": key},
                                         json=_gemini_body(system, user, temperature,
                                                           max_tokens, model, thinking=False))
    except httpx.HTTPError as exc:
        raise LLMError(f"Gemini call failed: {exc}") from exc
    if resp.status_code != 200:
        raise LLMError(f"Gemini returned {resp.status_code}: {resp.text[:300]}")
    try:
        data = resp.json()
        cand = (data.get("candidates") or [{}])[0]
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    except (ValueError, AttributeError, IndexError) as exc:
        raise LLMError(f"Gemini sent an unreadable reply: {exc}") from exc
    if not text:
        reason = cand.get("finishReason") or (data.get("promptFeedback") or {}).get("blockReason")
        raise LLMError(f"Gemini returned no text (reason: {reason})")
    return text


_CALL = {"anthropic": _anthropic_text, "gemini": _gemini_text}


# ---------------------------------------------------------------------------- public API
async def complete_text(
    system: str,
    user: str,
    temperature: float = 0.0,
    max_tokens: int = 1024,
    model: str | None = None,
) -> str:
    p = provider()
    try:
        if not _has_key(p):
            raise LLMError(f"no API key for {p}")
        return await _CALL[p](system, user, temperature, max_tokens, _model_for(p, model))
    except LLMError as exc:
        other = _other(p)
        if not (fallback_enabled() and _has_key(other)):
            raise
        log.warning("%s failed (%s); falling back to %s", p, exc, other)
        return await _CALL[other](system, user, temperature, max_tokens, _model_for(other, model))


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
    system: str,
    user: str,
    temperature: float = 0.0,
    max_tokens: int = 2048,
    model: str | None = None,
) -> Any:
    system = system.rstrip() + "\n\nRespond with JSON only. No prose, no code fences."
    last_err: Exception | None = None
    for attempt in range(2):  # retry once on parse failure
        text = await complete_text(system, user, temperature, max_tokens, model)
        try:
            return parse_json(text)
        except (json.JSONDecodeError, ValueError) as exc:
            last_err = exc
            log.warning("llm json parse failed (attempt %d): %s", attempt + 1, exc)
    raise LLMError(f"could not parse JSON from model: {last_err}")


async def selftest() -> dict:
    """One tiny call through the configured provider (GET /debug/llm)."""
    p = provider()
    info: dict[str, Any] = {"provider": p, "model": model_name(), "fast_model": fast_model_name(),
                            "key_set": _has_key(p), "fallback": fallback_enabled()}
    try:
        info["reply"] = (await complete_text("Reply with exactly: OK", "Say OK.",
                                             max_tokens=20, model=fast_model_name())).strip()
        info["ok"] = True
    except LLMError as exc:
        info["ok"], info["error"] = False, str(exc)
    return info
