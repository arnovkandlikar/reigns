"""Per-session engine runtime: handles every inbound message type and produces outbound
envelopes in the §12.2 order (FR-B8: verdicts.update → heat.update → bubble.content).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid
from typing import Callable, Optional

from app import extraction, plugins, triage
from app.aggregate import final_status, is_caved, to_verdict
from app.heat import HeatState
from app.ledger import Ledger
from app.learning import store
from app.models import (
    BubbleContent,
    ChatMessage,
    Claim,
    ClaimVerdict,
    CorrectionInserted,
    CorrectionRecord,
    DetectorResult,
    Envelope,
    FeedbackDisagree,
    HeatUpdate,
    MessageNew,
    SessionContext,
    SessionStart,
    SourceDoc,
    VerdictsUpdate,
)
from app import voice

log = logging.getLogger("reigns.session")

DETECTOR_TIMEOUT_S = float(os.environ.get("REIGNS_DETECTOR_TIMEOUT_S", "12"))


def envelope(type_: str, session_id: str, payload) -> Envelope:
    data = payload.model_dump() if hasattr(payload, "model_dump") else payload
    return Envelope(type=type_, session_id=session_id, payload=data)


class Session:
    def __init__(self, session_id: str, ledger: Ledger, clock: Callable[[], float] = time.monotonic):
        self.ctx = SessionContext(session_id=session_id)
        self.heat = HeatState()
        self.ledger = ledger
        self.clock = clock
        self.lock = asyncio.Lock()
        self.pending_pushback = False
        self.last_heat_sent: Optional[tuple] = None
        self._background: set[asyncio.Task] = set()
        self.voice_sink: Optional[Callable] = None  # set by the WS handler

    @property
    def sid(self) -> str:
        return self.ctx.session_id

    # ------------------------------------------------------------------ heat helpers
    def heat_update(self) -> HeatUpdate:
        now = self.clock()
        level = self.heat.tick(now)
        red, amber = triage.counts(self.ctx.active_verdicts())
        self.ctx.heat, self.ctx.level = self.heat.heat, level
        return HeatUpdate(
            heat=self.heat.heat, level=level, recovered=self.heat.recovered(now),
            red_count=red, amber_count=amber,
        )

    def heat_envelope_if_changed(self) -> Optional[Envelope]:
        """Used by the WS ticker: emits heat.update only when something visible changed."""
        hu = self.heat_update()
        key = (hu.heat, hu.level, hu.recovered)
        if key == self.last_heat_sent:
            return None
        self.last_heat_sent = key
        return envelope("heat.update", self.sid, hu)

    def _heat_env(self) -> Envelope:
        hu = self.heat_update()
        self.last_heat_sent = (hu.heat, hu.level, hu.recovered)
        return envelope("heat.update", self.sid, hu)

    def _spawn(self, coro) -> None:
        """Fire-and-forget for Mongo/voice: never block a verdict on them (§14 rule 15)."""
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # ------------------------------------------------------------------ dispatch
    async def handle(self, env: Envelope, payload) -> list[Envelope]:
        if isinstance(payload, SessionStart):
            self.ctx.app = payload.app
            return [self._heat_env()]
        if isinstance(payload, MessageNew):
            async with self.lock:
                return await self.on_message(payload)
        if isinstance(payload, CorrectionInserted):
            return await self.on_correction_inserted(payload)
        if isinstance(payload, FeedbackDisagree):
            return await self.on_disagree(payload)
        return []

    # ------------------------------------------------------------------ message.new
    async def on_message(self, msg: MessageNew) -> list[Envelope]:
        if self.ctx.message(msg.message_id):
            return []  # companion re-sent a message we already processed
        self.ctx.messages.append(ChatMessage(**msg.model_dump()))
        await self.ledger.message(self.sid, msg)
        if msg.role == "user":
            self._on_user(msg)
            return []
        return await self._on_assistant(msg)

    def _on_user(self, msg: MessageNew) -> None:
        if len(msg.text) > extraction.SOURCE_DOC_MIN_CHARS:  # FR-B11
            doc = SourceDoc(doc_id=str(uuid.uuid4()), message_id=msg.message_id, text=msg.text)
            self.ctx.source_docs[doc.doc_id] = doc
        prior = "\n".join(m.text for m in self.ctx.messages if m.message_id != msg.message_id)
        has_prev_answer = self.ctx.previous(msg.message_id, "assistant") is not None
        self.pending_pushback = has_prev_answer and triage.is_pushback_without_evidence(
            msg.text, prior
        )

    def _pushback_claim(self, msg: MessageNew) -> Claim:
        first = re.split(r"(?<=[.!?])\s+|\n", msg.text.strip(), maxsplit=1)[0].strip()
        quote = first if first and first in msg.text else msg.text[:200]
        user = self.ctx.previous(msg.message_id, "user")
        return Claim(
            claim_id=str(uuid.uuid4()), message_id=msg.message_id, quote=quote,
            normalized="The assistant's answer after the user pushed back without new evidence.",
            type="other", risk="high", context=(user.text if user else "")[:1000],
        )

    async def _run_stage(self, jobs: list[tuple[Claim, str]]) -> list[Optional[DetectorResult]]:
        return await asyncio.gather(
            *(plugins.run_detector(name, c, self.ctx, DETECTOR_TIMEOUT_S) for c, name in jobs)
        )

    async def _on_assistant(self, msg: MessageNew) -> list[Envelope]:
        t0 = time.perf_counter()
        # G1 latency: unambiguous references start their checks right away, overlapping with
        # the (slower) LLM extraction instead of waiting for it.
        fast = [triage.assign_risk(c) for c in extraction.fast_reference_claims(
            self.ctx, msg.message_id, msg.text)]
        for c in fast:
            self.ctx.claims[c.claim_id] = c
        fast_jobs = [(c, name) for c in fast for name in triage.route(c)]
        fast_task = asyncio.ensure_future(self._run_stage(fast_jobs))
        try:
            rest = [triage.assign_risk(c) for c in await extraction.extract_claims(
                self.ctx, msg.message_id, msg.text, exclude=fast)]
        except BaseException:
            fast_task.cancel()
            raise
        claims = fast + rest
        routes: dict[str, list[str]] = {c.claim_id: triage.route(c) for c in claims}
        if self.pending_pushback:  # FR-C4: check this reply for caving
            pb = self._pushback_claim(msg)
            claims.append(pb)
            routes[pb.claim_id] = ["pushback"]
            self.pending_pushback = False
        for c in claims:
            self.ctx.claims[c.claim_id] = c
        t_extract = time.perf_counter()

        # Stage 1 — all detectors for all claims concurrently (FR-B5); fast-path jobs are
        # already running, the rest start now.
        fast_ids = {c.claim_id for c in fast}
        jobs = [(c, name) for c in claims if c.claim_id not in fast_ids
                for name in routes[c.claim_id]]
        results: dict[str, list[DetectorResult]] = {c.claim_id: [] for c in claims}
        rest_results, fast_results = await asyncio.gather(self._run_stage(jobs), fast_task)
        for (c, _), r in list(zip(jobs, rest_results)) + list(zip(fast_jobs, fast_results)):
            if r is not None:
                results[c.claim_id].append(r)
        # Stage 2 — Consistency Probe where Claim Verifier found no evidence (FR-B4)
        jobs2 = [(c, "consistency_probe") for c in claims
                 if "claim_verifier" in routes[c.claim_id]
                 and triage.needs_consistency_probe(c, results[c.claim_id])]
        for (c, _), r in zip(jobs2, await self._run_stage(jobs2)):
            if r is not None:
                results[c.claim_id].append(r)
        t_detect = time.perf_counter()

        # Aggregate (§8.4)
        threshold = plugins.not_in_source_threshold()
        verdicts: list[ClaimVerdict] = []
        heat_items = []
        for c in claims:
            final = final_status(c, results[c.claim_id], threshold)
            v = to_verdict(c, results[c.claim_id], final)
            verdicts.append(v)
            self.ctx.verdicts[c.claim_id] = v
            heat_items.append((c.claim_id, final, is_caved(results[c.claim_id])))
        await self.ledger.claims(self.sid, claims)
        await self.ledger.verdicts(self.sid, verdicts)

        # Heat (§9) + verified-fix check (FR-D5 minimal version; Role D may take over)
        now = self.clock()
        prev_level = self.heat.displayed_level
        self.heat.add_claims(heat_items, now)
        self._check_fix(verdicts, now)
        level = self.heat.target_level(now)
        self.ctx.heat, self.ctx.level = self.heat.heat, level

        bubble = plugins.build_bubble(level, self.ctx)
        await self._record_correction(bubble)

        out = [
            envelope("verdicts.update", self.sid,
                     VerdictsUpdate(message_id=msg.message_id, claims=verdicts)),
            self._heat_env(),
            envelope("bubble.content", self.sid, bubble),
        ]
        self._spawn(store.record_verdicts(self.ctx, claims, verdicts))
        self._spawn(self._maybe_voice(prev_level, level, bubble))
        log.info("processed reply", extra={"session_id": self.sid, "message_id": msg.message_id,
                 "claims": len(claims), "extract_ms": int((t_extract - t0) * 1000),
                 "detect_ms": int((t_detect - t_extract) * 1000),
                 "total_ms": int((time.perf_counter() - t0) * 1000)})
        return out

    def _check_fix(self, verdicts: list[ClaimVerdict], now: float) -> None:
        pending = [c for c in self.ctx.corrections if c.inserted and c.fixed is None]
        if not pending:
            return
        corr = pending[-1]
        corr.fixed = not any(v.final == "red" for v in verdicts)
        if corr.fixed:
            self.heat.verified_fix(now)
        self._spawn(self.ledger.correction(self.sid, corr))
        self._spawn(store.record_trial(corr, self.ctx.app))
        plugins.on_fix_outcome(self.ctx, corr.correction_id, corr.fixed)

    async def _record_correction(self, bubble: BubbleContent) -> None:
        if not bubble.correction:
            return
        rec = CorrectionRecord(
            correction_id=bubble.correction.correction_id,
            prompt_type=bubble.correction.prompt_type,
            level=bubble.level, text=bubble.correction.text,
        )
        self.ctx.corrections.append(rec)
        await self.ledger.correction(self.sid, rec)

    async def _maybe_voice(self, prev_level: int, level: int, bubble: BubbleContent) -> None:
        vp = await voice.maybe_speak(prev_level, level, bubble, self.heat.recovered(self.clock()))
        if vp and self.voice_sink:
            await self.voice_sink(envelope("voice.play", self.sid, vp))

    # ------------------------------------------------------------------ other inbound
    async def on_correction_inserted(self, p: CorrectionInserted) -> list[Envelope]:
        rec = next((c for c in self.ctx.corrections if c.correction_id == p.correction_id), None)
        if rec is None:
            log.warning("unknown correction_id %s", p.correction_id)
            return []
        rec.inserted = True
        await self.ledger.correction(self.sid, rec)
        return []

    async def on_disagree(self, p: FeedbackDisagree) -> list[Envelope]:
        self.ctx.disagreed_claim_ids.add(p.claim_id)
        self.heat.disagree(p.claim_id, self.clock())
        await self.ledger.feedback(self.sid, p.claim_id, "disagree", p.note)
        v = self.ctx.verdicts.get(p.claim_id)
        if v:
            self._spawn(store.record_feedback(v))
        level = self.heat.target_level(self.clock())
        bubble = plugins.build_bubble(level, self.ctx)
        await self._record_correction(bubble)
        return [self._heat_env(), envelope("bubble.content", self.sid, bubble)]
