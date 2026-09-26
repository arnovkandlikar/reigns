"""ElevenLabs voice for the pet (FR-V1, FR-V2, Role B).

When the pet speaks (FR-V2):
- the level rises to 3 (Alarmed) or 4 (Meltdown) → says the bubble headline (≤ 15 words)
- a verified fix → "Fixed it!" (Recovered)
- (bubble open would also speak, but §12 has no companion→engine message for it yet)
At most one line per 20 s per session.

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
import time
from pathlib import Path
from typing import Optional

import httpx

from app.models import BubbleContent, VoicePlay

log = logging.getLogger("reigns.voice")

MIN_GAP_S = 20.0
MAX_WORDS = 15
TTS_TIMEOUT_S = 6.0
API_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
DEFAULT_MODEL = "eleven_flash_v2_5"  # ElevenLabs' low-latency model
CACHE_DIR = Path(__file__).resolve().parents[1] / ".voice_cache"

# Pre-generated at startup (FR-V2 "~20 common lines"): Role D's level ≥ 3 headlines + extras.
COMMON_LINES = [
    "Fixed it!",
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


def clip_words(text: str, n: int = MAX_WORDS) -> str:
    words = text.split()
    return " ".join(words[:n]) + ("…" if len(words) > n else "")


def line_for(prev_level: int, level: int, bubble: BubbleContent, recovered: bool) -> Optional[str]:
    if recovered:
        return "Fixed it!"
    if level >= 3 and level > prev_level and bubble.headline:
        return clip_words(bubble.headline)
    return None


def _cache_path(text: str) -> Path:
    key = hashlib.sha1(f"{os.environ.get('REIGNS_VOICE_ID')}|{_model()}|{text}".encode())
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
                json={"text": text, "model_id": _model()},
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
    """Per-session rate limit (≤ 1 line / 20 s)."""

    def __init__(self) -> None:
        self.last_spoken = -MIN_GAP_S


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
    text = line_for(prev_level, level, bubble, recovered)
    now = time.monotonic() if now is None else now
    if not text or now - state.last_spoken < MIN_GAP_S:
        return None
    try:
        audio = await synthesize(text)
    except Exception as exc:
        log.error("voice failed: %s", exc)
        return None
    if not audio:
        return None
    state.last_spoken = now
    return VoicePlay(text=text, audio_b64=base64.b64encode(audio).decode(), level=level)
