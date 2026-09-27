"""Per-detector red-threshold calibration from the last 20 user feedback signals (FR-L4).

The engine calls ``record_red_flag`` for each red decision and keeps its opaque flag ID
long enough to call ``record_disagreement`` if the user clicks I disagree. Only the
detector name, opaque ID, and Boolean feedback are persisted; claim text is never stored.
"""
from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from app.models import utc_now_iso

log = logging.getLogger("reigns.calibration")

WINDOW = 20
INCREASE_RATE = 0.30
DECREASE_RATE = 0.10
STEP = 0.05
MAX_INCREASE = 0.15
DEFAULT_THRESHOLDS = {
    "source_faithfulness": 0.80,
    "consistency_probe": 0.85,
}

_thresholds = DEFAULT_THRESHOLDS.copy()
_locks: dict[tuple[int, str], asyncio.Lock] = {}


def red_threshold(detector: str) -> float:
    """Return the active threshold for a detector, or the standard fallback."""
    return _thresholds.get(detector, 0.8)


def _valid_threshold(value: Any, default: float) -> float:
    try:
        threshold = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
        return default
    return min(default + MAX_INCREASE, max(default, threshold))


def _adjusted_threshold(current: float, flags: list[dict[str, Any]], default: float) -> float:
    if len(flags) < WINDOW:
        return current
    rate = sum(bool(flag.get("disagreed")) for flag in flags[-WINDOW:]) / WINDOW
    if rate > INCREASE_RATE:
        return min(default + MAX_INCREASE, current + STEP)
    if rate < DECREASE_RATE:
        return max(default, current - STEP)
    return current


def _lock_for(detector: str) -> asyncio.Lock:
    loop_id = id(asyncio.get_running_loop())
    return _locks.setdefault((loop_id, detector), asyncio.Lock())


async def initialize() -> None:
    """Load persisted thresholds at engine startup. Mongo can be disabled."""
    try:
        from app.learning import store

        db = store.get_db()
        if db is None:
            return
        for detector, default in DEFAULT_THRESHOLDS.items():
            doc = await db.detector_calibration.find_one({"_id": detector})
            if doc:
                _thresholds[detector] = _valid_threshold(doc.get("red_threshold"), default)
    except Exception as exc:  # noqa: BLE001 - calibration must not block startup
        log.warning("calibration load failed: %s", exc)


async def _save(detector: str, flags: list[dict[str, Any]], threshold: float) -> None:
    """Persist one detector's bounded feedback window and active threshold."""
    try:
        from app.learning import store

        db = store.get_db()
        if db is None:
            return
        rate = sum(bool(flag.get("disagreed")) for flag in flags) / len(flags) if flags else 0.0
        await db.detector_calibration.update_one(
            {"_id": detector},
            {
                "$set": {
                    "red_threshold": threshold,
                    "recent_disagree_rate": rate,
                    "recent_flags": flags[-WINDOW:],
                    "updated_at": utc_now_iso(),
                }
            },
            upsert=True,
        )
        _thresholds[detector] = threshold
    except Exception as exc:  # noqa: BLE001 - learning writes are best effort
        log.warning("calibration save failed for %s: %s", detector, exc)


async def record_red_flag(detector: str, flag_id: str) -> None:
    """Add one red-flag opportunity to the detector's rolling 20-item window."""
    default = DEFAULT_THRESHOLDS.get(detector)
    if default is None or not flag_id:
        return
    async with _lock_for(detector):
        try:
            from app.learning import store

            db = store.get_db()
            if db is None:
                return
            doc = await db.detector_calibration.find_one({"_id": detector}) or {}
            flags = [
                item
                for item in doc.get("recent_flags", [])
                if isinstance(item, dict) and isinstance(item.get("flag_id"), str)
            ]
            if any(item.get("flag_id") == flag_id for item in flags):
                return
            flags.append({"flag_id": flag_id, "disagreed": False})
            flags = flags[-WINDOW:]
            current = _valid_threshold(doc.get("red_threshold"), _thresholds.get(detector, default))
            threshold = _adjusted_threshold(current, flags, default)
            await _save(detector, flags, threshold)
        except Exception as exc:  # noqa: BLE001 - feedback learning must not block a turn
            log.warning("calibration flag record failed for %s: %s", detector, exc)


async def record_disagreement(detector: str, flag_id: str) -> None:
    """Mark a recent red flag as disagreed with and update its detector threshold."""
    default = DEFAULT_THRESHOLDS.get(detector)
    if default is None or not flag_id:
        return
    async with _lock_for(detector):
        try:
            from app.learning import store

            db = store.get_db()
            if db is None:
                return
            doc = await db.detector_calibration.find_one({"_id": detector}) or {}
            flags = [
                item
                for item in doc.get("recent_flags", [])
                if isinstance(item, dict) and isinstance(item.get("flag_id"), str)
            ]
            matched = False
            for flag in flags:
                if flag.get("flag_id") == flag_id:
                    flag["disagreed"] = True
                    matched = True
                    break
            if not matched:
                return
            current = _valid_threshold(doc.get("red_threshold"), _thresholds.get(detector, default))
            threshold = _adjusted_threshold(current, flags, default)
            await _save(detector, flags[-WINDOW:], threshold)
        except Exception as exc:  # noqa: BLE001 - feedback learning must not block a turn
            log.warning("calibration feedback failed for %s: %s", detector, exc)
