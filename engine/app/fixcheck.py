"""Verify whether an inserted correction actually fixed the problems it targeted (FR-D5).

Called on the first assistant reply after the companion reports `correction.inserted`.
That reply has already been fully checked by the normal pipeline, so this only compares:

- Did the new reply re-assert any targeted claim (same reference / same fact) and is that
  re-assertion still red or amber?  → not fixed
- Does the new reply contain any red claim at all?                    → not fixed
- Otherwise                                                           → fixed

Pure functions, no I/O: easy to test and never slows the pipeline.
"""
from __future__ import annotations

import re

from app.extraction import overlaps
from app.models import Claim, ClaimVerdict

_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "the a an and or of in on to for with by is are was were be been it its this that as at "
    "from paper exists exist which who what".split()
)


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 2}


def same_claim(new: Claim, target: Claim) -> bool:
    """Is `new` the model re-stating `target`?"""
    if new.type in ("paper", "url", "package") or target.type in ("paper", "url", "package"):
        if overlaps(new, target) or overlaps(target, new):
            return True
    a, b = _words(target.normalized), _words(new.normalized)
    return len(a) >= 3 and len(a & b) / len(a) >= 0.6


def verify_fix(
    targets: list[Claim], new_claims: list[Claim], new_verdicts: list[ClaimVerdict]
) -> tuple[bool, str]:
    by_id = {v.claim_id: v for v in new_verdicts}
    for c in new_claims:
        v = by_id.get(c.claim_id)
        if v is None:
            continue
        repeated = next((t for t in targets if same_claim(c, t)), None)
        if repeated is not None and v.final in ("red", "amber"):
            return False, f'Repeated a flagged claim: "{c.quote[:80]}"'
    reds = [by_id[c.claim_id] for c in new_claims
            if c.claim_id in by_id and by_id[c.claim_id].final == "red"]
    if reds:
        return False, f'New reply still has a false claim: "{reds[0].quote[:80]}"'
    return True, "No targeted claim was repeated and nothing in the new reply is red."
