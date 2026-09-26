"""MongoDB Atlas learning store (FR-L1, Role B) — STEP 6, hours 24–30.

Interface is fixed now so the pipeline already calls it; bodies are no-ops until
MONGODB_URI is set and the TODOs are done. Rules (§12.7, §14 rule 15, FR-L5):
- anonymized records only: no conversation text, no pasted docs, no names
- only confirmed signals (evidence-backed verdicts, user feedback, verified fixes)
- never raise, never block a verdict (callers fire-and-forget)
"""
from __future__ import annotations

import logging
import os

from app.models import ClaimVerdict, Claim, CorrectionRecord, SessionContext

log = logging.getLogger("reigns.store")

# Evidence-backed statuses that count as "confirmed" (FR-L5 a)
CONFIRMED_STATUSES = {"supported", "contradicted", "not_in_source", "nonexistent_api"}


def enabled() -> bool:
    return bool(os.environ.get("MONGODB_URI"))


async def record_verdicts(
    session: SessionContext, claims: list[Claim], verdicts: list[ClaimVerdict]
) -> None:
    """→ `cases` collection, one doc per confirmed verdict. TODO(FR-L1)."""
    if not enabled():
        return
    try:
        pass  # TODO: motor insert into reigns.cases (+ embedding via Role C's memory.embed)
    except Exception as exc:
        log.error("mongo record_verdicts failed: %s", exc)


async def record_feedback(verdict: ClaimVerdict) -> None:
    """→ `feedback` collection on 'I disagree'. TODO(FR-L1)."""
    if not enabled():
        return


async def record_trial(correction: CorrectionRecord, app: str) -> None:
    """→ `prompt_trials` collection after a fix is verified. TODO(FR-L1)."""
    if not enabled():
        return
