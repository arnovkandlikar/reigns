"""Claim Verifier (FR-C2) — checks factual claims against web search + Wikipedia.

Owner: Role C. Hallucination type #2 in PRD §7.0 ("false checkable facts": wrong dates,
numbers, names, statistics that a search can disprove).

Pipeline for one claim:
  1. Gather evidence in parallel (each source is optional; failures are tolerated):
       - web search: Tavily (TAVILY_API_KEY) or Brave (BRAVE_API_KEY), 5 results
       - Wikipedia: search for the best-matching article → REST summary + search snippet
  2. Judge LLM compares the claim to the numbered snippets and returns
       supported / contradicted / unverified + a VERBATIM quote + which snippet it came from.
  3. We verify the quote really appears in that snippet. A "contradicted" whose quote we
     can't find is downgraded to "unverified" — the judge must never produce a red flag
     from a quote it invented (PRD §8.4 precision rule).

Hard rules (FR-C2):
  - No evidence found → "unverified" (never "contradicted" without evidence). That verdict
    also tells the engine to run the Consistency Probe next (app/triage.py, stage 2).
  - Only the claim's normalized text goes to search APIs — never the conversation (§14 r13).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote as urlquote
from urllib.parse import urlparse

import httpx

from app.detectors.base import (
    BaseDetector,
    content_words,
    http_client,
    looks_like_instruction,
    normalize_text,
    snippet,
)
from app.detectors import experience
from app.detectors.claim_gate import gate
from app.llm import complete_json
from app.models import Claim, DetectorResult, Evidence, SessionContext

log = logging.getLogger("reigns.detectors.claim_verifier")

TAVILY_URL = "https://api.tavily.com/search"
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
WIKI_API_URL = "https://en.wikipedia.org/w/api.php"

SEARCH_RESULTS = 5  # FR-C2: 5 results
WIKI_ARTICLES = 2  # top Wikipedia articles read in full
WIKI_SENTENCES = 3  # best-matching sentences kept per article (+ the first 2)
SNIPPET_CHARS = 700  # per snippet sent to the judge — enough context, bounded cost
MAX_SNIPPETS = 14  # 2 articles × ≤5 sentences + web results

# Minimum judge confidence before we let a verdict stand as supported/contradicted.
MIN_DECISIVE_CONFIDENCE = 0.6
# "contradicted" turns the pet red, so it needs more: live testing showed the judge giving 0.70
# to an inference ("he was born in 1854, so he can't have been mayor in 1866") — maybe a
# different person with the same name. Real contradictions (1899 vs 1889) score ~0.99.
MIN_CONTRADICT_CONFIDENCE = 0.8


@dataclass
class Snippet:
    source: str  # "Wikipedia", or the website's domain for search results
    url: str | None
    text: str


JUDGE_SYSTEM = """You are a careful fact-checker. You get ONE claim made by an AI assistant
and numbered evidence snippets from web search and Wikipedia.

Decide, using ONLY the snippets (not your own memory):
- "supported": a snippet clearly states the same fact.
- "contradicted": a snippet clearly states something incompatible with the claim
  (a different date, number, name, place …).
- "unverified": the snippets don't settle it, are off-topic, or disagree with each other.
- "not_checkable": the claim is NOT a fact about the world that a public source could state —
  it's advice or an instruction ("sleep 0.6 s between calls"), arithmetic about the user's own
  setup ("that's about 1,500 cities per run"), an opinion/recommendation/best-practice tip
  ("Flask is a good fit", "it works best when the rows are sorted"), or it only makes sense
  with the earlier conversation ("it returns a new DataFrame").

Rules:
- "quote" MUST be copied character-for-character from ONE snippet, and must be the sentence
  or phrase that proves your verdict. For "unverified" use "".
- "evidence_index" is the number of the snippet you quoted (null for "unverified").
- "supported" ONLY when a snippet states the SAME fact about the SAME subject (same person,
  place, library, function, parameter …). A related or general fact is NOT support: e.g.
  "urllib3 has a Retry class" does not support "requests.get() has a retries argument".
  If the snippets are only about something nearby, answer "unverified".
- "contradicted" ONLY when a snippet DIRECTLY states a conflicting fact about the SAME
  subject. Do not reason from indirect facts (birth dates, related events, "so it couldn't
  have …"), and be careful with people who may merely share a name. If you have to infer,
  answer "unverified".
- Small rounding or phrasing differences ("about 330 m" vs "330 metres") are NOT
  contradictions. Different years, names or clearly different numbers ARE.
- "explanation" is one short plain-English sentence for a non-expert, e.g.
  "Wikipedia says it was completed in 1889, not 1899."
- You may also get PAST CONFIRMED CASES: similar claims checked before. They show how
  evidence was read in the past; they are NOT evidence for this claim. Never quote them and
  never base the verdict on them alone.

Return {"verdict": "supported|contradicted|unverified|not_checkable", "confidence": 0.0-1.0,
        "evidence_index": <int or null>, "quote": "<verbatim or empty>",
        "explanation": "<one sentence>"}"""

JudgeFn = Callable[[str, str], Awaitable[Any]]


class ClaimVerifier(BaseDetector):
    name = "claim_verifier"

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        judge: JudgeFn | None = None,
        gate_judge: JudgeFn | None = None,
        experience_fn: Callable[[str, str], Awaitable[list]] | None = None,
    ) -> None:
        self.gate_judge = gate_judge  # tests inject the Claim Gate's model
        # FR-L3: similar confirmed past cases (Atlas Vector Search); tests inject a fake.
        self.experience_fn = experience_fn or experience.similar_cases
        # Both injectable so tests run offline with canned search results and judge answers.
        self.transport = transport
        self.judge: JudgeFn = judge or complete_json

    def _client(self) -> httpx.AsyncClient:
        return http_client(transport=self.transport) if self.transport else http_client()

    # ------------------------------------------------------------------ main flow
    async def _check(self, claim: Claim, session: SessionContext) -> DetectorResult | None:
        if looks_like_instruction(claim.quote):
            return None  # advice/instructions aren't facts a search can confirm
        g = await gate(claim, session, self.gate_judge)
        if not g.checkable:
            return None  # advice / opinion / the user's own context / meta: no source can say
        claim = g.resolved(claim)  # context-resolved, standalone text (long-chat root cause)
        query = (claim.normalized or claim.quote).strip()[:300]  # privacy: claim text only

        # FR-L3: look up similar past cases WHILE searching, so it adds no latency.
        past = asyncio.ensure_future(self._past_cases(query, claim.type))
        try:
            snippets = await self.cached(
                session, f"evidence:{normalize_text(query)}", lambda: self._gather(query)
            )
        except BaseException:
            past.cancel()
            raise
        if not snippets:
            past.cancel()
            return self.result("unverified", 0.5, "No sources found that confirm or deny this.")

        examples = await past
        verdict = await self.cached(
            session,
            f"judge:{normalize_text(query)}",
            lambda: self._judge(claim, snippets, examples),
        )
        return self._to_result(verdict, snippets)

    async def _past_cases(self, query: str, claim_type: str) -> str:
        """Experience memory block for the judge ("" when Atlas is off or nothing is similar)."""
        try:
            cases = await self.experience_fn(query, claim_type)
            if cases:
                log.info("claim_verifier: %d similar past case(s) for the judge", len(cases))
            return experience.examples_block(cases)
        except Exception as exc:  # noqa: BLE001 (FR-L3: skip silently)
            log.info("claim_verifier: experience lookup failed: %r", exc)
            return ""

    async def _gather(self, query: str) -> list[Snippet]:
        """Search + Wikipedia in parallel. Returns [] only if every source came back empty."""
        web, wiki = await asyncio.gather(
            self._web_search(query), self._wikipedia(query), return_exceptions=True
        )
        for name, part in (("web search", web), ("Wikipedia", wiki)):
            if isinstance(part, BaseException):
                log.warning("claim_verifier: %s failed: %r", name, part)
        snippets: list[Snippet] = []
        # Wikipedia first: the most reliable source for the judge to lean on.
        for part in (wiki, web):
            if isinstance(part, list):
                snippets.extend(part)
        if isinstance(web, BaseException) and isinstance(wiki, BaseException):
            # Both sources errored (network down / bad key) → let check() report status "error"
            # instead of a misleading "no sources found".
            raise ConnectionError(f"all evidence sources failed: {web!r}; {wiki!r}")
        # de-duplicate identical snippets, keep order, cap
        seen: set[str] = set()
        unique = []
        for s in snippets:
            key = f"{s.url}|{normalize_text(s.text)}"  # same article can give several sentences
            if key not in seen and s.text.strip():
                seen.add(key)
                unique.append(s)
        return unique[:MAX_SNIPPETS]

    # ------------------------------------------------------------------ evidence sources
    async def _web_search(self, query: str) -> list[Snippet]:
        tavily, brave = os.environ.get("TAVILY_API_KEY"), os.environ.get("BRAVE_API_KEY")
        if tavily:
            return await self._tavily(query, tavily)
        if brave:
            return await self._brave(query, brave)
        return []  # no search key configured → Wikipedia only

    async def _tavily(self, query: str, key: str) -> list[Snippet]:
        body = {"query": query, "max_results": SEARCH_RESULTS, "search_depth": "basic"}
        async with self._client() as client:
            resp = await client.post(
                TAVILY_URL, json=body, headers={"Authorization": f"Bearer {key}"}
            )
            if resp.status_code in (400, 401):  # older Tavily accounts: key in the body
                resp = await client.post(TAVILY_URL, json={**body, "api_key": key})
            resp.raise_for_status()
            data = resp.json()
        return [
            Snippet(source=_domain(r.get("url")), url=r.get("url"), text=r.get("content", ""))
            for r in data.get("results", [])[:SEARCH_RESULTS]
        ]

    async def _brave(self, query: str, key: str) -> list[Snippet]:
        async with self._client() as client:
            resp = await client.get(
                BRAVE_URL,
                params={"q": query, "count": SEARCH_RESULTS},
                headers={"X-Subscription-Token": key, "Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
        return [
            Snippet(
                source=_domain(r.get("url")),
                url=r.get("url"),
                text=_strip_html(r.get("description", "")),
            )
            for r in (data.get("web") or {}).get("results", [])[:SEARCH_RESULTS]
        ]

    async def _wikipedia(self, query: str) -> list[Snippet]:
        """Find the best articles, read them in full, keep the sentences that match the claim.

        Why not just the REST summary (the article intro)? Live testing showed the intro often
        lacks the fact: the Eiffel Tower intro doesn't mention its height, so a correct
        "330 metres" claim came back unverified. Reading the whole article and picking the
        sentences that best match the claim fixes that at the cost of one extra request.
        """
        async with self._client() as client:
            resp = await client.get(
                WIKI_API_URL,
                params={
                    "action": "query",
                    "list": "search",
                    # MediaWiki search wants keywords; a full sentence finds almost nothing.
                    "srsearch": wiki_keywords(query),
                    "srlimit": WIKI_ARTICLES,
                    "format": "json",
                },
            )
            resp.raise_for_status()
            hits = resp.json().get("query", {}).get("search", [])

            async def best_sentences(title: str) -> list[Snippet]:
                r = await client.get(
                    WIKI_API_URL,
                    params={
                        "action": "query",
                        "prop": "extracts",
                        "explaintext": 1,
                        "titles": title,
                        "format": "json",
                        "redirects": 1,
                    },
                )
                r.raise_for_status()
                pages = r.json().get("query", {}).get("pages", {})
                text = next((pg.get("extract", "") for pg in pages.values()), "")
                url = f"https://en.wikipedia.org/wiki/{urlquote(title.replace(' ', '_'))}"
                return [Snippet("Wikipedia", url, sent) for sent in pick_sentences(text, query)]

            parts = await asyncio.gather(
                *(best_sentences(h["title"]) for h in hits), return_exceptions=True
            )
        return [sn for p in parts if isinstance(p, list) for sn in p]

    # ------------------------------------------------------------------ judge
    async def _judge(
        self, claim: Claim, snippets: list[Snippet], examples: str = ""
    ) -> dict[str, Any]:
        numbered = "\n\n".join(
            f"[{i}] ({s.source}) {s.text[:SNIPPET_CHARS]}" for i, s in enumerate(snippets)
        )
        user = f"CLAIM: {claim.normalized}\n"
        if claim.quote and claim.quote != claim.normalized:
            user += f"(original wording: {claim.quote[:300]})\n"
        user += f"\nEVIDENCE SNIPPETS:\n{numbered}"
        if examples:
            user += f"\n\n{examples}"
        data = await self.judge(JUDGE_SYSTEM, user)
        if not isinstance(data, dict):
            raise TypeError(f"judge returned {type(data).__name__}, expected an object")
        return data

    def _to_result(self, data: dict[str, Any], snippets: list[Snippet]) -> DetectorResult | None:
        verdict = str(data.get("verdict", "unverified")).lower().strip()
        if verdict == "not_checkable":
            return None  # nothing to say — don't turn advice/opinions/context-talk amber
        if verdict not in ("supported", "contradicted", "unverified"):
            verdict = "unverified"
        try:
            confidence = float(data.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        explanation = str(data.get("explanation") or "").strip()
        quote = str(data.get("quote") or "").strip()

        if verdict == "unverified":
            # No evidence attached: nothing relevant was found, and showing a random snippet
            # (live run: an unrelated news sentence) in the Details panel would only confuse.
            return self.result(
                "unverified",
                min(confidence, 0.6),
                explanation or "The sources found don't confirm or deny this.",
            )

        # supported / contradicted: the quote must really be in the cited snippet.
        source = _locate_quote(quote, snippets, data.get("evidence_index"))
        if source is None:
            if verdict == "contradicted":
                # Precision rule: no verifiable evidence → not red. Amber at most.
                return self.result(
                    "unverified",
                    0.5,
                    "The sources seem to disagree with this, but I couldn't pin down the exact "
                    "passage.",
                    [
                        Evidence(source=s.source, url=s.url, snippet=snippet(s.text, 200))
                        for s in snippets[:2]
                    ],
                )
            confidence = min(confidence, 0.7)  # supported but unquotable → less sure
            source = snippets[0]
            quote = snippet(source.text, 200)

        needed = MIN_CONTRADICT_CONFIDENCE if verdict == "contradicted" else MIN_DECISIVE_CONFIDENCE
        if confidence < needed:
            return self.result(
                "unverified",
                confidence,
                explanation or "The sources don't clearly settle this.",
                [Evidence(source=source.source, url=source.url, snippet=snippet(quote))],
            )

        default = "Matches the sources." if verdict == "supported" else "The sources disagree."
        return self.result(
            verdict,  # type: ignore[arg-type]
            confidence,
            explanation or default,
            [Evidence(source=source.source, url=source.url, snippet=snippet(quote))],
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _locate_quote(quote: str, snippets: list[Snippet], index: Any) -> Snippet | None:
    """Return the snippet that really contains `quote` (after normalizing), else None.

    Checks the snippet the judge pointed at first, then all others (judges sometimes get the
    index off by one). Normalizing ignores case/punctuation/spacing differences only.
    """
    want = normalize_text(quote)
    if len(want) < 8:  # too short to prove anything ("1889" alone could match anywhere)
        return None
    order = list(snippets)
    if isinstance(index, int) and 0 <= index < len(snippets):
        order.insert(0, snippets[index])
    for s in order:
        if want in normalize_text(s.text):
            return s
    return None


_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])|\n+")


def wiki_keywords(claim_text: str, max_words: int = 6) -> str:
    """'The Eiffel Tower was completed in 1899.' → 'Eiffel Tower completed'.

    Drops stopwords and numbers (a wrong number in the claim must not stop us finding the
    right article) and keeps the first few meaningful words, which usually name the subject.
    """
    words = [
        w
        for w in re.findall(r"[^\W\d_][\w'’-]*", claim_text)
        if len(w) > 1 and w.lower() not in _QUERY_STOP
    ]
    return " ".join(words[:max_words]) or claim_text


_QUERY_STOP = {
    "the",
    "a",
    "an",
    "of",
    "in",
    "on",
    "at",
    "to",
    "for",
    "by",
    "with",
    "and",
    "or",
    "is",
    "was",
    "were",
    "are",
    "be",
    "been",
    "it",
    "its",
    "that",
    "this",
    "about",
    "approximately",
    "around",
    "including",
    "who",
    "which",
    "took",
    "has",
    "had",
    "have",
    "than",
    "as",
    "from",
}


def pick_sentences(
    text: str, claim_text: str, k: int = WIKI_SENTENCES, intro: int = 2
) -> list[str]:
    """The article's first `intro` sentences + the k that best match the claim (article order).

    - The intro is always kept: it holds the key facts (dates, places, sizes) and is where a
      contradiction usually lives — e.g. "Constructed from 1887 to 1889" for a claim saying
      1899 shares no number with the claim, so pure matching would drop it.
    - Score = shared meaningful words
            + 3 per number the claim mentions that the sentence also has   (supports)
            + 1 per claim number with a same-length number in the sentence (1899 ↔ 1889:
              the kind of sentence that can contradict it)
    """
    want_words = content_words(claim_text)
    want_numbers = set(_NUMBER.findall(claim_text))
    want_lengths = {len(n) for n in want_numbers}
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(text) if len(s.strip()) > 20]
    chosen = set(range(min(intro, len(sentences))))
    scored = []
    for i, sent in enumerate(sentences):
        if i in chosen:
            continue
        numbers = set(_NUMBER.findall(sent))
        score = (
            len(want_words & content_words(sent))
            + 3 * len(want_numbers & numbers)
            + len(want_lengths & {len(n) for n in numbers - want_numbers})
        )
        if score:
            scored.append((score, i))
    chosen |= {i for _, i in sorted(scored, key=lambda t: (-t[0], t[1]))[:k]}
    return [sentences[i] for i in sorted(chosen)]


_TAGS = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return _TAGS.sub("", text).replace("&quot;", '"').replace("&#039;", "'").replace("&amp;", "&")


def _domain(url: str | None) -> str:
    host = urlparse(url or "").hostname or "Web"
    return host.removeprefix("www.")


detector = ClaimVerifier()
