"""FR-L2: Thompson sampling for Course Correct prompt variants."""

from __future__ import annotations

import asyncio
import logging
import random

from pymongo.errors import PyMongoError

from app.learning.store import get_db

log = logging.getLogger("reigns.course_correct.bandit")

VARIANTS = ("v1", "v2", "v3")  # evidence-first, question-first, checklist-style


async def choose_variant(failure_type: str, prompt_type: str) -> tuple[str, str]:
    """Return (variant name, stable ID); Mongo stores observed success/failure counts."""
    ids = [f"{failure_type}:{prompt_type}:{variant}" for variant in VARIANTS]
    counts: dict[str, tuple[int, int]] = {}
    try:
        db = get_db()
        if db is not None:
            rows = await asyncio.wait_for(
                db.prompt_variants.find({"_id": {"$in": ids}}).to_list(length=3),
                timeout=0.5,
            )
            for row in rows:
                if row.get("_id") in ids:
                    counts[row["_id"]] = (
                        max(0, int(row.get("alpha", 0))),
                        max(0, int(row.get("beta", 0))),
                    )
    except (PyMongoError, TimeoutError, OSError, ValueError, TypeError, ImportError) as exc:
        log.warning("variant counts unavailable; sampling from priors: %s", exc)

    scores = []
    for variant_id in ids:
        successes, failures = counts.get(variant_id, (0, 0))
        scores.append(random.betavariate(1 + successes, 1 + failures))
    chosen = max(range(len(scores)), key=scores.__getitem__)
    return VARIANTS[chosen], ids[chosen]
