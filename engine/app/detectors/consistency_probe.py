"""Consistency Probe (FR-C3) — catches confident answers that no search can settle.

Owner: Role C. Hallucination type #3 in PRD §7.0 ("made-up niche facts"): the first mayor of a
small town, an obscure statistic… Web search finds nothing, so the Claim Verifier can only say
"unverified". The idea (semantic entropy, Farquhar et al., Nature 2024): if the model really
KNOWS the answer, asking again gives the same answer; if it's making it up, answers scatter.

Pipeline for one claim (the engine only sends high-risk factual claims the Claim Verifier
couldn't settle — app/triage.py, stage 2):
  1. Question: claim.question from extraction if it's an open question (who/what/when);
     otherwise (missing or yes/no, which leaks the answer) one small LLM call writes one.
  2. Sample 5 fresh answers IN PARALLEL at temperature 1.0 (FR-C3). The samples never see the
     original answer, so they can't just copy it.
  3. A judge LLM groups the 5 samples by meaning ("Jógvan Poulsen" and "J. Poulsen" = same
     group) and says which group, if any, agrees with the original claim.
  4. Normalized semantic entropy (PRD §8.3):  H = -Σ p_i·ln(p_i),  H_norm = H / ln(N)
        > 0.6  → likely_hallucination   (answers all over the place)
        0.3–0.6 → uncertain
        < 0.3  → consistent             (the model keeps giving the same answer)
     Also likely_hallucination: NONE of the samples repeats the original answer (a real
     memory comes back at least once), even if entropy is below 0.6.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Awaitable, Callable
from typing import Any

from app.detectors.base import BaseDetector, looks_like_instruction, normalize_text, snippet
from app.detectors.claim_gate import gate
from app.llm import LLMError, complete_json, complete_text, fast_model_name
from app.models import Claim, DetectorResult, Evidence, SessionContext

N_SAMPLES = 5  # FR-C3
MIN_SAMPLES = 3  # if some sample calls fail, still decide with ≥ 3 answers
HIGH_ENTROPY = 0.6  # FR-C3 thresholds (Role D may tune them via FR-L4 later)
LOW_ENTROPY = 0.3
SAMPLE_MAX_TOKENS = 120
RETRY_BACKOFF_S = 0.8  # wait before re-trying a failed sample

SAMPLE_SYSTEM = """Answer the question in ONE short sentence with the specific answer
(a name, number, date, place …). Do not hedge, do not refuse, do not explain — give your single
best answer even if you are unsure."""

QUESTION_SYSTEM = """Turn a factual claim into the short, neutral question it answers, WITHOUT
revealing the answer. Example: claim "The first mayor of Tórshavn was Jógvan Poulsen." →
"Who was the first mayor of Tórshavn?".
The question must be answerable by someone who has NOT seen the conversation. If the claim is
advice, an instruction, an opinion, arithmetic about the user's own setup, or only makes sense
with earlier context ("it", "that function", "the second option"), return {"question": null}.
Return {"question": "..."} or {"question": null}."""

GROUP_SYSTEM = """You compare short answers to the same question and group them by MEANING.
Two answers are in the same group if they give the same specific answer, even if worded
differently ("Jógvan Poulsen" = "It was J. Poulsen"). Different names, numbers or dates are
different groups.

You also get ORIGINAL, the answer a chatbot gave earlier. Say which group (by index) has the
same meaning as ORIGINAL, or null if none does.

Return {"groups": [[<answer indices>], …], "original_group": <group index or null>}
Every answer index must appear in exactly one group."""

TextFn = Callable[..., Awaitable[str]]
JsonFn = Callable[..., Awaitable[Any]]


_REFUSAL = re.compile(
    r"\b(i (?:don't|do not|can't|cannot|am unable to|'m unable to|'m not able to) "
    r"(?:have|know|access|determine|answer|say|tell|provide|verify|confirm|find)|"
    r"(?:without|need|needs|require|requires) (?:more |additional |the |specific |any )*"
    r"(?:context|information|details|data)|not enough (?:context|information)|"
    r"(?:no|not) (?:reliable|verifiable|specific) (?:information|record|data)|"
    r"could you (?:provide|share|clarify)|please (?:provide|share|clarify)|"
    r"i'm not (?:sure|certain|aware))",
    re.IGNORECASE,
)


def is_refusal(answer: str) -> bool:
    """ "I don't have enough context to answer that" and similar non-answers."""
    return bool(_REFUSAL.search(answer[:300]))


def semantic_entropy(group_sizes: list[int]) -> float:
    """Normalized semantic entropy, PRD §8.3. 0 = all agree, 1 = all different."""
    n = sum(group_sizes)
    if n <= 1:
        return 0.0
    h = -sum((k / n) * math.log(k / n) for k in group_sizes if k > 0)
    return min(1.0, max(0.0, h / math.log(n)))  # clamp float noise (-0.0, 1.0000000002)


_YES_NO_START = re.compile(
    r"^\s*(is|was|were|are|did|does|do|has|have|had|can|could|will|would|should|isn't|wasn't)\b",
    re.IGNORECASE,
)


def is_open_question(question: str | None) -> bool:
    """True for "Who/What/When … ?" questions; False for yes/no questions or none.

    A yes/no question ("Was Jógvan Poulsen the first mayor?") leaks the answer to the samples
    and invites a reflexive "yes", which would make a made-up fact look consistent. Live
    engine run: extraction produced exactly such a question for scenario_niche_entropy.
    """
    return bool(question and question.strip() and not _YES_NO_START.match(question))


def clean_groups(raw: Any, n: int) -> list[list[int]]:
    """Make the judge's grouping valid: every index 0..n-1 exactly once.

    Judges sometimes repeat an index or forget one. Repeats keep their first group; forgotten
    answers become their own group (the cautious choice: it can only raise entropy a little,
    never hide disagreement).
    """
    seen: set[int] = set()
    groups: list[list[int]] = []
    for g in raw if isinstance(raw, list) else []:
        members = []
        for i in g if isinstance(g, list) else []:
            if isinstance(i, int) and 0 <= i < n and i not in seen:
                seen.add(i)
                members.append(i)
        if members:
            groups.append(members)
    groups += [[i] for i in range(n) if i not in seen]
    return groups


class ConsistencyProbe(BaseDetector):
    name = "consistency_probe"

    def __init__(
        self,
        sampler: TextFn | None = None,
        judge: JsonFn | None = None,
        gate_judge: JsonFn | None = None,
    ) -> None:
        self.gate_judge = gate_judge
        # Injectable so tests run offline with scripted answers.
        self.sampler: TextFn = sampler or complete_text
        self.judge: JsonFn = judge or complete_json

    async def _check(self, claim: Claim, session: SessionContext) -> DetectorResult | None:
        if looks_like_instruction(claim.quote):
            return None  # re-asking advice out of context only produces false alarms
        g = await gate(claim, session, self.gate_judge)
        if not g.checkable:
            return None  # only world facts can be re-asked meaningfully
        if g.source == "llm":
            # The gate saw the conversation: its question is answerable by a stranger, while
            # the extractor's may still say "it"/"that route".
            claim = g.resolved(claim).model_copy(update={"question": g.question})
        question = claim.question if is_open_question(claim.question) else None
        question = question or await self.cached(
            session, f"question:{normalize_text(claim.normalized)}", lambda: self._question(claim)
        )
        if not question:
            return None  # not a standalone fact (the question writer said so)
        data = await self.cached(
            session,
            f"probe:{normalize_text(question)}|{normalize_text(claim.normalized)}",
            lambda: self._probe(question, claim),
        )
        return self._to_result(data)

    # ------------------------------------------------------------------ steps
    async def _question(self, claim: Claim) -> str:
        data = await self.judge(
            QUESTION_SYSTEM, f"CLAIM: {claim.normalized}", max_tokens=100, model=fast_model_name()
        )
        if not isinstance(data, dict):
            raise LLMError("could not turn the claim into a question")
        # "" = not a standalone, checkable fact → the probe abstains (cached like a question)
        return str(data.get("question") or "").strip()

    async def _probe(self, question: str, claim: Claim) -> dict[str, Any]:
        """Sample, group, score. Returns plain data so it can live in session.cache."""
        # temperature=1.0 per FR-C3 (app.llm currently runs every call at the API default,
        # which is also 1.0 — passed explicitly so the intent survives future llm.py changes).
        raw = await asyncio.gather(
            *(self._sample(question) for _ in range(N_SAMPLES)), return_exceptions=True
        )
        answers = [a.strip() for a in raw if isinstance(a, str) and a.strip()]
        if len(answers) < MIN_SAMPLES:
            # Say WHY (e.g. "ANTHROPIC_API_KEY not set"), not just how many failed.
            reason = next((e for e in raw if isinstance(e, BaseException)), None)
            raise LLMError(
                f"only {len(answers)} of {N_SAMPLES} samples came back"
                + (f": {reason}" if reason else "")
            )

        # Refusals ("I don't have enough context…") are not answers. Out of context, questions
        # like "how much faster will it finish?" can't be answered, and counting the refusals
        # as "Claude never repeated its answer" made the probe cry wolf (live run: "That will
        # finish about 5x faster" → red). If most samples refuse, the probe abstains.
        real = [a for a in answers if not is_refusal(a)]
        if len(real) < MIN_SAMPLES or len(real) < len(answers) / 2:
            return {"question": question, "abstain": True, "refusals": len(answers) - len(real)}
        answers = real

        numbered = "\n".join(f"[{i}] {a[:300]}" for i, a in enumerate(answers))
        verdict = await self.judge(
            GROUP_SYSTEM,
            f"QUESTION: {question}\n\nANSWERS:\n{numbered}\n\nORIGINAL: {claim.normalized}",
            # Grouping short answers by meaning is easy → fast model (G1 latency). The SAMPLES
            # stay on the main model: we're measuring the model the user is talking to.
            model=fast_model_name(),
        )
        if not isinstance(verdict, dict):
            raise TypeError(f"grouping returned {type(verdict).__name__}, expected an object")
        groups = clean_groups(verdict.get("groups"), len(answers))
        og = verdict.get("original_group")
        original_group = og if isinstance(og, int) and 0 <= og < len(groups) else None
        return {
            "question": question,
            "answers": answers,
            "groups": groups,
            "original_group": original_group,
        }

    async def _sample(self, question: str) -> str:
        """One fresh answer; retried once if it errors or comes back empty.

        Live runs: 2 of 5 samples failed in bursts of parallel calls, leaving the probe one
        failure away from "error". One retry after a short back-off makes that rare.
        """
        last: BaseException | None = None
        for attempt in range(2):
            if attempt:
                # Back off before retrying: in the engine, several probes + the Claim Verifier
                # fire at once, and an instant retry lands in the same rate-limit burst.
                await asyncio.sleep(RETRY_BACKOFF_S)
            try:
                answer = await self.sampler(
                    SAMPLE_SYSTEM, question, temperature=1.0, max_tokens=SAMPLE_MAX_TOKENS
                )
                if isinstance(answer, str) and answer.strip():
                    return answer
                last = LLMError("empty answer")
            except LLMError as exc:
                last = exc
        raise last or LLMError("sample failed")

    # ------------------------------------------------------------------ verdict
    def _to_result(self, data: dict[str, Any]) -> DetectorResult | None:
        if data.get("abstain"):
            return None  # the question can't be answered out of context → nothing to say
        answers, groups = data["answers"], data["groups"]
        n = len(answers)
        sizes = [len(g) for g in groups]
        h = semantic_entropy(sizes)
        majority = max(range(len(groups)), key=lambda i: sizes[i])
        og = data["original_group"]
        original_in_majority = og is not None and sizes[og] == sizes[majority]
        agree_with_original = sizes[og] if og is not None else 0

        summary = (
            f"{n} samples → {len(groups)} different answer{'s' if len(groups) != 1 else ''} "
            f"(normalized entropy {h:.2f}); {agree_with_original}/{n} match the original"
        )
        evidence = [
            Evidence(source="Consistency Probe", url=None, snippet=summary),
            Evidence(
                source="Consistency Probe",
                url=None,
                snippet=snippet("Re-asked: " + " | ".join(a[:80] for a in answers), 300),
            ),
        ]

        if h > HIGH_ENTROPY:
            return self.result(
                "likely_hallucination",
                0.5 + 0.5 * h,  # 0.8 at H=0.6 … 1.0 at H=1
                f"Asked {n} times, Claude gave {len(groups)} different answers.",
                evidence,
            )
        if h < LOW_ENTROPY and not original_in_majority:
            # The model reliably says something else → the original answer was a one-off.
            best = answers[groups[majority][0]]
            return self.result(
                "likely_hallucination",
                0.5 + 0.4 * (sizes[majority] / n),
                f'Asked {n} times, Claude mostly answered "{snippet(best, 80)}", not what it '
                "said originally.",
                evidence,
            )
        if og is None:
            # Not one of the fresh answers repeats what the chat said. Live run: Tórshavn gave
            # [3, 1, 1] (entropy 0.59, just under 0.6) and 0/5 matched "Jógvan Poulsen" — a
            # real memory would come back at least once.
            return self.result(
                "likely_hallucination",
                0.75 + 0.2 * h,
                f"Asked {n} times, Claude never gave this answer again "
                f"({len(groups)} other answer{'s' if len(groups) != 1 else ''} instead).",
                evidence,
            )
        if h < LOW_ENTROPY:
            return self.result(
                "consistent",
                1.0 - h,
                f"Asked {n} times, Claude gave the same answer each time.",
                evidence,
            )
        return self.result(
            "uncertain",
            0.5,
            f"Asked {n} times, Claude's answers were mixed ({len(groups)} different answers).",
            evidence,
        )


detector = ConsistencyProbe()
