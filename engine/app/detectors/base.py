"""Shared foundation for Role C's detectors (PRD §12.4, FR-C5). Owner: Role C.

Why this file exists
--------------------
Every detector has to follow the same rules (PRD §12.4 + FR-C5):
  1. expose `name` and `async def check(claim, session) -> DetectorResult`
  2. NEVER raise — any failure becomes `status: "error"`
  3. report `latency_ms`
  4. cache identical lookups for the session (in `session.cache`)
  5. stay inside the engine's timeout

Instead of re-implementing those rules four times, each detector subclasses `BaseDetector`
and only writes `_check()` — the actual checking logic. `check()` (the public method the
engine calls through app/plugins.py) wraps `_check()` with timing and error handling.

The data models (Claim, DetectorResult, Evidence, SessionContext) are NOT redefined here:
Role B owns them in app/models.py (the §12 contract) and we import them, so there is exactly
one definition of the contract in the codebase.

Usage (e.g. app/detectors/reference_auditor.py):

    from app.detectors.base import BaseDetector, http_client, similarity

    class ReferenceAuditor(BaseDetector):
        name = "reference_auditor"

        async def _check(self, claim, session):
            data = await self.cached(session, f"crossref:{title}", lambda: fetch(title))
            ...
            return self.result("supported", 0.95, "Found in Crossref.", evidence=[...])

    detector = ReferenceAuditor()   # module-level instance the engine looks for
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import unicodedata
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from difflib import SequenceMatcher
from typing import Any, ClassVar, Protocol, runtime_checkable

import httpx

from app.llm import LLMError
from app.models import (
    Claim,
    DetectorName,
    DetectorResult,
    Evidence,
    SessionContext,
    VerdictStatus,
)

log = logging.getLogger("reigns.detectors")

# Individual network calls must finish well inside the engine's 12 s per-detector budget
# (FR-B5), so one slow API can't eat the whole budget. FR-C1 specifies 5 s for URL checks.
HTTP_TIMEOUT_S = 5.0


# Crossref gives faster, more reliable service to clients that identify themselves with a
# contact email ("polite pool"). Set CROSSREF_MAILTO in .env. Read at call time (not import
# time) so it works however the engine loads its environment.
def user_agent() -> str:
    mailto = os.environ.get("CROSSREF_MAILTO", "").strip()
    return "Reigns/1.0 (ShellHacks hallucination detector" + (
        f"; mailto:{mailto})" if mailto else ")"
    )


# Max characters for an evidence snippet — keeps the bubble / Details panel readable.
SNIPPET_MAX = 300


# ---------------------------------------------------------------------------
# The interface (§12.4). Anything with `name` + async `check` satisfies it — the engine
# doesn't require subclassing BaseDetector, but all Role C detectors should.
# ---------------------------------------------------------------------------
@runtime_checkable
class Detector(Protocol):
    name: str

    async def check(self, claim: Claim, session: SessionContext) -> DetectorResult: ...


class BaseDetector(ABC):
    """Subclass this, set `name`, implement `_check()`. `check()` handles the rules."""

    # ClassVar so pydantic-style tools / type checkers treat it as a class constant, and the
    # Literal type means a typo like "reference_auditer" is caught by the type checker.
    name: ClassVar[DetectorName]

    # ---- public entry point (called by app/plugins.py) -------------------------------------
    async def check(self, claim: Claim, session: SessionContext) -> DetectorResult | None:
        t0 = time.perf_counter()
        try:
            result = await self._check(claim, session)
        except asyncio.CancelledError:
            # The engine cancels us when its timeout fires (asyncio.wait_for). Cancellation must
            # propagate — swallowing it would make wait_for hang. plugins.py turns it into
            # status "error" for us.
            raise
        except LLMError as exc:
            # Expected failure mode (no API key, rate limit, unparseable JSON): no stack trace.
            log.warning("%s: LLM failure on claim %s: %s", self.name, claim.claim_id, exc)
            result = self.error(f"the AI judge was unavailable ({exc})")
        except Exception as exc:  # FR-C5: never raise
            log.exception("%s crashed on claim %s", self.name, claim.claim_id)
            result = self.error(f"{type(exc).__name__}: {exc}")

        if result is None:
            # "Nothing to say about this claim" (e.g. memory_consistency with no relevant
            # memory). The engine skips None results, so the verdict is left unchanged.
            return None
        # Always stamp latency here so no detector forgets to (§12.4 latency_ms).
        result.latency_ms = int((time.perf_counter() - t0) * 1000)
        return result

    @abstractmethod
    async def _check(self, claim: Claim, session: SessionContext) -> DetectorResult | None:
        """The detector's real logic. May raise — check() converts exceptions to 'error'.
        May return None for "nothing to say" (the engine then ignores this detector)."""

    # ---- result builders --------------------------------------------------------------------
    def result(
        self,
        status: VerdictStatus,
        confidence: float,
        explanation: str,
        evidence: list[Evidence] | None = None,
    ) -> DetectorResult:
        """Build a DetectorResult with this detector's name and a clamped confidence."""
        return DetectorResult(
            detector=self.name,
            status=status,
            confidence=max(0.0, min(1.0, float(confidence))),
            evidence=evidence or [],
            explanation=explanation.strip(),
            latency_ms=0,  # overwritten in check()
        )

    def error(self, message: str) -> DetectorResult:
        """status 'error' → aggregation treats the claim as skipped for this detector (§8.4)."""
        return self.result("error", 0.0, f"Check could not run: {message}")

    # ---- per-session cache (FR-C5) -------------------------------------------------------------
    async def cached(
        self,
        session: SessionContext,
        key: str,
        factory: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Return session.cache[<detector>:<key>], computing it with `factory()` on a miss.

        - Keys are namespaced by detector name so two detectors can't collide.
        - Only successful results are stored: if factory() raises, nothing is cached and the
          next claim retries (a network blip shouldn't poison the whole session).
        - This also makes detectors deterministic given cached lookups (§12.4 rule).
        """
        full_key = f"{self.name}:{key}"
        if full_key in session.cache:
            return session.cache[full_key]
        value = await factory()
        session.cache[full_key] = value
        return value


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def http_client(**overrides: Any) -> httpx.AsyncClient:
    """A configured httpx client. Use as `async with http_client() as client:`.

    A new client per use (instead of one global client) is deliberate: an AsyncClient is tied
    to the event loop it was created on, and tests create a fresh loop per test.
    """
    opts: dict[str, Any] = {
        "timeout": HTTP_TIMEOUT_S,
        "follow_redirects": True,
        "headers": {"User-Agent": user_agent()},
    }
    opts.update(overrides)
    return httpx.AsyncClient(**opts)


_PUNCT = re.compile(r"[^\w\s]")
_SPACES = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace.

    "Attention Is All You Need!" → "attention is all you need"
    "Jógvan Poulsen"             → "jogvan poulsen"
    """
    # NFKD splits "ó" into "o" + a combining accent; dropping combining marks removes accents.
    decomposed = unicodedata.normalize("NFKD", text)
    no_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    no_punct = _PUNCT.sub(" ", no_accents.lower()).replace("_", " ")
    return _SPACES.sub(" ", no_punct).strip()


def similarity(a: str, b: str) -> float:
    """0.0–1.0 similarity of two strings after normalize_text (FR-C1 title match ≥ 0.85)."""
    na, nb = normalize_text(a), normalize_text(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


# Words that carry no meaning in a paper title; ignored by word_overlap().
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "of",
        "for",
        "in",
        "on",
        "with",
        "and",
        "to",
        "by",
        "via",
        "using",
        "based",
        "is",
        "are",
        "we",
        "from",
        "at",
        "as",
        "its",
        "towards",
        "toward",
        "into",
        "over",
        "under",
    ]
)


def content_words(text: str) -> set[str]:
    return {w for w in normalize_text(text).split() if w not in _STOPWORDS}


def word_overlap(a: str, b: str) -> float:
    """Jaccard overlap of meaningful words (0.0–1.0).

    Complements similarity(): two titles can share many letters/short words and still be
    different papers. "HiveFormer: Attention-Based Acoustic Monitoring of Beehives" vs
    "MUS-Tracker: An IoT Based System … Monitoring of Beehives" → similarity 0.64 but
    word_overlap 0.20.
    """
    wa, wb = content_words(a), content_words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def snippet(text: str, limit: int = SNIPPET_MAX) -> str:
    """Single-line, length-capped evidence snippet."""
    flat = _SPACES.sub(" ", text).strip()
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# Is this a checkable fact at all? (long-chat false alarms, Assurant feedback)
# ---------------------------------------------------------------------------
_IMPERATIVE = re.compile(
    r"^\s*(?:(?:just|simply|then|first|next|now|also|so|instead|finally|and|or)\s+)*"
    r"(read|use|set|call|sleep|add|remove|install|run|create|make|write|try|check|store|save|"
    r"load|pass|return|import|open|replace|wrap|put|keep|change|update|configure|define|send|"
    r"fetch|loop|batch|avoid|consider|switch|start|stop|collect|build|move|drop|fill|sort|"
    r"deploy|upload|copy|paste|click|go|let's|lets|note|remember|make sure|don't|do not)\b",
    re.IGNORECASE,
)
_HERE_IS = re.compile(
    r"^\s*(here(?:'s| is| are)|below is|this (?:code|snippet|script))\b", re.IGNORECASE
)


def looks_like_instruction(text: str) -> bool:
    """Advice / instructions / code intros are not facts a source can confirm.

    "Sleep 0.6 seconds between calls…", "Read it from the response and fall back to 60 seconds
    if it's missing:" — the long-chat replay showed these being fact-checked, coming back
    "unverified", and (via the Consistency Probe asked out of context) even red. Checking them
    only produces false alarms, so detectors that search the world abstain on them.
    """
    t = (text or "").strip()
    return bool(t) and (bool(_IMPERATIVE.match(t)) or bool(_HERE_IS.match(t)) or t.endswith(":"))
