"""Truth Ledger — per-user memory of CONFIRMED facts from the user's chats. Owner: Role C.

Why this exists
---------------
Our detectors check each reply against the outside world (papers, Wikipedia, PyPI, library
signatures). They can't see drift INSIDE a conversation: the AI forgetting a constraint the user
gave ("our API allows 100 requests/min") or contradicting a fact that was already verified. The
Truth Ledger stores short "memory cards" so later replies can be checked against them
(detectors/memory_consistency.py), Fix-it prompts can remind the AI of them, and "Start fresh"
can hand a clean summary to a new chat. Also provides `embed()` for FR-L3 experience memory
(app/learning/store.py already calls it for `cases`).

The one rule: only CONFIRMED facts go in (PRD FR-L5 applied to memory)
----------------------------------------------------------------------
A memory that saves what the AI says would save its hallucinations and make them permanent.
So a card can only come from:
    user_fact / constraint   something the USER said (about themselves, their project, rules)
    verified_fact            a claim our detectors marked SUPPORTED with external evidence
    correction               a claim our detectors CONTRADICTED with external evidence
Unverified AI claims are never stored.

Storage
-------
Local SQLite (engine/reigns_memory.db, git-ignored — PRD §18 "conversations stay local") is the
source of truth. Optional mirror to MongoDB Atlas `user_memory` when REIGNS_MEMORY_SYNC=1 and
MONGODB_URI are set (opt-in, because card text is derived from the user's own messages).

Retrieval
---------
A user's ledger is small (dozens–hundreds of cards), so we load it and rank in memory: cosine
similarity when embeddings exist (Voyage AI, VOYAGE_API_KEY), otherwise word overlap. Fast,
offline-capable, and nothing to break at the venue.

Public API (used by plugins/session hooks, detectors, Course Correct, try_it.py)
    user_id_for(session)                         → str
    await on_user_message(session, message)       extract + store user cards
    await on_verdicts(session, claims, verdicts)  store verified facts / corrections
    await relevant(user_id, text, k=5)            → list[(MemoryCard, score)]
    await list_cards(user_id) / delete_card(user_id, card_id)
    await ledger_summary(user_id)                 → text block for "Start fresh"
    await embed(text)                             → list[float] | None   (FR-L3)
Every function here is best-effort: it never raises into the engine.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import unicodedata
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

import aiosqlite
import httpx

from app.detectors.base import _STOPWORDS as _STOP
from app.detectors.base import normalize_text
from app.llm import LLMError, complete_json, fast_model_name, llm_available
from app.models import Claim, ClaimVerdict, SessionContext, utc_now_iso

log = logging.getLogger("reigns.memory")

CardKind = Literal["user_fact", "constraint", "verified_fact", "correction"]
USER_KINDS = ("user_fact", "constraint")

MAX_CARD_WORDS = 30  # cards are short on purpose: easy to read, cheap to put in prompts
MAX_CARDS_PER_MESSAGE = 5
MIN_USER_TEXT = 12  # characters; shorter messages ("ok", "thanks") carry no facts
MAX_USER_TEXT = 1500  # longer = a pasted document (FR-B11) — not a statement about the user

# Dedupe: a new card this similar to an existing one refreshes it instead of being added.
DUP_COSINE = 0.92
DUP_WORDS = 0.8
# Retrieval: below these a card is "not about this claim" and isn't returned.
MIN_COSINE = 0.35
MIN_WORDS = 0.12

VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
EMBED_MODEL = os.environ.get("REIGNS_EMBED_MODEL", "voyage-3.5-lite")
EMBED_TIMEOUT_S = 4.0

# Cheap pre-filter so we don't pay for an LLM call on every "who was X?" question: user facts
# and constraints nearly always use first person, obligation words, or specific tool/version
# names.
_WORTH_EXTRACTING = re.compile(
    r"\b(i|i'm|im|i've|my|mine|me|we|we're|our|ours|us|must|should|need|needs|don't|do not|"
    r"never|always|only|can't|cannot|using|use|uses|prefer|limit|budget|deadline|version|v\d)\b",
    re.IGNORECASE,
)

EXTRACT_SYSTEM = """You maintain a short memory of FACTS THE USER STATED in a chat with an AI
assistant, so the assistant can be checked later for forgetting or contradicting them.

From the USER MESSAGE, extract only durable facts the user states about themselves, their
project, their setup, or rules the answer must follow. Two kinds:
- "constraint": a rule/requirement the AI's answers must respect
  (e.g. "API rate limit is 100 requests per minute", "Must not use external libraries",
  "Budget is under $500")
- "user_fact": a fact about the user's situation/setup
  (e.g. "Uses pandas 1.5 on Python 3.8", "Project is a Flask app deployed on AWS Lambda")

Rules:
- Only what the USER asserts. Questions are not facts ("what is X?" → nothing).
- Each card: one standalone sentence, at most 20 words, understandable without the chat.
- "subject": 2–5 lowercase words naming WHAT the fact is about ("api rate limit",
  "pandas version") — used to replace old cards when the user changes a fact.
- At most 5 cards. If there is nothing durable, return an empty list.

Return {"cards": [{"kind": "constraint|user_fact", "text": "...", "subject": "..."}]}"""


# =============================================================================================
# Data
# =============================================================================================
@dataclass
class MemoryCard:
    card_id: str
    user_id: str
    kind: CardKind
    text: str
    subject: str = ""
    source: str = ""  # "user_message" | "claim_verifier" | "reference_auditor" | …
    confidence: float = 1.0
    embedding: list[float] | None = None
    created_at: str = ""
    last_used_at: str = ""
    uses: int = 0
    superseded_by: str | None = None

    @property
    def active(self) -> bool:
        return self.superseded_by is None

    def public(self) -> dict[str, Any]:
        """JSON-safe view without the (large) embedding, for APIs / the companion panel."""
        d = asdict(self)
        d.pop("embedding", None)
        return d


def user_id_for(session: SessionContext | None) -> str:
    """Anonymous per-device profile. The companion may send one (Keychain UUID); until then,
    REIGNS_USER or "local" — so everything works with no accounts at all."""
    uid = getattr(session, "user_id", None) if session is not None else None
    return str(uid or os.environ.get("REIGNS_USER") or "local")


def clean_text(text: str) -> str:
    words = re.sub(r"\s+", " ", text).strip().split(" ")
    out = " ".join(words[:MAX_CARD_WORDS])
    return out if out.endswith((".", "!", "?")) else out + "."


def _subject_key(subject: str) -> str:
    return normalize_text(subject)


# =============================================================================================
# Similarity
# =============================================================================================
def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


# A number with optional ".digits" parts ("100", "1.5", "3.8.2"), or a run of letters.
# A trailing sentence period isn't matched because it isn't followed by a digit.
_TOKEN = re.compile(r"\d+(?:\.\d+)*|[^\W\d_]+")


def tokens(text: str) -> set[str]:
    """Meaningful lowercase tokens, keeping version-like numbers whole ("1.5", "3.8.2").

    base.content_words() splits "1.5" into "1" and "5", which makes versions and limits —
    exactly what this ledger is about — match weakly.
    """
    decomposed = unicodedata.normalize("NFKD", text.lower())
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return {t for t in _TOKEN.findall(plain) if t not in _STOP}


def word_sim(a: str, b: str) -> float:
    """Jaccard overlap of meaningful words; numbers count too ("100", "1.5")."""
    wa, wb = tokens(a), tokens(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def similarity(card: MemoryCard, text: str, text_emb: list[float] | None) -> tuple[float, bool]:
    """(score, used_embeddings). Embedding cosine when both sides have one, else words."""
    if text_emb and card.embedding:
        return cosine(card.embedding, text_emb), True
    return word_sim(card.text, text), False


# =============================================================================================
# Embeddings (FR-L3) — optional; everything works without them
# =============================================================================================
async def embed(text: str) -> list[float] | None:
    """Voyage AI embedding for `text`, or None (no key / error). Never raises."""
    key = os.environ.get("VOYAGE_API_KEY")
    if not key or not text.strip():
        return None
    try:
        async with httpx.AsyncClient(timeout=EMBED_TIMEOUT_S) as client:
            resp = await client.post(
                VOYAGE_URL,
                headers={"Authorization": f"Bearer {key}"},
                json={"input": [text[:2000]], "model": EMBED_MODEL},
            )
            resp.raise_for_status()
            vec = resp.json()["data"][0]["embedding"]
            return [float(x) for x in vec]
    except Exception as exc:  # noqa: BLE001 — embeddings are optional; fall back to words
        log.warning("embedding failed (falling back to word overlap): %r", exc)
        return None


# =============================================================================================
# Storage
# =============================================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_cards (
  card_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
  subject TEXT, source TEXT, confidence REAL, embedding TEXT, created_at TEXT,
  last_used_at TEXT, uses INTEGER DEFAULT 0, superseded_by TEXT);
CREATE INDEX IF NOT EXISTS idx_memory_user ON memory_cards(user_id);
"""
_COLS = [
    "card_id",
    "user_id",
    "kind",
    "text",
    "subject",
    "source",
    "confidence",
    "embedding",
    "created_at",
    "last_used_at",
    "uses",
    "superseded_by",
]


def default_path() -> str:
    return os.environ.get(
        "REIGNS_MEMORY_DB", str(Path(__file__).resolve().parents[2] / "reigns_memory.db")
    )


class MemoryStore:
    """SQLite-backed card store. One short-lived connection per call: tiny volumes, no pooling
    to manage, and safe across the engine's event loop and tests' fresh loops."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path or default_path()
        self._ready = False
        self._lock = asyncio.Lock()

    async def _conn(self) -> aiosqlite.Connection:
        db = await aiosqlite.connect(self.path)
        if not self._ready:
            await db.executescript(SCHEMA)
            await db.commit()
            self._ready = True
        return db

    @staticmethod
    def _row(row: tuple) -> MemoryCard:
        d = dict(zip(_COLS, row))
        d["embedding"] = json.loads(d["embedding"]) if d["embedding"] else None
        return MemoryCard(**d)

    async def all(self, user_id: str, include_superseded: bool = False) -> list[MemoryCard]:
        db = await self._conn()
        try:
            sql = f"SELECT {', '.join(_COLS)} FROM memory_cards WHERE user_id = ?"
            if not include_superseded:
                sql += " AND superseded_by IS NULL"
            async with db.execute(sql + " ORDER BY created_at", (user_id,)) as cur:
                return [self._row(r) for r in await cur.fetchall()]
        finally:
            await db.close()

    async def upsert(self, card: MemoryCard) -> None:
        values = [getattr(card, c) for c in _COLS]
        values[_COLS.index("embedding")] = json.dumps(card.embedding) if card.embedding else None
        db = await self._conn()
        try:
            await db.execute(
                f"INSERT OR REPLACE INTO memory_cards ({', '.join(_COLS)}) "
                f"VALUES ({', '.join('?' * len(_COLS))})",
                values,
            )
            await db.commit()
        finally:
            await db.close()

    async def delete(self, user_id: str, card_id: str) -> bool:
        db = await self._conn()
        try:
            cur = await db.execute(
                "DELETE FROM memory_cards WHERE user_id = ? AND card_id = ?", (user_id, card_id)
            )
            await db.commit()
            return cur.rowcount > 0
        finally:
            await db.close()

    async def touch(self, card_ids: list[str]) -> None:
        if not card_ids:
            return
        db = await self._conn()
        try:
            await db.executemany(
                "UPDATE memory_cards SET uses = uses + 1, last_used_at = ? WHERE card_id = ?",
                [(utc_now_iso(), cid) for cid in card_ids],
            )
            await db.commit()
        finally:
            await db.close()


_store: MemoryStore | None = None


def get_store() -> MemoryStore:
    global _store
    if _store is None or _store.path != default_path():
        _store = MemoryStore()
    return _store


def set_store(store: MemoryStore | None) -> None:
    """Tests (and try_it.py) point the module at their own database."""
    global _store
    _store = store


async def _mirror(card: MemoryCard) -> None:
    """Opt-in copy to Atlas `user_memory` (REIGNS_MEMORY_SYNC=1). Never raises."""
    if os.environ.get("REIGNS_MEMORY_SYNC") != "1":
        return
    try:
        from app.learning import store as atlas

        db = atlas.get_db()
        if db is None:
            return
        doc = {**card.public(), "_id": card.card_id}
        if card.embedding:
            doc["embedding"] = card.embedding
        await db.user_memory.replace_one({"_id": card.card_id}, doc, upsert=True)
    except Exception as exc:  # noqa: BLE001 — cloud mirror is best-effort
        log.warning("user_memory mirror failed: %r", exc)


# =============================================================================================
# Adding cards (dedupe + supersede)
# =============================================================================================
async def add_card(
    user_id: str,
    kind: CardKind,
    text: str,
    subject: str = "",
    source: str = "",
    confidence: float = 1.0,
    store: MemoryStore | None = None,
) -> tuple[MemoryCard, str]:
    """Store a card. Returns (card, action) with action in {"added", "refreshed", "replaced"}.

    - refreshed: an active card already says the same thing → bump its last_used, no duplicate
    - replaced : a user card with the same subject says something different → the old one is
                 marked superseded (so "limit is now 200/min" doesn't clash with "100/min")
    """
    store = store or get_store()
    async with store._lock:  # serialize adds so two turns can't create twin cards
        text = clean_text(text)
        emb = await embed(text)
        existing = await store.all(user_id)
        now = utc_now_iso()

        for old in existing:
            sim, used_emb = similarity(old, text, emb)
            if sim >= (DUP_COSINE if used_emb else DUP_WORDS):
                await store.touch([old.card_id])
                return old, "refreshed"

        card = MemoryCard(
            card_id=str(uuid.uuid4()),
            user_id=user_id,
            kind=kind,
            text=text,
            subject=_subject_key(subject),
            source=source,
            confidence=max(0.0, min(1.0, confidence)),
            embedding=emb,
            created_at=now,
            last_used_at=now,
        )
        action = "added"
        if card.subject and kind in USER_KINDS:
            for old in existing:
                if old.kind in USER_KINDS and old.subject == card.subject:
                    old.superseded_by = card.card_id
                    await store.upsert(old)
                    action = "replaced"
        await store.upsert(card)
    asyncio.ensure_future(_mirror(card))
    return card, action


# =============================================================================================
# Extraction
# =============================================================================================
async def extract_user_cards(text: str, context: str = "", judge=None) -> list[dict[str, str]]:
    """LLM (fast model) → [{"kind", "text", "subject"}]. [] when there's nothing durable."""
    text = text.strip()
    if not (MIN_USER_TEXT <= len(text) <= MAX_USER_TEXT) or not _WORTH_EXTRACTING.search(text):
        return []
    if judge is None:
        if not llm_available():
            return []
        judge = complete_json
    user = f"USER MESSAGE:\n{text}"
    if context:
        user = f"(Earlier the assistant said: {context[:500]})\n\n" + user
    data = await judge(EXTRACT_SYSTEM, user, max_tokens=500, model=fast_model_name())
    cards = data.get("cards") if isinstance(data, dict) else None
    out = []
    for c in cards if isinstance(cards, list) else []:
        if not isinstance(c, dict):
            continue
        kind = c.get("kind")
        body = str(c.get("text") or "").strip()
        if kind in USER_KINDS and len(body) >= 8:
            out.append({"kind": kind, "text": body, "subject": str(c.get("subject") or "")})
    return out[:MAX_CARDS_PER_MESSAGE]


def cards_from_verdicts(claims: list[Claim], verdicts: list[ClaimVerdict]) -> list[dict[str, Any]]:
    """Confirmed-only: green+supported-with-evidence → verified_fact;
    red+contradicted-with-evidence → correction. Everything else is ignored."""
    by_id = {c.claim_id: c for c in claims}
    out = []
    for v in verdicts:
        claim = by_id.get(v.claim_id)
        if claim is None or claim.type in ("source_summary", "code_api", "code", "other"):
            continue  # pasted-document facts, code, and pushback pseudo-claims stay out
        for r in v.detector_results:
            if not r.evidence or r.detector not in ("claim_verifier", "reference_auditor"):
                continue
            if v.final == "green" and r.status == "supported" and r.confidence >= 0.8:
                out.append(
                    {
                        "kind": "verified_fact",
                        "text": claim.normalized,
                        "source": r.detector,
                        "confidence": r.confidence,
                    }
                )
                break
            if v.final == "red" and r.status == "contradicted" and r.confidence >= 0.8:
                out.append(
                    {
                        "kind": "correction",
                        "text": r.explanation or claim.normalized,
                        "source": r.detector,
                        "confidence": r.confidence,
                    }
                )
                break
    return out


# =============================================================================================
# Engine hooks (called from session/plugins — best-effort, never raise)
# =============================================================================================
async def on_user_message(session: SessionContext, message: Any, judge=None) -> list[MemoryCard]:
    """After a user message: extract user facts/constraints and store them."""
    try:
        text = getattr(message, "text", "") or ""
        prev = session.previous(getattr(message, "message_id", ""), "assistant")
        extracted = await extract_user_cards(text, prev.text if prev else "", judge=judge)
        uid = user_id_for(session)
        stored = []
        for c in extracted:
            card, _ = await add_card(uid, c["kind"], c["text"], c["subject"], "user_message")
            stored.append(card)
        return stored
    except (LLMError, Exception) as exc:  # noqa: BLE001 — memory must never break the engine
        log.warning("memory.on_user_message failed: %r", exc)
        return []


async def on_verdicts(
    session: SessionContext, claims: list[Claim], verdicts: list[ClaimVerdict]
) -> list[MemoryCard]:
    """After a reply is judged: store evidence-backed facts and corrections."""
    try:
        uid = user_id_for(session)
        stored = []
        for c in cards_from_verdicts(claims, verdicts):
            card, _ = await add_card(uid, c["kind"], c["text"], "", c["source"], c["confidence"])
            stored.append(card)
        return stored
    except Exception as exc:  # noqa: BLE001
        log.warning("memory.on_verdicts failed: %r", exc)
        return []


# =============================================================================================
# Reading
# =============================================================================================
async def relevant(
    user_id: str,
    text: str,
    k: int = 5,
    kinds: tuple[str, ...] | None = None,
    store: MemoryStore | None = None,
) -> list[tuple[MemoryCard, float]]:
    """The k active cards most related to `text`, best first, with their scores.

    Score = similarity + small bonuses: +0.05 for user-stated cards (the user's word is the
    strongest memory) and +0.03 for cards used recently/often. Cards below the relevance floor
    are dropped, so an unrelated claim gets [] rather than random memories.
    """
    store = store or get_store()
    try:
        cards = await store.all(user_id)
        if kinds:
            cards = [c for c in cards if c.kind in kinds]
        if not cards:
            return []
        emb = await embed(text) if any(c.embedding for c in cards) else None
        scored = []
        for c in cards:
            sim, used_emb = similarity(c, text, emb)
            if sim < (MIN_COSINE if used_emb else MIN_WORDS):
                continue
            bonus = (0.05 if c.kind in USER_KINDS else 0.0) + (0.03 if c.uses else 0.0)
            scored.append((c, sim + bonus))
        scored.sort(key=lambda t: t[1], reverse=True)
        top = scored[:k]
        await store.touch([c.card_id for c, _ in top])
        return top
    except Exception as exc:  # noqa: BLE001
        log.warning("memory.relevant failed: %r", exc)
        return []


async def list_cards(user_id: str, store: MemoryStore | None = None) -> list[MemoryCard]:
    try:
        return await (store or get_store()).all(user_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("memory.list_cards failed: %r", exc)
        return []


async def delete_card(user_id: str, card_id: str, store: MemoryStore | None = None) -> bool:
    """User control: "What Reigns remembers" → delete."""
    try:
        return await (store or get_store()).delete(user_id, card_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("memory.delete_card failed: %r", exc)
        return False


_SUMMARY_HEADINGS = {
    "constraint": "Rules you must follow",
    "user_fact": "About me and my setup",
    "verified_fact": "Already verified",
    "correction": "Corrections (do not repeat these mistakes)",
}


async def ledger_summary(user_id: str, store: MemoryStore | None = None, limit: int = 20) -> str:
    """A clean block to paste into a NEW chat ("Start fresh", PRD §10 level 4)."""
    cards = await list_cards(user_id, store)
    if not cards:
        return ""
    cards = sorted(cards, key=lambda c: (c.last_used_at or c.created_at), reverse=True)[:limit]
    lines = ["Context from my previous chat (verified — please rely on it):"]
    for kind, heading in _SUMMARY_HEADINGS.items():
        group = [c for c in cards if c.kind == kind]
        if group:
            lines.append(f"\n{heading}:")
            lines += [f"- {c.text}" for c in group]
    return "\n".join(lines)


# =============================================================================================
# Try it by hand:  python -m app.learning.memory "I'm on pandas 1.5 and our API allows 100/min"
# =============================================================================================
async def _cli(message: str) -> None:
    from app.models import ChatMessage

    session = SessionContext(session_id="cli")
    session.messages.append(ChatMessage(message_id="u1", role="user", text=message, position=0))
    stored = await on_user_message(session, session.messages[0])
    print(f"Stored {len(stored)} card(s) for user '{user_id_for(session)}':")
    for c in stored:
        print(f"  [{c.kind}] {c.text}   (subject: {c.subject})")
    print("\nWhole ledger:")
    for c in await list_cards(user_id_for(session)):
        print(f"  [{c.kind}] {c.text}")


if __name__ == "__main__":
    import sys

    asyncio.run(_cli(" ".join(sys.argv[1:]) or "I'm on pandas 1.5 and our API allows 100/min."))
