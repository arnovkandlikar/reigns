"""ElevenLabs voice (FR-V1, FR-V2, Role B) — STEP 6, hours 24–30.

Speaks only when the level rises to 3/4, on Recovered, (and on bubble open — needs a
companion signal; not in §12 yet). ≤ 1 line per 20 s, ≤ 15 words, from the bubble headline.
Never raises; returns None when voice is off or fails (companion then stays silent).
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

from app.models import BubbleContent, VoicePlay

log = logging.getLogger("reigns.voice")

MIN_GAP_S = 20.0
_last_spoken = 0.0


def enabled() -> bool:
    return bool(os.environ.get("ELEVENLABS_API_KEY") and os.environ.get("REIGNS_VOICE_ID"))


def line_for(prev_level: int, level: int, bubble: BubbleContent, recovered: bool) -> Optional[str]:
    if recovered:
        return "Fixed it!"
    if level >= 3 and level > prev_level:
        return " ".join(bubble.headline.split()[:15])
    return None


async def synthesize(text: str) -> Optional[str]:
    """Return base64 mp3. TODO(FR-V1): ElevenLabs TTS + in-memory/disk cache of ~20 lines."""
    return None


async def maybe_speak(
    prev_level: int, level: int, bubble: BubbleContent, recovered: bool
) -> Optional[VoicePlay]:
    global _last_spoken
    if not enabled():
        return None
    text = line_for(prev_level, level, bubble, recovered)
    if not text or time.monotonic() - _last_spoken < MIN_GAP_S:
        return None
    try:
        audio = await synthesize(text)
    except Exception as exc:
        log.error("voice failed: %s", exc)
        return None
    if not audio:
        return None
    _last_spoken = time.monotonic()
    return VoicePlay(text=text, audio_b64=audio, level=level)
