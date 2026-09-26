"""Loads other roles' code by convention, with safe fallbacks (so no one blocks anyone).

- Detectors: app.detectors.<name>.detector  (Role C / B / D). Missing → detector skipped.
- Course Correct: app.course_correct.api.{diagnose, build_bubble, on_fix_outcome}  (Role D).
  Missing or crashing → a plain fallback bubble built here.
- Calibration: app.learning.calibration.red_threshold(detector) (Role D, FR-L4). Missing → 0.8.

Nobody needs to edit this file to plug their code in — just create the module.
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import time
import uuid
from typing import Any, Callable, Optional

from app.models import (
    BubbleContent,
    BubbleProblem,
    Claim,
    ClaimVerdict,
    Correction,
    DetectorResult,
    DriftProfile,
    SessionContext,
)

log = logging.getLogger("reigns.plugins")

DETECTOR_NAMES = [
    "reference_auditor",
    "claim_verifier",
    "consistency_probe",
    "code_api_checker",
    "pushback",
    "source_faithfulness",
]
_detectors: dict[str, Any] = {}
_missing_logged: set[str] = set()


def _optional_import(module: str) -> Any | None:
    try:
        return importlib.import_module(module)
    except ModuleNotFoundError as exc:
        if exc.name != module and not module.startswith(str(exc.name)):
            log.error("import error inside %s: %s", module, exc)
        return None
    except Exception as exc:  # syntax error etc. in someone's WIP file must not kill the engine
        log.error("failed to import %s: %s", module, exc)
        return None


def get_detector(name: str) -> Any | None:
    if name in _detectors:
        return _detectors[name]
    mod = _optional_import(f"app.detectors.{name}")
    det = getattr(mod, "detector", None) if mod else None
    if det is None and mod is not None:
        # tolerate a class instead of an instance: first class whose .name matches
        for obj in vars(mod).values():
            if isinstance(obj, type) and getattr(obj, "name", None) == name:
                try:
                    det = obj()
                except Exception as exc:
                    log.error("could not instantiate %s: %s", obj, exc)
                break
    if det is None:
        if name not in _missing_logged:
            log.warning("detector '%s' not available yet — skipping it", name)
            _missing_logged.add(name)
        return None
    _detectors[name] = det
    return det


def available_detectors() -> list[str]:
    return [n for n in DETECTOR_NAMES if get_detector(n) is not None]


async def run_detector(
    name: str, claim: Claim, session: SessionContext, timeout_s: float
) -> Optional[DetectorResult]:
    """FR-B5: per-detector timeout; exceptions become status 'error' (FR-C5)."""
    det = get_detector(name)
    if det is None:
        return None
    t0 = time.perf_counter()
    try:
        result = await asyncio.wait_for(det.check(claim, session), timeout=timeout_s)
        if isinstance(result, dict):
            result = DetectorResult.model_validate(result)
        return result
    except asyncio.TimeoutError:
        msg = f"timed out after {timeout_s:.0f}s"
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
    log.warning("detector %s failed on claim %s: %s", name, claim.claim_id, msg)
    return DetectorResult(
        detector=name,  # type: ignore[arg-type]
        status="error",
        confidence=0.0,
        evidence=[],
        explanation=f"Check could not run ({msg}).",
        latency_ms=int((time.perf_counter() - t0) * 1000),
    )


# ---------------------------------------------------------------------------
# Calibration (FR-L4, Role D)
# ---------------------------------------------------------------------------
def not_in_source_threshold() -> float:
    mod = _optional_import("app.learning.calibration")
    fn: Callable | None = getattr(mod, "red_threshold", None) if mod else None
    try:
        return float(fn("source_faithfulness")) if fn else 0.8
    except Exception:
        return 0.8


# ---------------------------------------------------------------------------
# Course Correct (§12.5, Role D) + fallback
# ---------------------------------------------------------------------------
PROMPT_TYPE_BY_LEVEL = {1: "verify_nudge", 2: "targeted_correction", 3: "diagnostic_reset",
                        4: "fresh_start"}
_HEADLINES = {
    0: "All good so far.",
    1: "Hmm, I couldn't confirm some claims.",
    2: "A few things here look off.",
    3: "Heads up: Claude is stating things that don't check out.",
    4: "This conversation has gone off track. Consider starting fresh.",
}


def _problem_text(v: ClaimVerdict) -> str:
    bad = [r for r in v.detector_results if r.status not in ("supported", "consistent", "error")]
    return bad[0].explanation if bad else f'Couldn\'t verify: "{v.quote[:80]}"'


def _evidence_url(v: ClaimVerdict) -> Optional[str]:
    for r in v.detector_results:
        for e in r.evidence:
            if e.url:
                return e.url
    return None


def fallback_bubble(level: int, session: SessionContext) -> BubbleContent:
    flagged = [v for v in session.active_verdicts() if v.final in ("red", "amber")]
    flagged.sort(key=lambda v: v.final != "red")
    problems = [
        BubbleProblem(claim_id=v.claim_id, text=_problem_text(v), evidence_url=_evidence_url(v))
        for v in flagged[:5]
    ]
    correction = None
    if level >= 1 and flagged:
        lines = "\n".join(f"- \"{v.quote[:120]}\": {_problem_text(v)}" for v in flagged[:5])
        correction = Correction(
            correction_id=str(uuid.uuid4()),
            prompt_type=PROMPT_TYPE_BY_LEVEL[level],  # type: ignore[arg-type]
            text=(
                "Please double-check these points from your last answer:\n"
                f"{lines}\n"
                "If you're not sure about something, it's fine to say so. "
                "If your original answer was right, keep it and explain why."
            ),
        )
    any_red = any(v.final == "red" for v in flagged)
    return BubbleContent(
        level=level,
        headline=_HEADLINES[level],
        problems=problems,
        pattern_text="",
        confidence_label="Very sure" if any_red else ("Not sure" if flagged else ""),
        confidence_reason="Checked against external sources." if any_red else "",
        action_text="I wrote a prompt to fix this." if correction else "",
        correction=correction,
    )


COURSE_CORRECT_TIMEOUT_S = 4.0  # an LLM-assisted diagnose() must not stall the verdict (G1)


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def build_bubble(level: int, session: SessionContext) -> BubbleContent:
    """Role D's diagnose()/build_bubble() may be sync (the original §12.5 contract) or async
    (so diagnose can call the shared LLM judge). Either way it gets COURSE_CORRECT_TIMEOUT_S,
    after which the fallback bubble is used."""
    mod = _optional_import("app.course_correct.api")
    if mod and hasattr(mod, "diagnose") and hasattr(mod, "build_bubble"):

        async def run() -> BubbleContent:
            profile = await _maybe_await(mod.diagnose(session))
            if isinstance(profile, dict):
                profile = DriftProfile.model_validate(profile)
            bubble = await _maybe_await(mod.build_bubble(level, profile, session))
            if isinstance(bubble, dict):
                bubble = BubbleContent.model_validate(bubble)
            return bubble

        try:
            bubble = await asyncio.wait_for(run(), timeout=COURSE_CORRECT_TIMEOUT_S)
            if level == 0:
                bubble.correction = None  # §12.2: correction is null at level 0
            return bubble
        except asyncio.TimeoutError:
            log.error("course_correct timed out after %.0fs, using fallback bubble",
                      COURSE_CORRECT_TIMEOUT_S)
        except Exception as exc:
            log.error("course_correct failed, using fallback bubble: %s", exc)
    return fallback_bubble(level, session)


def on_fix_outcome(session: SessionContext, correction_id: str, fixed: bool) -> None:
    mod = _optional_import("app.course_correct.api")
    fn = getattr(mod, "on_fix_outcome", None) if mod else None
    if fn:
        try:
            fn(session, correction_id, fixed)
        except Exception as exc:
            log.error("on_fix_outcome failed: %s", exc)
