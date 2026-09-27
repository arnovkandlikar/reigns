"""LLM provider switch: Anthropic (default) or Gemini, with fallback to the other one."""
import json

import httpx
import pytest

from app import llm


@pytest.fixture
def gemini(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if handler.status != 200:
            return httpx.Response(handler.status, text=handler.body)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [
            {"text": "thinking...", "thought": True}, {"text": handler.reply}]}}]})

    handler.status, handler.body, handler.reply = 200, "", "OK"
    handler.calls = calls
    monkeypatch.setattr(llm, "_transport", httpx.MockTransport(handler))
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    monkeypatch.setenv("REIGNS_LLM_PROVIDER", "gemini")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return handler


def test_default_is_anthropic(monkeypatch):
    monkeypatch.setenv("REIGNS_MODEL", "claude-sonnet-5")
    assert llm.provider() == "anthropic" and llm.model_name() == "claude-sonnet-5"
    assert not llm.llm_available()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    assert llm.llm_available()


def test_gemini_models_ignore_claude_settings(gemini, monkeypatch):
    monkeypatch.setenv("REIGNS_MODEL", "claude-sonnet-5")  # left over in .env
    assert llm.model_name() == llm.DEFAULT_GEMINI_MODEL
    assert llm.fast_model_name() == llm.DEFAULT_GEMINI_FAST_MODEL
    monkeypatch.setenv("REIGNS_GEMINI_FAST_MODEL", "gemini-x-flash")
    assert llm.fast_model_name() == "gemini-x-flash" and llm.llm_available()


async def test_gemini_request_and_reply(gemini):
    out = await llm.complete_text("sys", "hello", max_tokens=50, model=llm.fast_model_name())
    assert out == "OK"  # hidden "thought" parts are dropped
    req = gemini.calls[-1]
    assert req.url.path.endswith(f"/models/{llm.DEFAULT_GEMINI_FAST_MODEL}:generateContent")
    assert req.headers["x-goog-api-key"] == "g-key"
    body = json.loads(req.content)
    assert body["systemInstruction"]["parts"][0]["text"] == "sys"
    assert body["contents"][0]["parts"][0]["text"] == "hello"
    assert body["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}


async def test_gemini_json(gemini):
    gemini.reply = '```json\n{"verdict": "reversed"}\n```'
    assert await llm.complete_json("judge", "x") == {"verdict": "reversed"}


async def test_claude_model_name_is_mapped_for_gemini(gemini):
    await llm.complete_text("s", "u", model="claude-haiku-4-5")
    assert llm.DEFAULT_GEMINI_FAST_MODEL in str(gemini.calls[-1].url)


async def test_thinking_setting_rejected_retries_without_it(gemini):
    state = {"n": 0}
    original = llm._transport

    def handler(request):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(400, text="thinking_config is not supported")
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "OK"}]}}]})

    llm._transport = httpx.MockTransport(handler)
    try:
        assert await llm.complete_text("s", "u", model=llm.fast_model_name()) == "OK"
        assert state["n"] == 2
    finally:
        llm._transport = original


async def test_gemini_error_falls_back_to_claude(gemini, monkeypatch):
    gemini.status, gemini.body = 429, "quota exceeded"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    seen = {}

    async def fake_claude(system, user, temperature, max_tokens, model):
        seen["model"] = model
        return "from claude"

    monkeypatch.setitem(llm._CALL, "anthropic", fake_claude)
    out = await llm.complete_text("s", "u", model=llm.fast_model_name())
    assert out == "from claude" and seen["model"] == llm.DEFAULT_FAST_MODEL


async def test_gemini_error_without_fallback_raises(gemini, monkeypatch):
    gemini.status, gemini.body = 403, "API key not valid"
    with pytest.raises(llm.LLMError, match="403"):
        await llm.complete_text("s", "u")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    monkeypatch.setenv("REIGNS_LLM_FALLBACK", "0")
    with pytest.raises(llm.LLMError):
        await llm.complete_text("s", "u")


async def test_selftest_reports_status(gemini):
    info = await llm.selftest()
    assert info["provider"] == "gemini" and info["ok"] and info["reply"] == "OK"
    gemini.status, gemini.body = 403, "API key not valid"
    info = await llm.selftest()
    assert info["ok"] is False and "403" in info["error"]
