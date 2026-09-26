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
Variety: each warning opens with a different greeting (never the same one twice in a row), and a
clean reply that was actually checked gets a short, varied "good job" (REIGNS_VOICE_PRAISE=0 to
turn off; at most one per PRAISE_GAP_S).

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
import random
import re
from collections import deque
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

# Openers for warnings: the summary must start with one, so the horse doesn't say the same
# greeting every time. Recently used ones are skipped.
OPENERS = {
    "cowboy": [
        "Whoa there, partner.", "Hold your horses.", "Easy now.", "Well, shoot.",
        "Hang on a second, friend.", "Hoo boy.", "Now wait just a minute.", "Heads up, cowpoke.",
        "Rein it in a sec.", "Hold up there.", "Well, I'll be.", "Hate to spook ya, but",
    ],
    "plain": [
        "Heads up.", "Hang on.", "Quick note.", "Hold on a second.", "Just so you know,",
        "Careful here.", "One thing:", "Wait a moment.",
    ],
}
# Said when a reply was checked and nothing was wrong. Pre-generated at startup, so instant.
PRAISE_LINES = {
    "cowboy": [
        "That one checks out, partner.", "Clean as a whistle.", "Looks good to me, friend.",
        "Straight shooter, that one.", "All clear on this trail.", "Yep, that's the real deal.",
        "Checked it. Solid as a fence post.", "Good answer, no funny business.",
        "Can't find a thing wrong. Nice.", "Smooth ride so far.", "That's right as rain.",
        "Good work, Claude.",
    ],
    "plain": [
        "That one checks out.", "Looks good.", "All clear.", "Verified, nice.",
        "No problems found.", "Good answer.", "Checked it, all accurate.", "Nothing wrong there.",
    ],
}
# Spanish (REIGNS_LANGUAGE=es or session.start language "es"). ElevenLabs' flash v2.5 model
# speaks Spanish with the same voice.
OPENERS_ES = {
    "cowboy": [
        "¡Epa, compañero!", "Aguanta los caballos.", "Tranquilo, amigo.", "Uy, uy, uy.",
        "Espérate tantito.", "Ojo, vaquero.", "A ver, a ver.", "Frena un segundo.", "Híjole.",
        "No tan rápido, amigo.", "Detente ahí, compadre.", "Mira nada más.",
    ],
    "plain": [
        "Atención.", "Un momento.", "Ojo.", "Espera.", "Cuidado aquí.", "Una cosa:",
        "Fíjate en esto.", "Aguarda un segundo.",
    ],
}
PRAISE_LINES_ES = {
    "cowboy": [
        "Esa está bien, compañero.", "Limpiecito, sin trampa.", "Todo en orden por aquí.",
        "Eso es cierto, amigo.", "Respuesta derechita.", "Revisado y bien firme.",
        "Buen trabajo, Claude.", "Ni una falla. ¡Bien!", "Camino despejado.",
        "Así se hace, vaquero.", "Todo cuadra, compadre.", "Bien dicho, amigo.",
    ],
    "plain": [
        "Todo correcto.", "Se ve bien.", "Verificado, bien.", "Sin problemas.",
        "Buena respuesta.", "Nada que corregir.", "Revisado, todo exacto.", "Todo en orden.",
    ],
}
# Marley the unicorn (session.start character "marley"): bubbly and a little magical.
OPENERS["unicorn"] = [
    "Oh my stars!", "Sparkle check!", "Eek, wait a sec!", "Oopsie, heads up!", "Glitter alert!",
    "Wait, wait, wait!", "Hmm, something's not so magical here.", "Hold your rainbows.",
    "Uh-oh, friend.", "Pause the sparkles.", "Oh dear!", "Psst, heads up!",
]
PRAISE_LINES["unicorn"] = [
    "Sparkly clean!", "That one's pure magic.", "All checks out, friend!", "Rainbows all around!",
    "Totally true, love it.", "Shiny and correct!", "Magic! No mistakes.", "That's the real deal, yay!",
    "Glitter-approved!", "Nothing fishy there!", "Perfectly true, woohoo!", "Great job, Claude!",
]
OPENERS_ES["unicorn"] = [
    "¡Ay, mis estrellitas!", "¡Revisión de brillitos!", "¡Uy, un momentito!", "¡Ups, atención!",
    "¡Alerta de purpurina!", "¡Espera, espera!", "Mmm, aquí algo no es tan mágico.",
    "Guarda tus arcoíris.", "Ay, amiguito.", "Pausa a los brillos.", "¡Ay, no!", "Psst, ojo.",
]
PRAISE_LINES_ES["unicorn"] = [
    "¡Limpiecito y brillante!", "Eso es pura magia.", "¡Todo cuadra, amiguito!",
    "¡Arcoíris para todos!", "Totalmente cierto, me encanta.", "¡Brillante y correcto!",
    "¡Magia! Ni un error.", "¡Eso es de verdad, yupi!", "¡Aprobado con purpurina!",
    "¡Nada raro por aquí!", "Perfectamente cierto, ¡bien!", "¡Buen trabajo, Claude!",
]
OPENERS_BY_LANG = {"en": OPENERS, "es": OPENERS_ES}
PRAISE_BY_LANG = {"en": PRAISE_LINES, "es": PRAISE_LINES_ES}
RECOVERED = {
    ("en", "cowboy"): "Yeehaw, fixed it, partner!", ("en", "plain"): "Fixed it!",
    ("es", "cowboy"): "¡Yija! Arreglado, compañero.", ("es", "plain"): "¡Arreglado!",
    ("en", "unicorn"): "Yay, all fixed! Sparkles!", ("es", "unicorn"): "¡Yupi, todo arreglado! ¡Brillitos!",
}
# Which pet is on screen (session.start "character") → its personality. Anything else = the horse.
UNICORN_NAMES = ("marley", "unicorn")
LANGUAGES = ("en", "es")
PRAISE_GAP_S = 45.0  # praise at most this often, so it stays nice instead of naggy

_transport: Optional[httpx.AsyncBaseTransport] = None  # tests inject a MockTransport


def enabled() -> bool:
    return bool(os.environ.get("ELEVENLABS_API_KEY") and os.environ.get("REIGNS_VOICE_ID"))


def persona(character: Optional[str] = None) -> str:
    """'unicorn' for Marley; otherwise the horse's REIGNS_VOICE_STYLE (cowboy or plain)."""
    if (character or "").strip().lower() in UNICORN_NAMES:
        return "unicorn"
    return style()


def voice_id(p: Optional[str] = None) -> str:
    """The unicorn has her own ElevenLabs voice (REIGNS_UNICORN_VOICE_ID); falls back to the
    horse's REIGNS_VOICE_ID so she still talks if it isn't set."""
    if p == "unicorn" and os.environ.get("REIGNS_UNICORN_VOICE_ID"):
        return os.environ["REIGNS_UNICORN_VOICE_ID"]
    return os.environ.get("REIGNS_VOICE_ID", "")


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


def language(lang: Optional[str] = None) -> str:
    """'en' or 'es': the session's choice, else REIGNS_LANGUAGE, else English."""
    v = (lang or os.environ.get("REIGNS_LANGUAGE") or "en").strip().lower()[:2]
    return v if v in LANGUAGES else "en"


def praise_enabled() -> bool:
    return (os.environ.get("REIGNS_VOICE_PRAISE") or "1").strip() not in ("0", "false", "off", "no")


def _pick(options: list[str], recent: Optional[deque] = None) -> str:
    """Random choice that avoids the last few used (so no back-to-back repeats)."""
    fresh = [o for o in options if not recent or o not in recent] or options
    choice = random.choice(fresh)
    if recent is not None:
        recent.append(choice)
    return choice


def pick_opener(recent: Optional[deque] = None, lang: Optional[str] = None,
                p: Optional[str] = None) -> str:
    return _pick(OPENERS_BY_LANG[language(lang)][p or style()], recent)


def pick_praise(recent: Optional[deque] = None, lang: Optional[str] = None,
                p: Optional[str] = None) -> str:
    return _pick(PRAISE_BY_LANG[language(lang)][p or style()], recent)


def recovered_line(lang: Optional[str] = None, p: Optional[str] = None) -> str:
    return RECOVERED[(language(lang), p or style())]


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


def spanish_fallback(bubble: BubbleContent) -> str:
    """When the Spanish summary can't be made, never read the (English) bubble aloud: say, in
    Spanish, how many problems there are and point to the bubble."""
    n = len(bubble.problems)
    if n <= 1:
        return "Encontré un problema en esta respuesta. Revisa la burbuja para ver los detalles."
    return f"Encontré {n} problemas en esta respuesta. Revisa la burbuja para ver los detalles."


def _new_problem_ids(bubble: BubbleContent, spoken_ids: set[str]) -> set[str]:
    return {p.claim_id for p in bubble.problems} - spoken_ids


def line_for(
    prev_level: int,
    level: int,
    bubble: BubbleContent,
    recovered: bool,
    spoken_ids: Optional[set[str]] = None,
    opener: Optional[str] = None,
    lang: Optional[str] = None,
    p: Optional[str] = None,
) -> Optional[str]:
    p = p or style()
    spanish = language(lang) == "es"
    if recovered:
        return recovered_line(lang, p)
    if mode() == "short":
        if level >= 3 and level > prev_level and bubble.headline:
            return spanish_fallback(bubble) if spanish else clip_words(bubble.headline)
        return None
    # full mode
    if level < 1 or not bubble.headline:
        return None
    rose_to_alarm = level >= 3 and level > prev_level
    if _new_problem_ids(bubble, spoken_ids or set()) or rose_to_alarm:
        text = spanish_fallback(bubble) if spanish else full_line(bubble)
        if p in ("cowboy", "unicorn"):
            first = opener or OPENERS_BY_LANG[language(lang)][p][0]
            return clip_sentences(f"{first} {text}", FULL_MAX_WORDS)
        return text
    return None


_SUMMARY_SYSTEM = {
    "cowboy": (
        "You are Reigns, a friendly cowboy horse who watches over someone's chat with Claude and "
        "speaks out loud when Claude gets something wrong. Say it like a warm, laid-back cowboy "
        "talking to a friend: natural, relaxed, a little folksy ('I reckon', 'mighty', 'y'all' "
        "now and then) but never cartoonish, and keep the facts exact. Don't say 'howdy', and "
        "use 'partner' at most once."
    ),
    "plain": (
        "You are Reigns, a friendly assistant pet that speaks out loud when Claude gets something "
        "wrong in a chat. Sound natural and conversational, like a helpful friend."
    ),
    "unicorn": (
        "You are Marley, a cheerful, sparkly unicorn who watches over someone's chat with Claude "
        "and speaks out loud when Claude gets something wrong. Sound bubbly, warm and upbeat, a "
        "little magical (one touch of sparkles, rainbows or stars per reply at most) but never "
        "babyish or cartoonish, and keep the facts exact. Never say 'partner' or 'howdy'."
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


_SPANISH = (
    " Your ENTIRE reply must be in natural Latin American Spanish, even though the notes are in "
    "English (translate them; keep names, numbers and titles exact; no English sentences). "
    "For the cowboy style, use Spanish folksy words like 'compañero', 'amigo' or 'híjole' now and then instead of English ones; for the unicorn, "
    "keep it bubbly ('amiguito', 'brillitos') without overdoing it."
)


async def summarize(
    bubble: BubbleContent, opener: Optional[str] = None, lang: Optional[str] = None,
    p: Optional[str] = None,
) -> Optional[str]:
    """Natural spoken summary of the whole bubble via the fast model. None if unavailable/slow."""
    try:
        from app import llm
        if not llm.llm_available():
            return None
        raw = await asyncio.wait_for(
            llm.complete_text(
                _SUMMARY_SYSTEM[p or style()],
                _SUMMARY_RULES.format(n=SUMMARY_MAX_WORDS)
                + (_SPANISH if language(lang) == "es" else "")
                + (f' Start with exactly these words: "{opener}"' if opener else "")
                + "\n\n" + _summary_input(bubble),
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


def _cache_path(text: str, vid: Optional[str] = None) -> Path:
    vid = vid or os.environ.get("REIGNS_VOICE_ID")
    key = hashlib.sha1(f"{vid}|{_model()}|{_speed()}|{_stability()}|{text}".encode())
    return CACHE_DIR / f"{key.hexdigest()}.mp3"


async def synthesize(text: str, vid: Optional[str] = None) -> Optional[bytes]:
    """MP3 bytes for `text` in voice `vid` (default: the horse's), from the disk cache or
    ElevenLabs. None if voice is off/fails."""
    if not enabled() or not text:
        return None
    vid = vid or os.environ["REIGNS_VOICE_ID"]
    path = _cache_path(text, vid)
    if path.exists():
        return path.read_bytes()
    url = API_URL.format(voice_id=vid)
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


def warm_lines(p: Optional[str] = None) -> list[str]:
    """Lines pre-generated at startup for one pet: (the horse's common warnings) + its praise and
    "fixed it" in every language, so switching pet or language is instant too."""
    p = p or style()
    lines = list(COMMON_LINES) if p != "unicorn" else []
    for lang in LANGUAGES:
        lines += PRAISE_BY_LANG[lang][p] + [RECOVERED[(lang, p)]]
    return list(dict.fromkeys(lines))


async def warm_cache() -> None:
    """Pre-generate COMMON_LINES in the background at startup (FR-V2). Never raises."""
    if not enabled():
        log.info("voice disabled (no ELEVENLABS_API_KEY / REIGNS_VOICE_ID)")
        return
    made = 0
    jobs = [(line, voice_id(p)) for p in (style(), "unicorn") for line in warm_lines(p)]
    for line, vid in jobs:
        try:
            if not _cache_path(line, vid).exists() and await synthesize(line, vid):
                made += 1
        except Exception as exc:  # pragma: no cover - belt and braces
            log.warning("voice warm-up failed on %r: %s", line, exc)
        await asyncio.sleep(0)  # stay polite to the event loop
    log.info("voice cache ready", extra={"new_lines": made, "total": len(COMMON_LINES)})


class VoiceState:
    """Per-session rate limit (≤ 1 line / 20 s) + which problems were already read out."""

    def __init__(self) -> None:
        self.last_spoken = -MIN_GAP_S
        self.last_praise = -PRAISE_GAP_S
        self.spoken_ids: set[str] = set()
        self.recent_openers: deque = deque(maxlen=4)
        self.recent_praise: deque = deque(maxlen=4)


async def maybe_speak(
    state: VoiceState,
    prev_level: int,
    level: int,
    bubble: BubbleContent,
    recovered: bool,
    now: Optional[float] = None,
    clean: bool = False,
    lang: Optional[str] = None,
    character: Optional[str] = None,
) -> Optional[VoicePlay]:
    """`clean`: this reply was checked (≥ 1 claim) and nothing in it was red or amber."""
    if not enabled():
        return None
    now = time.monotonic() if now is None else now
    lang, p = language(lang), persona(character)
    opener = (pick_opener(state.recent_openers, lang, p)
              if p in ("cowboy", "unicorn") or mode() == "full" else None)
    text = line_for(prev_level, level, bubble, recovered, state.spoken_ids, opener, lang, p)
    praising = False
    if (not text and clean and praise_enabled() and not recovered
            and now - state.last_praise >= PRAISE_GAP_S):
        text, praising = pick_praise(state.recent_praise, lang, p), True
    if not text or now - state.last_spoken < MIN_GAP_S:
        return None
    prev_spoken, state.last_spoken = state.last_spoken, now  # hold the slot while we work
    if praising:
        state.last_praise = now
        log.info("voice: praising a clean reply")
    elif mode() == "full" and not recovered:
        summary = await summarize(bubble, opener, lang, p)
        log.info("voice: speaking %s (%d words)", "summary" if summary else "bubble text",
                 len((summary or text).split()))
        text = summary or text
    try:
        audio = await synthesize(text, voice_id(p))
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
