"""Claim extraction (FR-B3) + source-document tagging (FR-B11).

LLM path when ANTHROPIC_API_KEY is set; a deterministic heuristic fallback otherwise, so the
engine (and teammates without keys) always gets claims to work with.
"""
from __future__ import annotations

import logging
import re
import uuid

from app.llm import LLMError, complete_json, fast_model_name, llm_available
from app.models import Claim, SessionContext

log = logging.getLogger("reigns.extraction")

MAX_CLAIMS = 12
CONTEXT_CHARS = 1000
SOURCE_DOC_MIN_CHARS = 1500  # FR-B11
SOURCE_INTENT = re.compile(r"\b(summari[sz]e|summary|explain|according to|tl;?dr)\b", re.I)

# Hedges / self-talk that must never become claims (they'd come back amber and keep heat high
# right after a successful fix). Matched against the start of the quote.
HEDGE = re.compile(
    r"^\W*(?:(?:yes|no|ok(?:ay)?|sure|well|actually|honestly|good catch)[,!.]?\s+)?"
    r"(?:you'?re (?:right|correct)\b"
    r"|(?:i'?m|i am)\s+(?:not\s+(?:sure|confident|certain|aware)|unsure|uncertain)\b"
    r"|i\s+(?:don'?t|do not)\s+(?:know|have|think i)\b"
    r"|i\s+(?:can'?t|cannot|couldn'?t|could not)\s+(?:verify|confirm|find|be sure)\b"
    r"|i\s+(?:apologi[sz]e|shouldn'?t have|should not have|was wrong|made (?:a |an )?(?:mistake|error))"
    r"|(?:sorry|my apologies|apologies)\b"
    r"|(?:it'?s|it is)\s+(?:possible|unclear)\s+(?:that\s+)?(?:i|my)\b)",
    re.I,
)


def is_hedge(quote: str) -> bool:
    return bool(HEDGE.match(quote.strip()))


CODE_BLOCK = re.compile(r"```(\w+)?[ \t]*\n(.*?)```", re.DOTALL)
URL = re.compile(r"https?://[^\s)\]>\"']+")
PIP = re.compile(r"\b(?:pip|pip3)\s+install\s+([A-Za-z0-9_.\-]+)")
PAPER = re.compile(
    r"[^.\n]*(?:\bet al\.?|\(\s*(?:19|20)\d{2}\s*\)|\bdoi:|10\.\d{4,}/)[^.\n]*", re.I
)

EXTRACT_SYSTEM = """You extract atomic, checkable claims from an AI assistant's reply.
Return a JSON array (max 12 items). Each item:
{"quote": "<EXACT substring copied from the reply>",
 "normalized": "<standalone restatement that makes sense without context>",
 "type": "fact|paper|url|package|number|other",
 "risk": "high|medium|low",
 "question": "<the claim as a short question, or null>",
 "scope": "public|private"}
Rules:
- quote MUST be copied character-for-character from the reply.
- Each cited paper, URL and software package is its own claim (type paper/url/package).
- risk high = specific numbers, dates, names, citations, package names; low = general
  explanations, opinions, advice. Skip greetings and filler entirely.
- Skip the assistant's statements about its OWN knowledge or earlier answer: uncertainty
  ("I'm not confident those papers exist", "I don't know of any work on X"), apologies,
  self-corrections, and refusals. They are honest hedges, not checkable claims. If such a
  sentence also names a specific paper, URL or package, extract only that reference.
- scope "private" = only knowable from the user's own context: their project, repo, commits,
  files, code, company, notes, team, or anything stated earlier in THIS conversation
  (e.g. "your repo has 19 new commits", "Role C added the Consistency Probe",
  "your function returns None"). These cannot be checked against public sources, so mark
  them private. Everything checkable in public sources (encyclopedias, papers, docs,
  registries, the news) is "public". A cited paper, URL or package is always public.
- Do not extract claims from inside code blocks."""


def _new_id() -> str:
    return str(uuid.uuid4())


def repair_quote(quote: str, text: str) -> str | None:
    """Ensure the quote is an exact substring of the reply (FR-B3)."""
    if not quote:
        return None
    if quote in text:
        return quote
    idx = text.lower().find(quote.lower())
    if idx >= 0:
        return text[idx : idx + len(quote)]
    squashed = re.sub(r"\s+", " ", quote).strip()
    if squashed and squashed in text:
        return squashed
    return None


def _code_claims(text: str, message_id: str, context: str) -> list[Claim]:
    claims = []
    for lang, body in CODE_BLOCK.findall(text):
        if (lang or "python").lower() not in ("python", "py", "python3"):
            continue
        if not re.search(r"^\s*(import|from)\s+\w+", body, re.M):
            continue
        claims.append(
            Claim(
                claim_id=_new_id(),
                message_id=message_id,
                quote=body.strip(),
                normalized="The Python code uses real library functions and parameters.",
                type="code_api",
                risk="high",
                context=context,
                code=body,
            )
        )
    return claims


def _heuristic_claims(text: str, message_id: str, context: str) -> list[Claim]:
    prose = CODE_BLOCK.sub(" ", text)
    out: list[Claim] = []
    seen: set[str] = set()

    def add(quote: str, ctype: str, normalized: str | None = None) -> None:
        quote = quote.strip().strip("-*• ").strip()
        if len(quote) < 4 or quote in seen or quote not in text:
            return
        seen.add(quote)
        out.append(
            Claim(
                claim_id=_new_id(),
                message_id=message_id,
                quote=quote,
                normalized=normalized or quote,
                type=ctype,  # type: ignore[arg-type]
                risk="medium",
                question=None,
                context=context,
            )
        )

    for m in PAPER.finditer(prose):
        add(m.group(0), "paper")
    for m in URL.finditer(prose):
        add(m.group(0).rstrip(".,;"), "url", f"The URL {m.group(0)} exists.")
    for m in PIP.finditer(text):
        add(m.group(1), "package", f"The Python package '{m.group(1)}' exists on PyPI.")
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", prose):
        s = sentence.strip()
        if len(s) < 20 or any(s.rstrip(".!?") in q or q in s for q in seen):
            continue
        if is_hedge(s):
            continue
        if re.search(r"\d", s):
            add(s, "number")
        elif re.search(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b", s[1:]):
            add(s, "fact")
    return out


async def _llm_claims(text: str, message_id: str, context: str) -> list[Claim]:
    prose = CODE_BLOCK.sub(" [code block omitted] ", text)
    items = await complete_json(
        EXTRACT_SYSTEM,
        f"User asked:\n{context}\n\nAssistant reply:\n{prose}",
        max_tokens=1500,
        model=fast_model_name(),  # G1 latency: extraction is simple, use the fast model
    )
    if not isinstance(items, list):
        raise LLMError("extraction did not return a list")
    out: list[Claim] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        # FR-B4: claims about the user's own private context can't be checked against public
        # sources; checking them only produces false alarms (docs/requests.md 04:55, Role A).
        if str(it.get("scope", "public")).lower() == "private" and it.get("type") not in (
            "paper", "url", "package"
        ):
            continue
        quote = repair_quote(str(it.get("quote", "")), text)
        if not quote:
            continue
        ctype = it.get("type", "other")
        if ctype not in ("fact", "paper", "url", "package", "number", "other"):
            ctype = "other"
        risk = it.get("risk", "medium")
        out.append(
            Claim(
                claim_id=_new_id(),
                message_id=message_id,
                quote=quote,
                normalized=str(it.get("normalized") or quote),
                type=ctype,
                risk=risk if risk in ("high", "medium", "low") else "medium",
                question=it.get("question") or None,
                context=context,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Fast path (G1 latency): unambiguous references found instantly, without the LLM, so the
# Reference Auditor can start while LLM extraction is still running (see session.py).
# Deliberately conservative: a paper needs a quoted title AND (year / et al. / DOI), so a
# random "(2019)" in prose never becomes a paper claim.
# ---------------------------------------------------------------------------
_QUOTED_TITLE = re.compile(r"[\"“]([^\"”]{10,})[\"”]")


def _context(session: SessionContext, message_id: str) -> str:
    user_msg = session.previous(message_id, "user")
    return (user_msg.text if user_msg else "")[:CONTEXT_CHARS]


def fast_reference_claims(session: SessionContext, message_id: str, text: str) -> list[Claim]:
    context = _context(session, message_id)
    prose = CODE_BLOCK.sub(" ", text)
    out: list[Claim] = []
    seen: set[str] = set()

    def add(quote: str, ctype: str, normalized: str) -> None:
        if not quote or quote in seen or quote not in text or is_hedge(quote):
            return
        seen.add(quote)
        out.append(Claim(claim_id=_new_id(), message_id=message_id, quote=quote,
                         normalized=normalized, type=ctype,  # type: ignore[arg-type]
                         risk="high", context=context))

    for line in prose.splitlines():
        m = PAPER.search(line)
        if not m:
            continue
        quote = m.group(0).strip().strip("-*•0123456789. ").strip()
        title = _QUOTED_TITLE.search(quote)
        if title:
            add(quote, "paper", f"The paper '{title.group(1).strip()}' exists.")
    for m in URL.finditer(prose):
        url = m.group(0).rstrip(".,;:!?")
        add(url, "url", f"The URL {url} exists.")
    for m in PIP.finditer(text):
        add(m.group(1), "package", f"The Python package '{m.group(1)}' exists on PyPI.")
    return out[:MAX_CLAIMS]


def overlaps(claim: Claim, ref: Claim) -> bool:
    """True if `claim` (from the LLM/heuristic) is the same reference as fast-path `ref`."""
    a, b = claim.quote.lower(), ref.quote.lower()
    if a in b or b in a:
        return True
    title = _QUOTED_TITLE.search(ref.quote)
    return bool(title and title.group(1).lower() in a)


async def extract_claims(
    session: SessionContext, message_id: str, text: str, exclude: list[Claim] | None = None
) -> list[Claim]:
    """Full extraction. `exclude` = fast-path claims already being checked; overlapping
    claims are dropped so the same reference is never checked twice."""
    context = _context(session, message_id)

    claims = _code_claims(text, message_id, context)
    prose_claims: list[Claim] = []
    if llm_available():
        try:
            prose_claims = await _llm_claims(text, message_id, context)
        except LLMError as exc:
            log.warning("LLM extraction failed, using heuristic: %s", exc)
    if not prose_claims:
        prose_claims = _heuristic_claims(text, message_id, context)

    prose_claims = [c for c in prose_claims if not is_hedge(c.quote)]
    exclude = exclude or []
    prose_claims = [c for c in prose_claims if not any(overlaps(c, r) for r in exclude)]
    claims = (claims + prose_claims)[: max(0, MAX_CLAIMS - len(exclude))]
    tag_source_summary(session, message_id, claims)
    return claims


# ---------------------------------------------------------------------------
# FR-B11 source documents
# ---------------------------------------------------------------------------
def active_source_doc_id(session: SessionContext, assistant_message_id: str) -> str | None:
    """If the user message right before this reply pasted a doc (or asked about the latest
    doc), return that doc's id."""
    user_msg = session.previous(assistant_message_id, "user")
    if not user_msg or not session.source_docs:
        return None
    for doc in session.source_docs.values():
        if doc.message_id == user_msg.message_id:
            return doc.doc_id
    if SOURCE_INTENT.search(user_msg.text):
        latest = max(
            session.source_docs.values(),
            key=lambda d: (session.message(d.message_id).position if session.message(d.message_id) else -1),
        )
        return latest.doc_id
    return None


def tag_source_summary(session: SessionContext, message_id: str, claims: list[Claim]) -> None:
    doc_id = active_source_doc_id(session, message_id)
    if not doc_id:
        return
    for c in claims:
        if c.type in ("fact", "number", "other"):
            c.type = "source_summary"
            c.source_ref = doc_id
