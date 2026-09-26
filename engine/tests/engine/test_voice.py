"""FR-V1/FR-V2 voice: when it speaks, rate limit, disk cache, never raises. No real API calls."""
import base64
import json

import httpx
import pytest

from app import voice
from app.models import BubbleContent, BubbleProblem


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


def test_when_it_speaks_short_mode(monkeypatch):
    monkeypatch.setenv("REIGNS_VOICE_MODE", "short")
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


def full_bubble(level=2, ids=("c1", "c2")):
    return BubbleContent(
        level=level,
        headline="Heads up: a cited source could not be confirmed",
        problems=[BubbleProblem(claim_id=i, text=f"Problem {i} does not check out") for i in ids],
        pattern_text="Claude tends to invent citations in this topic",
    )


def test_full_mode_reads_whole_bubble(monkeypatch):
    monkeypatch.delenv("REIGNS_VOICE_MODE", raising=False)  # full is the default
    line = voice.line_for(0, 2, full_bubble(), False)
    assert line == ("Heads up: a cited source could not be confirmed. Problem c1 does not check out. "
                    "Problem c2 does not check out. Claude tends to invent citations in this topic.")
    # nothing new → silent; a new problem → speaks again; level 0 → silent
    assert voice.line_for(2, 2, full_bubble(), False, {"c1", "c2"}) is None
    assert voice.line_for(2, 2, full_bubble(ids=("c1", "c3")), False, {"c1", "c2"})
    assert voice.line_for(0, 0, full_bubble(level=0), False) is None
    # long bubbles are capped
    many = full_bubble(ids=[f"c{i}" for i in range(10)])
    many.pattern_text = " ".join(["word"] * 100)
    capped = voice.line_for(0, 2, many, False)
    assert "Plus 7 more." in capped and len(capped.split()) == voice.FULL_MAX_WORDS


async def test_full_mode_does_not_repeat_itself(api, monkeypatch):
    monkeypatch.setenv("REIGNS_VOICE_MODE", "full")
    st = voice.VoiceState()
    assert await voice.maybe_speak(st, 0, 2, full_bubble(), False, now=0.0)
    assert await voice.maybe_speak(st, 2, 2, full_bubble(), False, now=30.0) is None  # same problems
    assert await voice.maybe_speak(st, 2, 2, full_bubble(ids=("c9",)), False, now=60.0)
    assert await voice.maybe_speak(st, 2, 0, full_bubble(level=0), True, now=90.0)  # Fixed it!
    assert st.spoken_ids == set()


async def test_speed_setting(api, monkeypatch):
    monkeypatch.delenv("REIGNS_VOICE_SPEED", raising=False)
    await voice.synthesize("hello there")
    assert json.loads(api.calls[-1].content)["voice_settings"]["speed"] == voice.DEFAULT_SPEED
    monkeypatch.setenv("REIGNS_VOICE_SPEED", "5")  # clamped to ElevenLabs' max
    await voice.synthesize("hello there")  # new speed → new cache entry → new call
    assert len(api.calls) == 2
    assert json.loads(api.calls[-1].content)["voice_settings"]["speed"] == 1.2
