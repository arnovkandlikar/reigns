"""ElevenLabs voice for the pet (FR-V1, FR-V2, Role B).

Two modes, picked with REIGNS_VOICE_MODE:
- full (default): whenever a reply brings NEW problems (level ≥ 1), the pet talks the user
  through ALL of them. With an Anthropic key, the fast model turns the bubble into a short,
  natural spoken explanation (~50 words, 2-3 sentences); without one (or if it's slow) it reads the bubble.
- short (PRD FR-V2): only when the level rises to 3 or 4, says the headline (≤ 15 words).
Personality: REIGNS_VOICE_STYLE=cowboy (default) or plain. Pair it with a cowboy voice from
the ElevenLabs Voice Library in REIGNS_VOICE_ID.
Delivery: REIGNS_VOICE_SPEED (0.7–1.2, default 1.2) and REIGNS_VOICE_STABILITY (0–1, default
0.35; lower = more expressive) are ElevenLabs' own settings, so no pitch change.
A verified fix → "Yeehaw, fixed it, partner!" / "Fixed it!". At most one line per 20 s.

Speed: every line is cached on disk (engine/.voice_cache/, git-ignored) and ~20 common lines
are pre-generated at startup, so the usual lines play instantly.
Safety: never raises, never blocks a verdict (called fire-and-forget). If ElevenLabs is off
or fails, returns None and the companion falls back to macOS speech or silence (FR-V3).
Uses the ElevenLabs REST API through httpx (already a dependency), so no SDK version issues.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse
from typing import Optional

import httpx

from app.models import BubbleContent, VoicePlay

log = logging.getLogger("reigns.voice")

MIN_GAP_S = 20.0
MAX_WORDS = 15
FULL_MAX_WORDS = 55
FULL_MAX_PROBLEMS = 3
SUMMARY_MAX_WORDS = 50  # explains, doesn't lecture: ~12-15 s of speech
SUMMARY_HARD_CAP = 75  # safety net only; the prompt keeps it well under this
SUMMARY_TIMEOUT_S = 8.0  # voice is fire-and-forget, so waiting a bit longer never delays a verdict
TTS_TIMEOUT_S = 6.0
API_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
DEFAULT_MODEL = "eleven_flash_v2_5"  # ElevenLabs' low-latency model
DEFAULT_SPEED = 1.2  # 1.0 = normal; ElevenLabs allows 0.7–1.2
DEFAULT_STABILITY = 0.35  # lower = livelier, more natural delivery
CACHE_DIR = Path(__file__).resolve().parents[1] / ".voice_cache"

# Pre-generated at startup (FR-V2 "~20 common lines"): Role D's level ≥ 3 headlines + extras.
COMMON_LINES = [
    "Fixed it!",
    "Yeehaw, fixed it, partner!",
    "Heads up: a cited source could not be confirmed.",
    "Heads up: the summary drifts from the document.",
    "Heads up: this code uses an unsupported API.",
    "The answer changed without new evidence.",
    "Some details are uncertain and need checking.",
    "This detail may be out of date.",
    "Later steps may rely on a wrong assumption.",
    "Some claims conflict with the evidence.",
    "This conversation has drifted off course.",
    "Heads up: Claude is stating things that don't check out.",
    "This conversation has gone off track. Consider starting fresh.",
    "Hmm, that paper doesn't exist.",
    "Hmm, that link is broken.",
    "That package doesn't exist.",
    "That number isn't in your article.",
    "That function doesn't exist.",
    "Claude caved. Its first answer was right.",
    "Heads up: Claude is making up sources.",
    "Let's double-check that.",
]

_transport: Optional[httpx.AsyncBaseTransport] = None  # tests inject a MockTransport


def enabled() -> bool:
    return bool(os.environ.get("ELEVENLABS_API_KEY") and os.environ.get("REIGNS_VOICE_ID"))


def _model() -> str:
    return os.environ.get("REIGNS_VOICE_MODEL") or DEFAULT_MODEL


def _speed() -> float:
    try:
        v = float(os.environ.get("REIGNS_VOICE_SPEED") or DEFAULT_SPEED)
    except ValueError:
        v = DEFAULT_SPEED
    return round(min(1.2, max(0.7, v)), 2)


_SENTENCE = re.compile(r"(?<=[.!?…])\s+")


def clip_sentences(text: str, n: int) -> str:
    """Keep whole sentences while they fit in n words (so speech never stops mid-thought);
    fall back to a word clip only if the first sentence alone is too long."""
    out, count = [], 0
    for sent in _SENTENCE.split(" ".join(text.split())):
        w = len(sent.split())
        if count + w > n:
            break
        out.append(sent)
        count += w
    return " ".join(out) if out else clip_words(text, n)


def _stability() -> float:
    try:
        v = float(os.environ.get("REIGNS_VOICE_STABILITY") or DEFAULT_STABILITY)
    except ValueError:
        v = DEFAULT_STABILITY
    return round(min(1.0, max(0.0, v)), 2)


def style() -> str:
    st = (os.environ.get("REIGNS_VOICE_STYLE") or "cowboy").strip().lower()
    return st if st in ("cowboy", "plain") else "cowboy"


def recovered_line() -> str:
    return "Yeehaw, fixed it, partner!" if style() == "cowboy" else "Fixed it!"


def clip_words(text: str, n: int = MAX_WORDS) -> str:
    words = text.split()
    return " ".join(words[:n]) + ("…" if len(words) > n else "")


def mode() -> str:
    m = (os.environ.get("REIGNS_VOICE_MODE") or "full").strip().lower()
    return m if m in ("full", "short") else "full"


def _sentence(text: str) -> str:
    text = " ".join(text.split())
    return text if not text or text[-1] in ".!?…" else text + "."


def full_line(bubble: BubbleContent) -> str:
    """Headline + up to 3 problems + pattern, as one spoken paragraph (≤ ~70 words)."""
    parts = [bubble.headline]
    parts += [p.text for p in bubble.problems[:FULL_MAX_PROBLEMS]]
    extra = len(bubble.problems) - FULL_MAX_PROBLEMS
    parts.append(bubble.pattern_text)
    if extra > 0:
        parts.append("There's a bit more in the bubble.")
    text = " ".join(_sentence(t) for t in parts if t and t.strip())
    return clip_sentences(text, FULL_MAX_WORDS)


def _new_problem_ids(bubble: BubbleContent, spoken_ids: set[str]) -> set[str]:
    return {p.claim_id for p in bubble.problems} - spoken_ids


def line_for(
    prev_level: int,
    level: int,
    bubble: BubbleContent,
    recovered: bool,
    spoken_ids: Optional[set[str]] = None,
) -> Optional[str]:
    if recovered:
        return recovered_line()
    if mode() == "short":
        if level >= 3 and level > prev_level and bubble.headline:
            return clip_words(bubble.headline)
        return None
    # full mode
    if level < 1 or not bubble.headline:
        return None
    rose_to_alarm = level >= 3 and level > prev_level
    if _new_problem_ids(bubble, spoken_ids or set()) or rose_to_alarm:
        text = full_line(bubble)
        return clip_sentences("Whoa there, partner. " + text, FULL_MAX_WORDS) if style() == "cowboy" else text
    return None


_SUMMARY_SYSTEM = {
    "cowboy": (
        "You are Reigns, a friendly cowboy horse who watches over someone's chat with Claude and "
        "speaks out loud when Claude gets something wrong. Say it like a warm, laid-back cowboy "
        "talking to a friend: natural, relaxed, a little folksy (e.g. 'whoa there', 'partner', "
        "'I reckon') but never cartoonish, and keep the facts exact."
    ),
    "plain": (
        "You are Reigns, a friendly assistant pet that speaks out loud when Claude gets something "
        "wrong in a chat. Sound natural and conversational, like a helpful friend."
    ),
}
_SUMMARY_RULES = (
    "Explain the notes below out loud IN YOUR OWN WORDS; don't read them back. 2-3 sentences, "
    "about {n} words at most. Cover: what Claude got wrong (every problem, similar ones grouped, "
    "e.g. 'two made-up papers and a broken link'); briefly why it's wrong or what the real answer "
    "is, using the evidence source if given (e.g. 'Wikipedia says 1889'); and what the user should "
    "do next. Elaborate a little, but don't list every detail. Always finish your last sentence. "
    "Spoken English only: no lists, markdown, emojis, URLs, stage directions, or quotation marks "
    "around the whole thing. Reply with only the words to speak."
)


def _summary_input(bubble: BubbleContent) -> str:
    lines = [f"Headline: {bubble.headline}"]
    for p in bubble.problems:
        src = urlparse(p.evidence_url).netloc.removeprefix("www.") if p.evidence_url else ""
        lines.append(f"Problem: {p.text}" + (f" (evidence: {src})" if src else ""))
    if bubble.pattern_text:
        lines.append(f"Pattern: {bubble.pattern_text}")
    if bubble.action_text:
        lines.append(f"Suggested next step: {bubble.action_text}")
    if bubble.confidence_label:
        lines.append(f"How sure: {bubble.confidence_label} {bubble.confidence_reason}".strip())
    return "\n".join(lines)


def _clean_spoken(text: str) -> str:
    text = re.sub(r"[*_#`>\[\]]", "", text or "")
    text = re.sub(r"https?://\S+", "", text)
    text = " ".join(text.split()).strip().strip('"').strip()
    return clip_sentences(text, SUMMARY_HARD_CAP)


async def summarize(bubble: BubbleContent) -> Optional[str]:
    """Natural spoken summary of the whole bubble via the fast model. None if unavailable/slow."""
    try:
        from app import llm
        if not llm.llm_available():
            return None
        raw = await asyncio.wait_for(
            llm.complete_text(
                _SUMMARY_SYSTEM[style()],
                _SUMMARY_RULES.format(n=SUMMARY_MAX_WORDS) + "\n\n" + _summary_input(bubble),
                max_tokens=300,
                model=llm.fast_model_name(),
            ),
            timeout=SUMMARY_TIMEOUT_S,
        )
    except Exception as exc:  # LLMError, timeout, anything: fall back to reading the bubble
        log.warning("voice: summary failed (%r), reading the bubble instead", exc)
        return None
    text = _clean_spoken(raw)
    return text if len(text.split()) >= 4 else None


def _cache_path(text: str) -> Path:
    key = hashlib.sha1(f"{os.environ.get('REIGNS_VOICE_ID')}|{_model()}|{_speed()}|{_stability()}|{text}".encode())
    return CACHE_DIR / f"{key.hexdigest()}.mp3"


async def synthesize(text: str) -> Optional[bytes]:
    """MP3 bytes for `text`, from the disk cache or ElevenLabs. None if voice is off/fails."""
    if not enabled() or not text:
        return None
    path = _cache_path(text)
    if path.exists():
        return path.read_bytes()
    url = API_URL.format(voice_id=os.environ["REIGNS_VOICE_ID"])
    try:
        async with httpx.AsyncClient(timeout=TTS_TIMEOUT_S, transport=_transport) as client:
            resp = await client.post(
                url,
                params={"output_format": "mp3_44100_128"},
                headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"],
                         "accept": "audio/mpeg"},
                json={"text": text, "model_id": _model(),
                      "voice_settings": {"speed": _speed(), "stability": _stability(),
                                         "similarity_boost": 0.8}},
            )
    except httpx.HTTPError as exc:
        log.warning("elevenlabs request failed: %s", exc)
        return None
    if resp.status_code != 200 or not resp.content:
        log.warning("elevenlabs returned %s: %s", resp.status_code, resp.text[:200])
        return None
    try:
        CACHE_DIR.mkdir(exist_ok=True)
        path.write_bytes(resp.content)
    except OSError as exc:
        log.warning("voice cache write failed: %s", exc)
    return resp.content


async def warm_cache() -> None:
    """Pre-generate COMMON_LINES in the background at startup (FR-V2). Never raises."""
    if not enabled():
        log.info("voice disabled (no ELEVENLABS_API_KEY / REIGNS_VOICE_ID)")
        return
    made = 0
    for line in COMMON_LINES:
        try:
            if not _cache_path(line).exists() and await synthesize(line):
                made += 1
        except Exception as exc:  # pragma: no cover - belt and braces
            log.warning("voice warm-up failed on %r: %s", line, exc)
        await asyncio.sleep(0)  # stay polite to the event loop
    log.info("voice cache ready", extra={"new_lines": made, "total": len(COMMON_LINES)})


class VoiceState:
    """Per-session rate limit (≤ 1 line / 20 s) + which problems were already read out."""

    def __init__(self) -> None:
        self.last_spoken = -MIN_GAP_S
        self.spoken_ids: set[str] = set()


async def maybe_speak(
    state: VoiceState,
    prev_level: int,
    level: int,
    bubble: BubbleContent,
    recovered: bool,
    now: Optional[float] = None,
) -> Optional[VoicePlay]:
    if not enabled():
        return None
    text = line_for(prev_level, level, bubble, recovered, state.spoken_ids)
    now = time.monotonic() if now is None else now
    if not text or now - state.last_spoken < MIN_GAP_S:
        return None
    prev_spoken, state.last_spoken = state.last_spoken, now  # hold the slot while we work
    if mode() == "full" and not recovered:
        summary = await summarize(bubble)
        log.info("voice: speaking %s (%d words)", "summary" if summary else "bubble text",
                 len((summary or text).split()))
        text = summary or text
    try:
        audio = await synthesize(text)
    except Exception as exc:
        log.error("voice failed: %s", exc)
        audio = None
    if not audio:
        state.last_spoken = prev_spoken
        return None
    if recovered:
        state.spoken_ids.clear()  # after a fix, a relapse should be read out again
    else:
        state.spoken_ids |= {p.claim_id for p in bubble.problems}
    return VoicePlay(text=text, audio_b64=base64.b64encode(audio).decode(), level=level)
