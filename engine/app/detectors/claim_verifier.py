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
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote as urlquote
from urllib.parse import urlparse

import httpx

from app.detectors.base import BaseDetector, http_client, normalize_text, snippet
from app.llm import complete_json
from app.models import Claim, DetectorResult, Evidence, SessionContext

TAVILY_URL = "https://api.tavily.com/search"
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
WIKI_SEARCH_URL = "https://en.wikipedia.org/w/api.php"
WIKI_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"

SEARCH_RESULTS = 5  # FR-C2: 5 results
WIKI_ARTICLES = 2  # summaries fetched for the top Wikipedia hits
SNIPPET_CHARS = 700  # per snippet sent to the judge — enough context, bounded cost
MAX_SNIPPETS = 8

# Minimum judge confidence before we let a verdict stand as supported/contradicted.
MIN_DECISIVE_CONFIDENCE = 0.6


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

Rules:
- "quote" MUST be copied character-for-character from ONE snippet, and must be the sentence
  or phrase that proves your verdict. For "unverified" use "".
- "evidence_index" is the number of the snippet you quoted (null for "unverified").
- Small rounding or phrasing differences ("about 330 m" vs "330 metres") are NOT
  contradictions. Different years, names or clearly different numbers ARE.
- "explanation" is one short plain-English sentence for a non-expert, e.g.
  "Wikipedia says it was completed in 1889, not 1899."

Return {"verdict": "supported|contradicted|unverified", "confidence": 0.0-1.0,
        "evidence_index": <int or null>, "quote": "<verbatim or empty>",
        "explanation": "<one sentence>"}"""

JudgeFn = Callable[[str, str], Awaitable[Any]]


class ClaimVerifier(BaseDetector):
    name = "claim_verifier"

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        judge: JudgeFn | None = None,
    ) -> None:
        # Both injectable so tests run offline with canned search results and judge answers.
        self.transport = transport
        self.judge: JudgeFn = judge or complete_json

    def _client(self) -> httpx.AsyncClient:
        return http_client(transport=self.transport) if self.transport else http_client()

    # ------------------------------------------------------------------ main flow
    async def _check(self, claim: Claim, session: SessionContext) -> DetectorResult:
        query = (claim.normalized or claim.quote).strip()[:300]  # privacy: claim text only

        snippets = await self.cached(
            session, f"evidence:{normalize_text(query)}", lambda: self._gather(query)
        )
        if not snippets:
            return self.result("unverified", 0.5, "No sources found that confirm or deny this.")

        verdict = await self.cached(
            session, f"judge:{normalize_text(query)}", lambda: self._judge(claim, snippets)
        )
        return self._to_result(verdict, snippets)

    async def _gather(self, query: str) -> list[Snippet]:
        """Search + Wikipedia in parallel. Returns [] only if every source came back empty."""
        web, wiki = await asyncio.gather(
            self._web_search(query), self._wikipedia(query), return_exceptions=True
        )
        snippets: list[Snippet] = []
        # Wikipedia first: the most reliable source for the judge to lean on.
        for part in (wiki, web):
            if isinstance(part, list):
                snippets.extend(part)
        if isinstance(web, BaseException) and isinstance(wiki, BaseException):
            # Both sources errored (network down / bad key) → let check() report status "error"
            # instead of a misleading "no sources found".
            raise ConnectionError(f"all evidence sources failed: {web!r}; {wiki!r}")
        # de-duplicate by URL, keep order, cap
        seen: set[str] = set()
        unique = []
        for s in snippets:
            key = s.url or s.text[:80]
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
        """Find the best articles with MediaWiki search, then fetch their REST summaries."""
        async with self._client() as client:
            resp = await client.get(
                WIKI_SEARCH_URL,
                params={
                    "action": "query",
                    "list": "search",
                    "srsearch": query,
                    "srlimit": WIKI_ARTICLES,
                    "format": "json",
                },
            )
            resp.raise_for_status()
            hits = resp.json().get("query", {}).get("search", [])

            async def summary(hit: dict[str, Any]) -> list[Snippet]:
                title = hit["title"]
                url = f"https://en.wikipedia.org/wiki/{urlquote(title.replace(' ', '_'))}"
                out = []
                r = await client.get(
                    WIKI_SUMMARY_URL.format(title=urlquote(title.replace(" ", "_"), safe=""))
                )
                if r.status_code == 200 and r.json().get("extract"):
                    out.append(Snippet("Wikipedia", url, r.json()["extract"]))
                # The search snippet is the passage that matched the query — often exactly the
                # sentence with the fact, even when the summary doesn't mention it.
                if hit.get("snippet"):
                    out.append(Snippet("Wikipedia", url, _strip_html(hit["snippet"])))
                return out

            parts = await asyncio.gather(*(summary(h) for h in hits), return_exceptions=True)
        return [s for p in parts if isinstance(p, list) for s in p]

    # ------------------------------------------------------------------ judge
    async def _judge(self, claim: Claim, snippets: list[Snippet]) -> dict[str, Any]:
        numbered = "\n\n".join(
            f"[{i}] ({s.source}) {s.text[:SNIPPET_CHARS]}" for i, s in enumerate(snippets)
        )
        user = f"CLAIM: {claim.normalized}\n"
        if claim.quote and claim.quote != claim.normalized:
            user += f"(original wording: {claim.quote[:300]})\n"
        user += f"\nEVIDENCE SNIPPETS:\n{numbered}"
        data = await self.judge(JUDGE_SYSTEM, user)
        if not isinstance(data, dict):
            raise TypeError(f"judge returned {type(data).__name__}, expected an object")
        return data

    def _to_result(self, data: dict[str, Any], snippets: list[Snippet]) -> DetectorResult:
        verdict = str(data.get("verdict", "unverified")).lower().strip()
        if verdict not in ("supported", "contradicted", "unverified"):
            verdict = "unverified"
        try:
            confidence = float(data.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        explanation = str(data.get("explanation") or "").strip()
        quote = str(data.get("quote") or "").strip()

        if verdict == "unverified":
            return self.result(
                "unverified",
                min(confidence, 0.6),
                explanation or "The sources found don't confirm or deny this.",
                [
                    Evidence(source=s.source, url=s.url, snippet=snippet(s.text, 200))
                    for s in snippets[:2]
                ],
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

        if confidence < MIN_DECISIVE_CONFIDENCE:
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


_TAGS = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return _TAGS.sub("", text).replace("&quot;", '"').replace("&#039;", "'").replace("&amp;", "&")


def _domain(url: str | None) -> str:
    host = urlparse(url or "").hostname or "Web"
    return host.removeprefix("www.")


detector = ClaimVerifier()
