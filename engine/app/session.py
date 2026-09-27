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
from app.fixcheck import verify_fix
from app.heat import HeatState
from app.ledger import Ledger
from app.detectors import session_brief
from app.learning import memory, store
from app.models import (
    BriefOffer,
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
    SessionUpdate,
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
        self.voice_state = voice.VoiceState()
        # Per-chat heat memory: flipping back to a chat restores its panic rating.
        self.chat_key: Optional[str] = None
        self._restored_counts = (0, 0)  # red/amber from the chat's earlier visits
        self._last_saved_heat: Optional[tuple] = None
        self._heat_to_save: Optional[tuple] = None

    @property
    def sid(self) -> str:
        return self.ctx.session_id

    # ------------------------------------------------------------------ heat helpers
    def heat_update(self) -> HeatUpdate:
        now = self.clock()
        level = self.heat.tick(now)
        red, amber = triage.counts(self.ctx.active_verdicts())
        red, amber = red + self._restored_counts[0], amber + self._restored_counts[1]
        self.ctx.heat, self.ctx.level = self.heat.heat, level
        self._remember_heat(level, red, amber)
        return HeatUpdate(
            heat=self.heat.heat, level=level, recovered=self.heat.recovered(now),
            red_count=red, amber_count=amber,
        )

    def _remember_heat(self, level: int, red: int, amber: int) -> None:
        """Note this chat's latest heat; _save_heat() writes it if it changed."""
        self._heat_to_save = (self.heat.heat, level, red, amber)

    async def _save_heat(self) -> None:
        """Persist the chat's heat (a local SQLite upsert, ~1 ms) when it changed. Awaited rather
        than spawned so the write can't be cancelled halfway and wedge the ledger connection."""
        key = self._heat_to_save
        if self.chat_key is None or key is None or key == self._last_saved_heat:
            return
        self._last_saved_heat = key
        await self.ledger.save_chat_heat(self.chat_key, self.sid, *key)

    async def _restore_chat(self, chat_key: str) -> list[Envelope]:
        """session.start for a chat we've seen before → put its heat (and bubble) back."""
        self.chat_key = chat_key
        saved = await self.ledger.load_chat(chat_key)
        if not saved:
            return [self._heat_env()]
        self.heat.restore(int(saved["heat"]), self.clock())
        self._restored_counts = (int(saved["red_count"]), int(saved["amber_count"]))
        self._last_saved_heat = None
        out = [self._heat_env()]
        if saved.get("bubble") and self.heat.heat > 0:
            try:
                out.append(envelope("bubble.content", self.sid,
                                    BubbleContent.model_validate_json(saved["bubble"])))
            except ValueError as exc:
                log.warning("saved bubble unreadable for chat %s: %s", chat_key, exc)
        log.info("restored chat heat", extra={"session_id": self.sid, "heat": self.heat.heat})
        return out

    def heat_envelope_if_changed(self) -> Optional[Envelope]:
        """Used by the WS ticker: emits heat.update only when something visible changed."""
        hu = self.heat_update()
        key = (hu.heat, hu.level, hu.recovered)
        if key == self.last_heat_sent:
            return None
        self._spawn(self._save_heat())  # ticker path (decay): runs on the WebSocket's own loop
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
            self.ctx.language = voice.language(payload.language)
            self.ctx.character = (payload.character or "charlie").strip().lower()
            if payload.chat_key:
                return await self._restore_chat(payload.chat_key)
            return [self._heat_env()]
        if isinstance(payload, SessionUpdate):
            async with self.lock:  # never switch language halfway through judging a reply
                return await self.on_session_update(payload)
        if isinstance(payload, MessageNew):
            async with self.lock:
                return await self.on_message(payload)
        if isinstance(payload, CorrectionInserted):
            out = await self.on_correction_inserted(payload)
            await self._save_heat()
            return out
        if isinstance(payload, FeedbackDisagree):
            out = await self.on_disagree(payload)
            await self._save_heat()
            return out
        return []

    async def on_session_update(self, p: SessionUpdate) -> list[Envelope]:
        """Language / pet switched mid-chat: same session, so the score, history and spoken
        problems all carry over. The current bubble is rebuilt so it can follow the language."""
        if p.language is not None:
            self.ctx.language = voice.language(p.language)
        if p.character is not None:
            self.ctx.character = (p.character or "charlie").strip().lower()
        log.info("session updated", extra={"session_id": self.sid, "language": self.ctx.language,
                                           "character": self.ctx.character})
        out = [self._heat_env()]
        if self.heat.heat > 0 and self.ctx.verdicts:
            bubble = await plugins.build_bubble(self.heat.target_level(self.clock()), self.ctx)
            await self._record_correction(bubble)
            out.append(envelope("bubble.content", self.sid, bubble))
        return out

    # ------------------------------------------------------------------ message.new
    async def on_message(self, msg: MessageNew) -> list[Envelope]:
        if self.ctx.message(msg.message_id):
            return []  # companion re-sent a message we already processed
        self.ctx.messages.append(ChatMessage(**msg.model_dump()))
        if self.chat_key is None and msg.position == 0:
            self.chat_key = msg.message_id  # a brand-new chat: its first message names it
        await self.ledger.message(self.sid, msg, heat=self.heat.heat)
        if msg.role == "user":
            self._on_user(msg)
            return []
        return await self._on_assistant(msg)

    def _reign_authored(self, text: str) -> bool:
        """Is this user message REIGN's own prompt (Fix it / fresh-start hand-off / context
        refresh) that the companion pasted? Role C's memory.reign_authored decides; until that
        lands (or if it fails) nothing changes."""
        check = getattr(memory, "reign_authored", None)
        if check is None:
            return False
        try:
            return bool(check(self.ctx, text))
        except Exception as exc:  # never let this break message handling
            log.warning("reign_authored failed: %r", exc)
            return False

    def _on_user(self, msg: MessageNew) -> None:
        if self._reign_authored(msg.text):
            self.pending_pushback = False
            return  # REIGN's own prompt: not a source document, not pushback
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
        # G1 latency: the Consistency Probe (5 samples) is slow, so for likely candidates start it
        # *now*, in parallel with the Claim Verifier, instead of after it. If the verifier finds
        # evidence (supported/contradicted) the speculative probe is cancelled.
        speculative = {
            c.claim_id: asyncio.ensure_future(
                plugins.run_detector("consistency_probe", c, self.ctx, DETECTOR_TIMEOUT_S))
            for c in claims
            if "claim_verifier" in routes[c.claim_id] and triage.could_need_probe(c)
        } if plugins.get_detector("consistency_probe") else {}
        try:
            rest_results, fast_results = await asyncio.gather(self._run_stage(jobs), fast_task)
            for (c, _), r in list(zip(jobs, rest_results)) + list(zip(fast_jobs, fast_results)):
                if r is not None:
                    results[c.claim_id].append(r)
            # Stage 2 — Consistency Probe where Claim Verifier found no evidence (FR-B4)
            need = [c for c in claims
                    if "claim_verifier" in routes[c.claim_id]
                    and triage.needs_consistency_probe(c, results[c.claim_id])]
            for c in need:
                if c.claim_id not in speculative:
                    speculative[c.claim_id] = asyncio.ensure_future(plugins.run_detector(
                        "consistency_probe", c, self.ctx, DETECTOR_TIMEOUT_S))
            needed_ids = {c.claim_id for c in need}
            for cid, task in speculative.items():
                if cid not in needed_ids:
                    task.cancel()  # verifier found evidence: probe not needed
            for c in need:
                r = await speculative[c.claim_id]
                if r is not None:
                    results[c.claim_id].append(r)
        finally:
            for task in speculative.values():
                if not task.done():
                    task.cancel()
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

        # Heat (§9) + verified-fix check (FR-D5)
        now = self.clock()
        prev_level = self.heat.displayed_level
        self.heat.add_claims(heat_items, now)
        self._check_fix(claims, verdicts, now)
        level = self.heat.show_now(now)  # the horse reacts now, not 2 s later
        self.ctx.heat, self.ctx.level = self.heat.heat, level

        # Verdicts + heat go out right away; the bubble (Course Correct, up to 4 s) follows.
        out = [
            envelope("verdicts.update", self.sid,
                     VerdictsUpdate(message_id=msg.message_id, claims=verdicts)),
            self._heat_env(),
        ]
        if self.voice_sink:  # live WebSocket: push them now instead of returning them later
            for env in out:
                await self.voice_sink(env)
            out = []

        bubble = await plugins.build_bubble(level, self.ctx)
        await self._record_correction(bubble)
        out.append(envelope("bubble.content", self.sid, bubble))
        if level <= 1:  # never cover a warning bubble with a refresh offer
            offer = session_brief.offer(self.ctx)  # once per judged reply (it marks itself)
            if offer:
                try:
                    out.append(envelope("brief.offer", self.sid, BriefOffer(**offer)))
                except ValueError as exc:  # a malformed offer must never break the reply
                    log.warning("brief offer dropped: %s", exc)
        await self.ledger.message_heat(self.sid, msg.message_id, self.heat.heat)
        if self.chat_key:
            await self.ledger.save_chat_bubble(self.chat_key, bubble.model_dump_json())
        await self._save_heat()
        self._spawn(store.record_verdicts(self.ctx, claims, verdicts))
        self._spawn(memory.on_verdicts(self.ctx, claims, verdicts))
        clean = any(v.final == "green" for v in verdicts) and not any(
            v.final in ("red", "amber") for v in verdicts)
        self._spawn(self._maybe_voice(prev_level, level, bubble, clean))
        log.info("processed reply", extra={"session_id": self.sid, "message_id": msg.message_id,
                 "claims": len(claims), "extract_ms": int((t_extract - t0) * 1000),
                 "detect_ms": int((t_detect - t_extract) * 1000),
                 "total_ms": int((time.perf_counter() - t0) * 1000)})
        return out

    def _check_fix(self, claims: list[Claim], verdicts: list[ClaimVerdict], now: float) -> None:
        """FR-D5: judge the first reply after an inserted correction against its targets."""
        pending = [c for c in self.ctx.corrections if c.inserted and c.fixed is None]
        if not pending:
            return
        corr = pending[-1]
        targets = [self.ctx.claims[i] for i in corr.target_claim_ids if i in self.ctx.claims]
        corr.fixed, corr.fix_reason = verify_fix(targets, claims, verdicts)
        if corr.fixed:
            # The targeted claims are resolved: they leave the bubble and stop counting toward
            # heat, then the −20 for a verified fix applies (§9) and Recovered shows for 3 s.
            self.ctx.resolved_claim_ids.update(corr.target_claim_ids)
            self.heat.resolve(corr.target_claim_ids, now)
            self.heat.verified_fix(now)
        log.info("fix verified" if corr.fixed else "fix not verified",
                 extra={"session_id": self.sid, "correction_id": corr.correction_id,
                        "variant_id": corr.variant_id, "reason": corr.fix_reason})
        self._spawn(self.ledger.correction(self.sid, corr))
        self._spawn(store.record_trial(corr, self.ctx.app))
        plugins.on_fix_outcome(self.ctx, corr.correction_id, corr.fixed)

    async def _record_correction(self, bubble: BubbleContent) -> None:
        if not bubble.correction:
            return
        cid = bubble.correction.correction_id
        flagged = [v.claim_id for v in self.ctx.active_verdicts() if v.final in ("red", "amber")]
        shown = [p.claim_id for p in bubble.problems]
        rec = CorrectionRecord(
            correction_id=cid,
            prompt_type=bubble.correction.prompt_type,
            level=bubble.level,
            text=bubble.correction.text,
            # FR-L2: Role D's bandit tags the variant it chose via session.cache
            variant_id=self.ctx.cache.get(f"course_correct:variant:{cid}"),
            target_claim_ids=list(dict.fromkeys(shown + flagged)),
        )
        self.ctx.corrections.append(rec)
        await self.ledger.correction(self.sid, rec)

    async def _maybe_voice(
        self, prev_level: int, level: int, bubble: BubbleContent, clean: bool = False
    ) -> None:
        vp = await voice.maybe_speak(
            self.voice_state, prev_level, level, bubble, self.heat.recovered(self.clock()),
            clean=clean, lang=self.ctx.language, character=self.ctx.character,
        )
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
        bubble = await plugins.build_bubble(level, self.ctx)
        await self._record_correction(bubble)
        return [self._heat_env(), envelope("bubble.content", self.sid, bubble)]
