"""FR-V1/FR-V2 voice: when it speaks, rate limit, disk cache, never raises. No real API calls."""
import base64
import json

import httpx
import pytest

from app import voice
from app.models import BubbleContent


@pytest.fixture
def api(monkeypatch, tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if handler.fail:
            return httpx.Response(401, json={"detail": "bad key"})
        return httpx.Response(200, content=b"ID3fake-mp3", headers={"content-type": "audio/mpeg"})

    handler.fail = False
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    monkeypatch.setenv("REIGNS_VOICE_ID", "voice123")
    monkeypatch.setattr(voice, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(voice, "_transport", httpx.MockTransport(handler))
    handler.calls = calls
    return handler


def bubble(level, headline="Heads up: a cited source could not be confirmed."):
    return BubbleContent(level=level, headline=headline)


def test_when_it_speaks():
    assert voice.line_for(0, 3, bubble(3), False) == bubble(3).headline
    assert voice.line_for(3, 3, bubble(3), False) is None  # didn't rise
    assert voice.line_for(1, 2, bubble(2), False) is None  # below Alarmed
    assert voice.line_for(3, 0, bubble(0), True) == "Fixed it!"
    long = " ".join(["word"] * 30)
    assert len(voice.line_for(0, 4, bubble(4, long), False).split()) == 15


async def test_speaks_then_rate_limits_and_caches(api):
    st = voice.VoiceState()
    vp = await voice.maybe_speak(st, 0, 3, bubble(3), False, now=100.0)
    assert vp and vp.level == 3 and base64.b64decode(vp.audio_b64) == b"ID3fake-mp3"
    req = api.calls[0]
    assert req.url.path == "/v1/text-to-speech/voice123"
    assert req.headers["xi-api-key"] == "test-key"
    assert json.loads(req.content)["text"] == bubble(3).headline
    # within 20 s → silent
    assert await voice.maybe_speak(st, 0, 4, bubble(4), False, now=110.0) is None
    # after 20 s, same line → served from disk cache, no new API call
    assert await voice.maybe_speak(st, 0, 3, bubble(3), False, now=121.0)
    assert len(api.calls) == 1


async def test_failure_is_silent(api):
    api.fail = True
    assert await voice.maybe_speak(voice.VoiceState(), 0, 3, bubble(3), False, now=0) is None


async def test_off_without_keys(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    assert await voice.maybe_speak(voice.VoiceState(), 0, 3, bubble(3), False) is None
    await voice.warm_cache()  # no-op, no error


async def test_warm_cache_generates_common_lines(api):
    await voice.warm_cache()
    assert len(api.calls) == len(voice.COMMON_LINES)
    await voice.warm_cache()  # second time: all cached
    assert len(api.calls) == len(voice.COMMON_LINES)
