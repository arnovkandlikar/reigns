"""MongoDB Atlas learning store (FR-L1, Role B).

Writes anonymized, confirmed-only learning records to Atlas (§12.7, FR-L5):
- cases           confirmed hallucinations / confirmed-correct claims
- feedback        "I disagree" clicks
- prompt_trials   one row per inserted correction once its outcome is known
(`prompt_variants` belongs to Role D's bandit, `detector_calibration` to Role D's calibration;
they can use `get_db()` from here instead of opening their own connection.)

Rules this file enforces:
- no conversation text, no pasted documents, no names — only the claim's normalized form
  (and never for source_summary claims, whose text comes from the user's document)
- only confirmed signals: a red/green final backed by external evidence, user feedback,
  or a verified fix. Never likely_hallucination/uncertain alone.
- never raise, never block a verdict: callers fire-and-forget; failures go to a small
  in-memory queue that is retried on the next successful write (R14).
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections import deque
from typing import Any, Optional

from app.models import Claim, ClaimVerdict, CorrectionRecord, SessionContext, utc_now_iso

log = logging.getLogger("reigns.store")

DB_NAME = "reigns"
# Evidence-backed statuses that count as "confirmed" (FR-L5 a)
CONFIRMED_STATUSES = {"supported", "contradicted", "not_in_source", "nonexistent_api"}
_NEVER_STORE_TYPES = {"source_summary"}  # text derives from the user's pasted document
_QUEUE_MAX = 500

_client: Any = None
_db: Any = None
_queue: deque[tuple[str, dict]] = deque(maxlen=_QUEUE_MAX)  # (collection, doc) awaiting retry
_lock = asyncio.Lock()


def enabled() -> bool:
    return bool(os.environ.get("MONGODB_URI"))


def get_db() -> Any:
    """Motor database handle (or None when Mongo is off). Shared with Roles C/D."""
    global _client, _db
    if not enabled():
        return None
    if _db is None:
        import certifi
        from motor.motor_asyncio import AsyncIOMotorClient

        # tlsCAFile: python.org builds on macOS ship without system CA certs, which makes
        # Atlas fail with CERTIFICATE_VERIFY_FAILED. certifi's bundle fixes it everywhere.
        _client = AsyncIOMotorClient(
            os.environ["MONGODB_URI"],
            serverSelectionTimeoutMS=3000,
            appname="reigns-engine",
            tlsCAFile=certifi.where(),
        )
        _db = _client[DB_NAME]
    return _db


async def ping() -> bool:
    db = get_db()
    if db is None:
        return False
    try:
        await db.command("ping")
        return True
    except Exception as exc:
        log.warning("mongo ping failed: %s", exc)
        return False


async def startup() -> None:
    """Called once at engine start: ping + indexes. Never raises."""
    if not enabled():
        log.info("mongo disabled (no MONGODB_URI)")
        return
    if not await ping():
        return
    try:
        db = get_db()
        await db.cases.create_index([("claim_type", 1), ("created_at", -1)])
        await db.cases.create_index("origin")
        await db.feedback.create_index([("detector", 1), ("created_at", -1)])
        await db.prompt_trials.create_index([("variant_id", 1), ("created_at", -1)])
        log.info("mongo connected", extra={"db": DB_NAME})
    except Exception as exc:
        log.warning("mongo index setup failed: %s", exc)


# ---------------------------------------------------------------------------------- writes
async def _insert(collection: str, doc: dict) -> None:
    """Insert one doc; on failure queue it. On success, flush anything queued earlier."""
    db = get_db()
    if db is None:
        return
    try:
        await db[collection].insert_one(doc)
    except Exception as exc:
        _queue.append((collection, doc))
        log.warning("mongo write to %s failed (queued %d): %s", collection, len(_queue), exc)
        return
    await _flush()


async def _flush() -> None:
    if not _queue or _lock.locked():
        return
    async with _lock:
        db = get_db()
        while _queue:
            collection, doc = _queue[0]
            try:
                await db[collection].insert_one(doc)
            except Exception as exc:
                if "duplicate key" not in str(exc).lower():
                    return  # still down; keep the rest queued
            _queue.popleft()


def _confirmed_result(v: ClaimVerdict):
    """The detector result that externally confirms this verdict, or None (FR-L5)."""
    if v.final not in ("red", "green"):
        return None
    for r in v.detector_results:
        if r.status in CONFIRMED_STATUSES and r.evidence and r.status != "error":
            return r
    return None


def case_doc(claim: Claim, v: ClaimVerdict, app: str, confirmed_by: str = "evidence") -> Optional[dict]:
    if claim.type in _NEVER_STORE_TYPES:
        return None
    r = _confirmed_result(v)
    if r is None:
        return None
    return {
        "_id": str(uuid.uuid4()),
        "normalized": claim.normalized[:500],
        "claim_type": claim.type,
        "detector": r.detector,
        "final": v.final,
        "confirmed_by": confirmed_by,
        "origin": "live",
        "evidence_snippet": (r.evidence[0].snippet if r.evidence else "")[:300],
        "app": app,
        "created_at": utc_now_iso(),
    }


async def _embed(text: str) -> Optional[list[float]]:
    """Role C's experience memory (FR-L3) owns embeddings; use it if present, else skip."""
    try:
        from app.learning import memory  # type: ignore[attr-defined]
    except Exception:
        return None
    fn = getattr(memory, "embed", None)
    if fn is None:
        return None
    try:
        vec = await fn(text) if asyncio.iscoroutinefunction(fn) else fn(text)
        return list(vec) if vec else None
    except Exception as exc:
        log.warning("embedding failed, storing case without it: %s", exc)
        return None


async def record_verdicts(
    session: SessionContext, claims: list[Claim], verdicts: list[ClaimVerdict]
) -> None:
    """→ `cases`: one doc per confirmed red/green verdict."""
    if not enabled():
        return
    try:
        by_id = {c.claim_id: c for c in claims}
        for v in verdicts:
            claim = by_id.get(v.claim_id)
            doc = case_doc(claim, v, session.app) if claim else None
            if doc is None:
                continue
            emb = await _embed(doc["normalized"])
            if emb:
                doc["embedding"] = emb
            await _insert("cases", doc)
    except Exception as exc:
        log.error("record_verdicts failed: %s", exc)


async def record_feedback(verdict: ClaimVerdict) -> None:
    """→ `feedback` on "I disagree" (FR-L5 b). Uses the detector that flagged the claim."""
    if not enabled():
        return
    try:
        flagged = [
            r for r in verdict.detector_results
            if r.status not in ("supported", "consistent", "error")
        ]
        r = flagged[0] if flagged else (verdict.detector_results or [None])[0]
        await _insert("feedback", {
            "_id": str(uuid.uuid4()),
            "detector": r.detector if r else "unknown",
            "claim_type": verdict.type,
            "confidence": r.confidence if r else 0.0,
            "created_at": utc_now_iso(),
        })
    except Exception as exc:
        log.error("record_feedback failed: %s", exc)


async def record_trial(correction: CorrectionRecord, app: str) -> None:
    """→ `prompt_trials` once a correction's outcome is known (FR-L5 c, FR-D5)."""
    if not enabled() or correction.fixed is None:
        return
    try:
        await _insert("prompt_trials", {
            "_id": correction.correction_id,
            "variant_id": correction.variant_id or f"default:{correction.prompt_type}:v1",
            "level": correction.level,
            "fixed": bool(correction.fixed),
            "origin": "live",
            "app": app,
            "created_at": utc_now_iso(),
        })
    except Exception as exc:
        log.error("record_trial failed: %s", exc)


def queued() -> int:
    return len(_queue)
