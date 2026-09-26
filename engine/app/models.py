"""Pydantic v2 mirrors of PRD §12 (FR-B2). Role B is the schema steward.

⚠️  Do not change field names, enums or types here without the §15.3 change process:
propose in team chat → Role B updates this file + /shared/schemas + /shared/fixtures in ONE
commit (run scripts/export_schemas.py and scripts/make_fixtures.py) → everyone pulls.

Roles C and D: import everything you need from here, e.g.
    from app.models import Claim, SessionContext, DetectorResult, Evidence
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Enums (§8.1, §8.2, §12)
# ---------------------------------------------------------------------------
VerdictStatus = Literal[
    "supported",
    "contradicted",
    "unverified",
    "likely_hallucination",
    "uncertain",
    "consistent",
    "caved_without_evidence",
    "not_in_source",
    "nonexistent_api",
    "error",
]
ClaimType = Literal[
    "fact", "paper", "url", "package", "number", "code", "code_api", "source_summary", "other"
]
Risk = Literal["high", "medium", "low"]
FinalStatus = Literal["red", "amber", "green", "skipped"]
DetectorName = Literal[
    "reference_auditor",
    "claim_verifier",
    "consistency_probe",
    "pushback",
    "source_faithfulness",
    "code_api_checker",
    "memory_consistency",  # Role C: contradicts facts/constraints the user stated earlier
]
PromptType = Literal["verify_nudge", "targeted_correction", "diagnostic_reset", "fresh_start"]
RootCause = Literal[
    "knowledge_gap",
    "anchored_wrong_assumption",
    "caving",
    "fabricated_sources",
    "outdated_knowledge",
    "source_drift",
    "api_confusion",
]
MessageType = Literal[
    # Companion → Engine (§12.1)
    "session.start",
    "message.new",
    "correction.inserted",
    "feedback.disagree",
    # Engine → Companion (§12.2)
    "verdicts.update",
    "heat.update",
    "bubble.content",
    "voice.play",
    "error",
]


def utc_now_iso() -> str:
    """ISO-8601 UTC with a 'Z' suffix and no fractional seconds (§12)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Strict(BaseModel):
    """Wire models reject unknown fields so contract drift fails loudly."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Envelope (§12)
# ---------------------------------------------------------------------------
class Envelope(_Strict):
    type: MessageType
    session_id: str
    ts: str = Field(default_factory=utc_now_iso)
    payload: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Companion → Engine (§12.1)
# ---------------------------------------------------------------------------
class SessionStart(_Strict):
    app: str = "claude"
    app_version: str = "unknown"
    companion_version: str = "1.0"
    # Stable id of the Claude chat this session watches: the message_id of the chat's first
    # message (position 0). Lets the engine restore that chat's heat when the user flips back.
    chat_key: Optional[str] = None
    # Language the pet speaks: "en" (default) or "es". Unknown values fall back to English.
    language: Optional[str] = None


class MessageNew(_Strict):
    message_id: str
    role: Literal["assistant", "user"]
    text: str
    position: int = Field(ge=0)


class CorrectionInserted(_Strict):
    correction_id: str


class FeedbackDisagree(_Strict):
    claim_id: str
    note: Optional[str] = None


# ---------------------------------------------------------------------------
# Detector results (§12.4) — also part of verdicts.update on the wire
# ---------------------------------------------------------------------------
class Evidence(_Strict):
    source: str
    url: Optional[str] = None
    snippet: str


class DetectorResult(_Strict):
    detector: DetectorName
    status: VerdictStatus
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[Evidence] = Field(default_factory=list)
    explanation: str
    latency_ms: int = 0


# ---------------------------------------------------------------------------
# Engine → Companion (§12.2)
# ---------------------------------------------------------------------------
class ClaimVerdict(_Strict):
    claim_id: str
    quote: str
    type: ClaimType
    risk: Risk
    final: FinalStatus
    detector_results: list[DetectorResult] = Field(default_factory=list)


class VerdictsUpdate(_Strict):
    message_id: str
    claims: list[ClaimVerdict]


class HeatUpdate(_Strict):
    heat: int = Field(ge=0, le=100)
    level: int = Field(ge=0, le=4)
    recovered: bool = False
    red_count: int = 0
    amber_count: int = 0


class BubbleProblem(_Strict):
    claim_id: str
    text: str
    evidence_url: Optional[str] = None


class Correction(_Strict):
    correction_id: str
    prompt_type: PromptType
    text: str


class BubbleContent(_Strict):
    level: int = Field(ge=0, le=4)
    headline: str
    problems: list[BubbleProblem] = Field(default_factory=list)
    pattern_text: str = ""
    confidence_label: str = ""
    confidence_reason: str = ""
    action_text: str = ""
    correction: Optional[Correction] = None  # null at level 0


class VoicePlay(_Strict):
    text: str
    audio_b64: str
    mime: str = "audio/mpeg"
    level: int = Field(ge=0, le=4)


class ErrorPayload(_Strict):
    code: str
    message: str


# type string → payload model, used to validate every inbound/outbound message (FR-B2)
PAYLOAD_MODELS: dict[str, type[BaseModel]] = {
    "session.start": SessionStart,
    "message.new": MessageNew,
    "correction.inserted": CorrectionInserted,
    "feedback.disagree": FeedbackDisagree,
    "verdicts.update": VerdictsUpdate,
    "heat.update": HeatUpdate,
    "bubble.content": BubbleContent,
    "voice.play": VoicePlay,
    "error": ErrorPayload,
}
INBOUND_TYPES = {"session.start", "message.new", "correction.inserted", "feedback.disagree"}


# ---------------------------------------------------------------------------
# Internal models (§12.3) — Role B defines, Roles C/D consume
# ---------------------------------------------------------------------------
class Claim(BaseModel):
    claim_id: str
    message_id: str
    quote: str  # exact substring of the reply
    normalized: str  # standalone restatement
    type: ClaimType
    risk: Risk
    question: Optional[str] = None  # question form for the Consistency Probe
    context: str = ""  # user message that prompted this reply (≤ 1,000 chars)
    source_ref: Optional[str] = None  # SourceDoc id when type == "source_summary" (FR-B11)
    code: Optional[str] = None  # the code block when type == "code_api"


class SourceDoc(BaseModel):
    doc_id: str
    message_id: str
    text: str  # kept locally only (§18 privacy)


class ChatMessage(BaseModel):
    message_id: str
    role: Literal["assistant", "user"]
    text: str
    position: int
    ts: str = Field(default_factory=utc_now_iso)


class DriftProfile(BaseModel):
    """§12.5 — Role D's diagnose() returns this."""

    failures: list[str] = Field(default_factory=list)
    blast_radius: list[str] = Field(default_factory=list)
    trend: Literal["isolated", "worsening"] = "isolated"
    root_causes: list[RootCause] = Field(default_factory=list)


class CorrectionRecord(BaseModel):
    """A correction the engine offered; `inserted` flips when the companion pastes it."""

    correction_id: str
    prompt_type: PromptType
    level: int
    text: str
    inserted: bool = False
    fixed: Optional[bool] = None
    variant_id: Optional[str] = None  # Role D's bandit variant, if any
    target_claim_ids: list[str] = Field(default_factory=list)  # FR-D5: what this fix targets
    fix_reason: Optional[str] = None  # why it was judged fixed / not fixed


class SessionContext(BaseModel):
    """Read-mostly view of a session passed to detectors (§12.4) and Course Correct (§12.5).

    Detectors: treat everything as read-only EXCEPT `cache`, which is your per-session lookup
    cache (FR-C5). Namespace your keys, e.g. cache["crossref:" + title].
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    session_id: str
    app: str = "claude"
    language: str = "en"  # "en" | "es" — from session.start (or REIGNS_LANGUAGE); bubble/voice copy
    messages: list[ChatMessage] = Field(default_factory=list)
    source_docs: dict[str, SourceDoc] = Field(default_factory=dict)
    claims: dict[str, Claim] = Field(default_factory=dict)
    verdicts: dict[str, ClaimVerdict] = Field(default_factory=dict)  # claim_id → verdict
    corrections: list[CorrectionRecord] = Field(default_factory=list)
    disagreed_claim_ids: set[str] = Field(default_factory=set)
    resolved_claim_ids: set[str] = Field(default_factory=set)  # fixed by a verified correction
    heat: int = 0
    level: int = 0
    cache: dict[str, Any] = Field(default_factory=dict)

    # --- helpers for detectors / Course Correct -------------------------------------------
    def message(self, message_id: str) -> Optional[ChatMessage]:
        return next((m for m in self.messages if m.message_id == message_id), None)

    def previous(self, message_id: str, role: str) -> Optional[ChatMessage]:
        """Latest message with `role` that comes before `message_id` in the conversation."""
        target = self.message(message_id)
        cutoff = target.position if target else 10**9
        earlier = [m for m in self.messages if m.role == role and m.position < cutoff]
        return max(earlier, key=lambda m: m.position) if earlier else None

    def claims_for(self, message_id: str) -> list[Claim]:
        return [c for c in self.claims.values() if c.message_id == message_id]

    def active_verdicts(self) -> list[ClaimVerdict]:
        """All verdicts except ones the user disagreed with or a verified fix resolved."""
        hidden = self.disagreed_claim_ids | self.resolved_claim_ids
        return [v for k, v in self.verdicts.items() if k not in hidden]
