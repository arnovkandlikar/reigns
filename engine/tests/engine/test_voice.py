"""FR-V1/FR-V2 voice: when it speaks, rate limit, disk cache, never raises. No real API calls."""
import base64
import json

import httpx
import pytest

from app import voice
from app.models import BubbleContent, BubbleProblem


@pytest.fixture(autouse=True)
def _plain_style_no_llm(monkeypatch):
    """Existing tests check exact wording: plain style, and no real LLM summary calls."""
    monkeypatch.setenv("REIGNS_VOICE_STYLE", "plain")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


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
    n = len({(line, voice.voice_id(p)) for p in (voice.style(), "unicorn")
             for line in voice.warm_lines(p)})
    assert "¡Arreglado!" in voice.warm_lines() and "Sin problemas." in voice.warm_lines()
    await voice.warm_cache()
    assert len(api.calls) == n
    await voice.warm_cache()  # second time: all cached
    assert len(api.calls) == n


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
    # long bubbles: every problem is still read; the long pattern line is what gets dropped
    many = full_bubble(ids=[f"c{i}" for i in range(10)])
    many.pattern_text = " ".join(["word"] * 100)
    capped = voice.line_for(0, 2, many, False)
    assert len(capped.split()) <= voice.full_budget(10)
    assert all(f"Problem c{i} does not check out." in capped for i in range(10))
    assert capped.endswith(".") and "word word" not in capped


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
    monkeypatch.setenv("REIGNS_VOICE_SPEED", "0.1")  # clamped to ElevenLabs' min
    await voice.synthesize("hello there")  # new speed → new cache entry → new call
    assert len(api.calls) == 2
    assert json.loads(api.calls[-1].content)["voice_settings"]["speed"] == 0.7
    monkeypatch.setenv("REIGNS_VOICE_SPEED", "5")  # clamped to ElevenLabs' max
    await voice.synthesize("hello there again")
    assert json.loads(api.calls[-1].content)["voice_settings"]["speed"] == 1.2


def test_cowboy_style_lines(monkeypatch):
    monkeypatch.setenv("REIGNS_VOICE_STYLE", "cowboy")
    assert voice.line_for(3, 0, bubble(0), True) == "Yeehaw, fixed it, partner!"
    assert voice.line_for(0, 2, full_bubble(), False).startswith("Whoa there, partner. Heads up")
    assert "Yeehaw, fixed it, partner!" in voice.COMMON_LINES


async def test_full_mode_speaks_a_natural_summary(api, monkeypatch):
    from app import llm

    seen = {}

    async def fake_complete_text(system, user, **kw):
        seen["system"], seen["user"], seen["model"] = system, user, kw.get("model")
        return '"Whoa there, partner. That paper does not exist and the link is busted. **Double-check** it."'

    monkeypatch.setenv("REIGNS_VOICE_STYLE", "cowboy")
    monkeypatch.setattr(llm, "llm_available", lambda: True)
    monkeypatch.setattr(llm, "complete_text", fake_complete_text)
    vp = await voice.maybe_speak(voice.VoiceState(), 0, 2, full_bubble(), False, now=0.0)
    assert vp.text == "Whoa there, partner. That paper does not exist and the link is busted. Double-check it."
    assert "cowboy" in seen["system"] and "Problem c1" in seen["user"] and "Problem c2" in seen["user"]
    assert seen["model"] == llm.fast_model_name()
    body = json.loads(api.calls[-1].content)
    assert body["text"] == vp.text and body["voice_settings"]["stability"] == voice.DEFAULT_STABILITY


async def test_summary_failure_falls_back_to_reading_the_bubble(api, monkeypatch):
    from app import llm

    async def boom(*a, **k):
        raise llm.LLMError("down")

    monkeypatch.setattr(llm, "llm_available", lambda: True)
    monkeypatch.setattr(llm, "complete_text", boom)
    vp = await voice.maybe_speak(voice.VoiceState(), 0, 2, full_bubble(), False, now=0.0)
    assert vp.text == voice.full_line(full_bubble())


async def test_failed_audio_does_not_use_up_the_slot(api):
    api.fail = True
    st = voice.VoiceState()
    assert await voice.maybe_speak(st, 0, 3, bubble(3), False, now=0.0) is None
    api.fail = False
    assert await voice.maybe_speak(st, 0, 3, bubble(3), False, now=1.0)


def test_clip_sentences_keeps_whole_sentences():
    text = "One two three. Four five six seven. Eight nine."
    assert voice.clip_sentences(text, 7) == "One two three. Four five six seven."
    assert voice.clip_sentences(text, 2) == "One two…"  # first sentence alone too long


async def test_long_llm_summary_is_trimmed_to_whole_sentences(api, monkeypatch):
    from app import llm

    async def chatty(*a, **k):
        return "Whoa there, partner, that paper is made up. " + "This goes on and on. " * 10

    monkeypatch.setattr(llm, "llm_available", lambda: True)
    monkeypatch.setattr(llm, "complete_text", chatty)
    vp = await voice.maybe_speak(voice.VoiceState(), 0, 2, full_bubble(), False, now=0.0)
    assert len(vp.text.split()) <= voice.summary_hard_cap(2) and vp.text.endswith(".")


def test_summary_input_gives_evidence_source_and_next_step():
    b = BubbleContent(
        level=2, headline="A date is wrong",
        problems=[BubbleProblem(claim_id="c1", text="The tower was finished in 1889, not 1899",
                                evidence_url="https://en.wikipedia.org/wiki/Eiffel_Tower")],
        action_text="Want Claude to double-check?",
    )
    notes = voice._summary_input(b)
    assert "(evidence: en.wikipedia.org)" in notes and "Suggested next step:" in notes
    assert "IN YOUR OWN WORDS" in voice._SUMMARY_RULES


SIX = [("p1", "The paper 'Deep Hive Networks' by Lee does not exist"),
       ("p2", "The paper 'Swarm Transformers' by Ortiz does not exist"),
       ("d1", "The Eiffel Tower was finished in 1889, not 1899"),
       ("d2", "The Berlin Wall fell in 1989, not 1991"),
       ("d3", "The Moon landing was in 1969, not 1972"),
       ("u1", "The link example.org/dataset is broken")]


def six_bubble():
    return BubbleContent(level=3, headline="Several claims don't check out",
                         problems=[BubbleProblem(claim_id=c, text=t) for c, t in SIX],
                         pattern_text="Claude is guessing on dates and sources in this chat")


def test_bubble_fallback_reads_every_problem():
    line = voice.full_line(six_bubble())
    for _, text in SIX:
        assert text in line  # each problem said in full, none dropped
    assert "bit more" not in line and "more problems" not in line
    assert len(line.split()) <= voice.full_budget(6) and line.endswith(".")
    # and a very long list still names every problem, just more briefly
    many = BubbleContent(level=4, headline="Lots of problems", problems=[
        BubbleProblem(claim_id=f"x{i}", text=f"Claim number {i} about topic {i} is contradicted by "
                      "the evidence found on Wikipedia and two other sources") for i in range(12)])
    line = voice.full_line(many)
    assert all(f"Claim number {i} " in line for i in range(12))
    assert len(line.split()) <= voice.full_budget(12)


def test_word_budget_grows_with_problems():
    assert voice.summary_budget(1) == voice.summary_budget(3) == 50
    assert voice.summary_budget(6) == 95 and voice.summary_budget(50) == voice.SUMMARY_MAX_CAP
    assert voice.full_budget(3) == 55 and voice.full_budget(6) == 100
    assert voice.summary_hard_cap(6) == 120


async def test_summary_covers_all_six_problems(api, monkeypatch):
    from app import llm

    seen = {}
    spoken = ("Hold your horses. Claude made up two papers, 'Deep Hive Networks' and 'Swarm "
              "Transformers'. It also got three dates wrong: the Eiffel Tower was finished in "
              "1889, the Berlin Wall fell in 1989, and the Moon landing was in 1969. On top of that, "
              "the example.org dataset link is broken. It's guessing on dates and sources, so ask "
              "it to check each of these against a real source before you use any of it.")

    async def fake(system, user, **kw):
        seen["user"], seen["max_tokens"] = user, kw.get("max_tokens")
        return spoken

    monkeypatch.setenv("REIGNS_VOICE_STYLE", "cowboy")
    monkeypatch.setattr(llm, "llm_available", lambda: True)
    monkeypatch.setattr(llm, "complete_text", fake)
    vp = await voice.maybe_speak(voice.VoiceState(), 0, 3, six_bubble(), False, now=0.0)
    # the model is told the bigger budget and to cover all 6, and gets all 6
    assert "About 95 words" in seen["user"] and "EVERY one of the 6" in seen["user"]
    assert all(f"Problem: {text}" in seen["user"] for _, text in SIX)
    assert seen["max_tokens"] >= 600
    # the ~75-word answer is spoken whole, not clipped at the old 75-word cap
    assert vp.text == spoken
    for word in ("Deep Hive", "Swarm", "1889", "1989", "1969", "example.org"):
        assert word in vp.text


async def test_spanish_long_summary_is_not_cut(api, monkeypatch):
    from app import llm

    seen = {}
    spoken = ("¡Epa, compañero! Claude inventó dos artículos, 'Deep Hive Networks' y 'Swarm "
              "Transformers', y se equivocó en tres fechas: la Torre Eiffel se terminó en 1889, el "
              "Muro de Berlín cayó en 1989 y la llegada a la Luna fue en 1969. Además, el enlace "
              "de example.org está roto. Pídele que revise cada dato con una fuente real antes de "
              "usarlo.")

    async def fake(system, user, **kw):
        seen["user"] = user
        return spoken

    monkeypatch.setenv("REIGNS_VOICE_STYLE", "cowboy")
    monkeypatch.setattr(llm, "llm_available", lambda: True)
    monkeypatch.setattr(llm, "complete_text", fake)
    vp = await voice.maybe_speak(voice.VoiceState(), 0, 3, six_bubble(), False, now=0.0, lang="es")
    assert "Latin American Spanish" in seen["user"] and "EVERY one of the 6" in seen["user"]
    assert vp.text == spoken
    # Spanish fallback (no summary) still counts all six, in Spanish
    assert "Encontré 6 problemas" in voice.spanish_fallback(six_bubble())


def test_openers_and_praise_never_repeat_back_to_back(monkeypatch):
    from collections import deque

    monkeypatch.setenv("REIGNS_VOICE_STYLE", "cowboy")
    recent = deque(maxlen=4)
    openers = [voice.pick_opener(recent) for _ in range(30)]
    assert all(a != b for a, b in zip(openers, openers[1:]))
    assert len(set(openers)) > 5
    praise_recent = deque(maxlen=4)
    praise = [voice.pick_praise(praise_recent) for _ in range(30)]
    assert all(a != b for a, b in zip(praise, praise[1:]))


async def test_summary_is_asked_to_start_with_the_chosen_opener(api, monkeypatch):
    from app import llm

    seen = {}

    async def fake(system, user, **kw):
        seen["user"] = user
        return "Hold your horses. Claude made up a paper and a link, so double-check before using them."

    monkeypatch.setenv("REIGNS_VOICE_STYLE", "cowboy")
    monkeypatch.setattr(llm, "llm_available", lambda: True)
    monkeypatch.setattr(llm, "complete_text", fake)
    st = voice.VoiceState()
    await voice.maybe_speak(st, 0, 2, full_bubble(), False, now=0.0)
    assert f'Start with exactly these words: "{st.recent_openers[-1]}"' in seen["user"]
    assert "howdy" in voice._SUMMARY_SYSTEM["cowboy"].lower()  # told NOT to say it


async def test_clean_reply_gets_praise_but_not_too_often(api, monkeypatch):
    st = voice.VoiceState()
    calm = BubbleContent(level=0, headline="All clear")
    vp = await voice.maybe_speak(st, 0, 0, calm, False, now=100.0, clean=True)
    assert vp and vp.text in voice.PRAISE_LINES["plain"]
    assert await voice.maybe_speak(st, 0, 0, calm, False, now=125.0, clean=True) is None  # < 45 s
    assert await voice.maybe_speak(st, 0, 0, calm, False, now=150.0, clean=True)
    assert await voice.maybe_speak(voice.VoiceState(), 0, 0, calm, False, now=0.0) is None  # not checked
    monkeypatch.setenv("REIGNS_VOICE_PRAISE", "0")
    assert await voice.maybe_speak(voice.VoiceState(), 0, 0, calm, False, now=0.0, clean=True) is None


async def test_spanish_session_speaks_spanish(api, monkeypatch):
    from app import llm

    seen = {}

    async def fake(system, user, **kw):
        seen["user"] = user
        return "¡Epa, compañero! Claude inventó un artículo y un enlace roto, así que revísalos."

    monkeypatch.setenv("REIGNS_VOICE_STYLE", "cowboy")
    monkeypatch.setattr(llm, "llm_available", lambda: True)
    monkeypatch.setattr(llm, "complete_text", fake)
    st = voice.VoiceState()
    vp = await voice.maybe_speak(st, 0, 2, full_bubble(), False, now=0.0, lang="es")
    assert "Latin American Spanish" in seen["user"]
    assert st.recent_openers[-1] in voice.OPENERS_ES["cowboy"]
    assert vp.text.startswith("¡Epa")
    fix = await voice.maybe_speak(voice.VoiceState(), 2, 0, full_bubble(level=0), True, now=0.0, lang="es")
    assert fix.text == "¡Yija! Arreglado, compañero."
    calm = BubbleContent(level=0, headline="Todo bien")
    praise = await voice.maybe_speak(voice.VoiceState(), 0, 0, calm, False, now=0.0, clean=True, lang="es")
    assert praise.text in voice.PRAISE_LINES_ES["cowboy"]


def test_language_defaults_and_env(monkeypatch):
    monkeypatch.delenv("REIGNS_LANGUAGE", raising=False)
    assert voice.language(None) == "en" and voice.language("fr") == "en"
    assert voice.language("es") == "es" and voice.language("ES-mx") == "es"
    monkeypatch.setenv("REIGNS_LANGUAGE", "es")
    assert voice.language(None) == "es"


async def test_unicorn_has_its_own_personality_and_voice(api, monkeypatch):
    from app import llm

    seen = {}

    async def fake(system, user, **kw):
        seen["system"], seen["user"] = system, user
        return "Oh my stars! Claude made up a paper and a broken link, so double-check those."

    monkeypatch.setenv("REIGNS_UNICORN_VOICE_ID", "sparkly456")
    monkeypatch.setattr(llm, "llm_available", lambda: True)
    monkeypatch.setattr(llm, "complete_text", fake)
    st = voice.VoiceState()
    vp = await voice.maybe_speak(st, 0, 2, full_bubble(), False, now=0.0, character="Marley")
    assert "Marley, a cheerful, sparkly unicorn" in seen["system"]
    assert st.recent_openers[-1] in voice.OPENERS["unicorn"]
    assert api.calls[-1].url.path == "/v1/text-to-speech/sparkly456"  # her own voice
    assert vp.text.startswith("Oh my stars")
    fix = await voice.maybe_speak(voice.VoiceState(), 2, 0, full_bubble(level=0), True, now=0.0,
                                  character="marley", lang="es")
    assert fix.text == "¡Yupi, todo arreglado! ¡Brillitos!"
    calm = BubbleContent(level=0, headline="All clear")
    praise = await voice.maybe_speak(voice.VoiceState(), 0, 0, calm, False, now=0.0, clean=True,
                                     character="marley")
    assert praise.text in voice.PRAISE_LINES["unicorn"]
    # the horse is untouched: default character keeps the horse's voice
    await voice.maybe_speak(voice.VoiceState(), 0, 0, calm, False, now=0.0, clean=True)
    assert api.calls[-1].url.path == "/v1/text-to-speech/voice123"


def test_unicorn_falls_back_to_the_horse_voice_id(monkeypatch):
    monkeypatch.setenv("REIGNS_VOICE_ID", "horse1")
    monkeypatch.delenv("REIGNS_UNICORN_VOICE_ID", raising=False)
    assert voice.voice_id("unicorn") == "horse1"
    assert voice.persona("Marley") == "unicorn" and voice.persona("charlie") == voice.style()
    assert voice.persona(None) == voice.style()


async def test_spanish_never_reads_the_english_bubble(api, monkeypatch):
    from app import llm

    async def down(*a, **k):
        raise llm.LLMError("down")

    monkeypatch.setenv("REIGNS_VOICE_STYLE", "cowboy")
    monkeypatch.setattr(llm, "llm_available", lambda: True)
    monkeypatch.setattr(llm, "complete_text", down)
    for character, openers in (("charlie", voice.OPENERS_ES["cowboy"]),
                               ("marley", voice.OPENERS_ES["unicorn"])):
        vp = await voice.maybe_speak(voice.VoiceState(), 0, 2, full_bubble(), False, now=0.0,
                                     lang="es", character=character)
        assert "Encontré 2 problemas" in vp.text and "Problem" not in vp.text
        assert any(vp.text.startswith(o) for o in openers)
    monkeypatch.setenv("REIGNS_VOICE_MODE", "short")
    assert voice.line_for(0, 3, full_bubble(level=3), False, lang="es").startswith("Encontré")
