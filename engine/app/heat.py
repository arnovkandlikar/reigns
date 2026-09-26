"""Heat score + level (FR-B7, PRD §9 — exact algorithm).

Pure, clock-injected logic so it is fully unit-testable: every public method takes `now`
(seconds, e.g. time.monotonic()).
"""
from __future__ import annotations

from dataclasses import dataclass, field

# §9 Adds / Subtracts
RED_POINTS = 25
CAVED_POINTS = 15
AMBER_POINTS = 8
CLEAN_REPLY_POINTS = -10
VERIFIED_FIX_POINTS = -20
DECAY_EVERY_S = 30.0  # −1 per 30 s since last heat increase
HYSTERESIS_S = 2.0  # level changes only after the new range holds for 2 s
RECOVERED_S = 3.0  # Recovered shown for 3 s after a verified fix

# §9 Levels: (level, max heat inclusive)
LEVEL_BOUNDS = [(0, 10), (1, 30), (2, 55), (3, 80), (4, 100)]


def level_for(heat: int) -> int:
    for level, upper in LEVEL_BOUNDS:
        if heat <= upper:
            return level
    return 4


def points_for(final: str, caved: bool) -> int:
    """Points a single claim contributes. caved_without_evidence is red but scores +15 (§9)."""
    if final == "red":
        return CAVED_POINTS if caved else RED_POINTS
    if final == "amber":
        return AMBER_POINTS
    return 0


@dataclass
class HeatState:
    heat: int = 0
    displayed_level: int = 0
    _pending_level: int | None = None
    _pending_since: float = 0.0
    _decay_anchor: float | None = None  # time of last heat increase (decay reference)
    _recovered_until: float = 0.0
    contributions: dict[str, int] = field(default_factory=dict)  # claim_id → points added

    # --- internals --------------------------------------------------------------------------
    def _set(self, value: int) -> None:
        self.heat = max(0, min(100, value))

    def _decay(self, now: float) -> None:
        if self._decay_anchor is None or self.heat == 0:
            return
        steps = int((now - self._decay_anchor) // DECAY_EVERY_S)
        if steps > 0:
            self._set(self.heat - steps)
            self._decay_anchor += steps * DECAY_EVERY_S

    # --- events -----------------------------------------------------------------------------
    def add_claims(self, finals: list[tuple[str, str, bool]], now: float) -> None:
        """finals: (claim_id, final, caved). Clean reply (no red/amber) → −10."""
        self._decay(now)
        added = 0
        for claim_id, final, caved in finals:
            pts = points_for(final, caved)
            if pts:
                self.contributions[claim_id] = pts
                added += pts
        if added:
            self._set(self.heat + added)
            self._decay_anchor = now
        else:
            self._set(self.heat + CLEAN_REPLY_POINTS)

    def verified_fix(self, now: float) -> None:
        self._decay(now)
        self._set(self.heat + VERIFIED_FIX_POINTS)
        self._recovered_until = now + RECOVERED_S

    def disagree(self, claim_id: str, now: float) -> bool:
        """Remove that claim's contribution. Returns True if anything changed."""
        self._decay(now)
        pts = self.contributions.pop(claim_id, 0)
        if pts:
            self._set(self.heat - pts)
        return bool(pts)

    def resolve(self, claim_ids: list[str], now: float) -> None:
        """A verified fix resolved these claims: their contribution no longer counts (FR-D5).
        Applied together with verified_fix()'s −20."""
        for cid in claim_ids:
            self.disagree(cid, now)

    def restore(self, heat: int, now: float) -> None:
        """Returning to a chat: show its saved heat right away (no hysteresis wait). Time spent in
        other chats doesn't count toward decay; old claim contributions aren't known anymore."""
        self._set(heat)
        self.displayed_level = level_for(self.heat)
        self._pending_level = None
        self._decay_anchor = now if self.heat else None
        self.contributions.clear()

    # --- reads ------------------------------------------------------------------------------
    def target_level(self, now: float) -> int:
        """Level implied by current heat, ignoring hysteresis (used for bubble content)."""
        self._decay(now)
        return level_for(self.heat)

    def recovered(self, now: float) -> bool:
        return now < self._recovered_until

    def tick(self, now: float) -> int:
        """Advance hysteresis; returns the level the pet should display right now."""
        target = self.target_level(now)
        if target == self.displayed_level:
            self._pending_level = None
        elif self._pending_level != target:
            self._pending_level, self._pending_since = target, now
        elif now - self._pending_since >= HYSTERESIS_S:
            self.displayed_level, self._pending_level = target, None
        return self.displayed_level
