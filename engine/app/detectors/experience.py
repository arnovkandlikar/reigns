"""Experience memory (FR-L3). Owner: Role C.

What it does
------------
Every confirmed verdict is already written to the MongoDB Atlas `cases` collection by the
learning store (FR-L1, `app/learning/store.py`), with a Voyage embedding of the claim. This
module is the READ side: before a judge model decides a claim, it finds the most similar
past cases with Atlas Vector Search and puts them in the judge prompt as labeled examples:

    PAST CONFIRMED CASES
    1. turned out WRONG: "The Eiffel Tower was completed in 1899" (evidence: "...1889...")
    2. turned out CORRECT: "The Eiffel Tower is 330 metres tall" (evidence: "...330 m...")

So REIGN gets steadier with use: the judge sees how similar claims were settled before.

PRD rules (FR-L3, FR-L5)
------------------------
- top 3 cases, same claim type, cosine similarity >= 0.75
- only CONFIRMED cases (the store only writes evidence-backed red/green, user feedback or
  verified fixes; we re-check `final` and `confirmed_by` here anyway)
- if Atlas or embeddings fail, skip silently: the judge just runs without examples

Safety: past cases calibrate the judge, they are never evidence. The claim verifier still
requires a verbatim quote from TODAY's search snippets for supported/contradicted, so a
past case alone can never turn a claim red.

Atlas setup
-----------
Needs MONGODB_URI (the same one the store uses) and VOYAGE_API_KEY. The vector index
`cases_vector` is created automatically on first use (idempotent). To create it by hand in
the Atlas UI instead: Atlas Search → Create Index → Vector Search → JSON editor, collection
`reigns.cases`, name `cases_vector`, definition = index_definition() below.

Public API
----------
    await similar_cases(text, claim_type)  → list[Case] (never raises; [] when unavailable)
    examples_block(cases)                  → text block for a judge prompt ("" if none)
    await ensure_index()                   → create the vector index if missing
Env: REIGNS_EXPERIENCE=0 turns it off.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

log = logging.getLogger("reigns.detectors.experience")

INDEX_NAME = "cases_vector"
COLLECTION = "cases"
K = 3
MIN_COSINE = 0.75  # PRD FR-L3
NUM_CANDIDATES = 100  # ANN candidates Atlas considers before returning the best `limit`
LOOKUP_TIMEOUT_S = 2.5  # embedding + vector query; the judge never waits longer than this
CONFIRMED_BY = {"evidence", "user_feedback", "verified_fix", "dataset"}
SNIPPET_CHARS = 200

EmbedFn = Callable[[str], Awaitable[list[float] | None]]

_index_lock: asyncio.Lock | None = None
_index_ready = False
_index_failed = False


@dataclass(frozen=True)
class Case:
    text: str
    final: str  # "red" | "green"
    detector: str
    evidence: str
    cosine: float
    origin: str = "live"


# ------------------------------------------------------------------------------ helpers
def enabled() -> bool:
    if os.environ.get("REIGNS_EXPERIENCE", "1") == "0":
        return False
    return bool(os.environ.get("MONGODB_URI")) and bool(os.environ.get("VOYAGE_API_KEY"))


def embed_dim() -> int:
    from app.learning import memory

    return memory.EMBED_DIM


def index_definition() -> dict[str, Any]:
    """Atlas Vector Search index on cases.embedding, filterable by claim type (§12.7)."""
    return {
        "fields": [
            {
                "type": "vector",
                "path": "embedding",
                "numDimensions": embed_dim(),
                "similarity": "cosine",
            },
            {"type": "filter", "path": "claim_type"},
        ]
    }


def cosine_from_score(score: float) -> float:
    """Atlas reports cosine matches as score = (1 + cosine) / 2, in [0, 1]. Undo that so the
    PRD's "cosine >= 0.75" means what it says (score 0.875)."""
    return 2.0 * float(score) - 1.0


def pipeline(vector: list[float], claim_type: str, k: int = K) -> list[dict[str, Any]]:
    return [
        {
            "$vectorSearch": {
                "index": INDEX_NAME,
                "path": "embedding",
                "queryVector": vector,
                "numCandidates": NUM_CANDIDATES,
                "limit": k * 4,  # extra room: some are dropped below (duplicates, unconfirmed)
                "filter": {"claim_type": claim_type},
            }
        },
        {
            "$project": {
                "_id": 0,
                "normalized": 1,
                "final": 1,
                "detector": 1,
                "evidence_snippet": 1,
                "confirmed_by": 1,
                "origin": 1,
                "score": {"$meta": "vectorSearchScore"},
            }
        },
    ]


def _db() -> Any:
    from app.learning import store  # Role B's shared Motor handle

    return store.get_db()


async def _default_embed(text: str) -> list[float] | None:
    from app.learning import memory

    # "document" on both sides: cases were stored with the same input type, and we compare
    # claim to claim (symmetric), not a question to an answer.
    return await memory.embed(text, "document")


# ------------------------------------------------------------------------------ index
async def ensure_index(db: Any = None) -> bool:
    """Create the `cases_vector` index if it doesn't exist. Once per process; never raises."""
    global _index_lock, _index_ready, _index_failed
    if _index_ready:
        return True
    if _index_failed:
        return False
    db = db if db is not None else _db()
    if db is None:
        return False
    if _index_lock is None:
        _index_lock = asyncio.Lock()
    async with _index_lock:
        if _index_ready:
            return True
        try:
            coll = db[COLLECTION]
            cursor = coll.list_search_indexes(INDEX_NAME)
            existing = await cursor.to_list(length=1)
            if not existing:
                from pymongo.operations import SearchIndexModel

                model = SearchIndexModel(
                    definition=index_definition(), name=INDEX_NAME, type="vectorSearch"
                )
                await coll.create_search_index(model)
                log.info("experience: created Atlas vector index %s (builds in ~1 min)", INDEX_NAME)
            _index_ready = True
        except Exception as exc:  # noqa: BLE001 (e.g. local MongoDB without Atlas Search)
            _index_failed = True
            log.warning("experience: couldn't check/create vector index: %r", exc)
    return _index_ready


def _reset_for_tests() -> None:
    global _index_lock, _index_ready, _index_failed
    _index_lock, _index_ready, _index_failed = None, False, False


# ------------------------------------------------------------------------------ lookup
def _to_cases(rows: list[dict[str, Any]], k: int) -> list[Case]:
    out: list[Case] = []
    seen: set[str] = set()
    for r in rows:
        try:
            cos = cosine_from_score(r.get("score", 0.0))
        except (TypeError, ValueError):
            continue
        text = " ".join(str(r.get("normalized") or "").split())
        final = r.get("final")
        if cos < MIN_COSINE or not text or final not in ("red", "green"):
            continue
        if r.get("confirmed_by", "evidence") not in CONFIRMED_BY:
            continue
        key = text.lower()
        if key in seen:
            continue  # the same claim confirmed several times: show it once
        seen.add(key)
        out.append(
            Case(
                text=text[:300],
                final=final,
                detector=str(r.get("detector") or ""),
                evidence=" ".join(str(r.get("evidence_snippet") or "").split())[:SNIPPET_CHARS],
                cosine=round(cos, 3),
                origin=str(r.get("origin") or "live"),
            )
        )
    out.sort(key=lambda c: c.cosine, reverse=True)
    return out[:k]


async def _lookup(text: str, claim_type: str, k: int, db: Any, embed_fn: EmbedFn) -> list[Case]:
    vector = await embed_fn(text)
    if not vector:
        return []
    await ensure_index(db)
    cursor = db[COLLECTION].aggregate(pipeline(vector, claim_type, k))
    rows = await cursor.to_list(length=k * 4)
    return _to_cases(rows, k)


async def similar_cases(
    text: str,
    claim_type: str,
    k: int = K,
    db: Any = None,
    embed_fn: EmbedFn | None = None,
) -> list[Case]:
    """Up to k confirmed past cases similar to `text` (same claim type, cosine >= 0.75).

    Never raises and never waits longer than LOOKUP_TIMEOUT_S: [] means "no examples".
    """
    text = (text or "").strip()
    if not text:
        return []
    if db is None:
        if not enabled():
            return []
        db = _db()
        if db is None:
            return []
    try:
        return await asyncio.wait_for(
            _lookup(text[:500], claim_type, k, db, embed_fn or _default_embed),
            timeout=LOOKUP_TIMEOUT_S,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 (FR-L3: skip silently)
        log.info("experience lookup skipped: %r", exc)
        return []


# ------------------------------------------------------------------------------ prompt
def examples_block(cases: list[Case]) -> str:
    """Labeled examples for a judge prompt. "" when there are none (prompt stays unchanged)."""
    if not cases:
        return ""
    lines = [
        "PAST CONFIRMED CASES (similar claims REIGN checked before; each outcome was confirmed "
        "by evidence). Use them only to calibrate how you read evidence. They are NOT evidence "
        "about this claim: your verdict and quote must come from the evidence snippets.",
    ]
    for i, c in enumerate(cases, 1):
        outcome = "turned out WRONG" if c.final == "red" else "turned out CORRECT"
        ev = f' (evidence: "{c.evidence}")' if c.evidence else ""
        lines.append(f'{i}. {outcome}: "{c.text}"{ev}')
    return "\n".join(lines)
