"""Reference Auditor (FR-C1) — catches invented papers, dead links and fake packages.

Owner: Role C. Hallucination type #1 in PRD §7.0 ("fake references"), and the demo's opening
moment (§11 step 1), so precision matters most here: we only say `contradicted` when a real
lookup succeeded and found nothing close.

Claim types handled (routed here by app/triage.py):
  paper   → Crossref + Semantic Scholar + OpenAlex title search, in parallel
  url     → HTTP GET; 404/410 or DNS failure → contradicted
  package → PyPI JSON API (or the npm registry for JavaScript packages)

Verdict rules for papers (FR-C1):
  supported     title similarity ≥ 0.85 AND an author surname matches AND year within ±1
  unverified    a partial match (title close, but authors/year don't line up), or the title is
                only somewhat similar — "when in doubt → amber" (§8.4 precision rule)
  contradicted  every source that answered found nothing close ("no such paper found")
  error         no source answered (network down / rate-limited) → engine marks it skipped
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import weakref
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote as urlquote
from urllib.parse import urlencode, urlparse

import httpx

from app.detectors.base import (
    BaseDetector,
    http_client,
    normalize_text,
    similarity,
    snippet,
    word_overlap,
)
from app.models import Claim, DetectorResult, Evidence, SessionContext

# --- thresholds (tune with Role D's eval results, §6.1 h24–30) --------------------------------
TITLE_MATCH = 0.85  # FR-C1: normalized title similarity for "same paper"
# A "near-miss" (→ amber) needs BOTH close spelling AND shared meaningful words. 0.60 on
# spelling alone let unrelated titles about the same topic count as near-misses (Arnov's live
# run: a fake "HiveFormer …Monitoring of Beehives" matched "MUS-Tracker …Monitoring of
# Beehives" → amber instead of red).
TITLE_PARTIAL = 0.75
WORDS_PARTIAL = 0.50
YEAR_TOLERANCE = 1  # FR-C1: year ±1 (preprint vs. journal year)
# Title + authors match but the record is LATER than the cited year (re-registrations, later
# editions, journal versions of old preprints) → still the real paper, slightly less sure.
MAX_LATER_RECORD_YEARS = 10
SEARCH_ROWS = 5  # FR-C1: rows=5

# Rate limits: Crossref and Semantic Scholar both return 429 when one reply cites several
# papers and we query them all at once. So calls to each API go one at a time (the two APIs
# still run in parallel with each other), and a 429 is retried once after Retry-After
# (capped so we stay inside the engine's 12 s detector budget).
MAX_RETRY_WAIT_S = 2.0

CROSSREF_URL = "https://api.crossref.org/works"
S2_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
# OpenAlex: free, no key, generous rate limits, indexes arXiv/NeurIPS with original years.
# Added after a live run where S2 rate-limited us and Crossref's only "Attention Is All You
# Need" record was a 2025 re-registration, so a real 2017 paper came back amber.
OPENALEX_URL = "https://api.openalex.org/works"
PYPI_URL = "https://pypi.org/pypi/{name}/json"
NPM_URL = "https://registry.npmjs.org/{name}"


# =============================================================================================
# Parsing the claim text
# =============================================================================================
@dataclass
class PaperRef:
    """What the AI claimed about a paper, pulled out of the claim's text."""

    title: str
    surnames: list[str] = field(default_factory=list)
    year: int | None = None


# Titles are normally quoted: "…", “…”, or '…' (the extractor's normalized form uses '…').
_QUOTED = re.compile(r"\"([^\"]{6,})\"|“([^”]{6,})”|'([^']{6,})'")
_YEAR = re.compile(r"\b(19[5-9]\d|20[0-4]\d)\b")
# Capitalized words, allowing accents, hyphens and apostrophes (O'Neil, García-Márquez).
_NAME_WORD = re.compile(r"\b[A-Z][\w'’\-]+", re.UNICODE)
_NOT_NAMES = {"Et", "Al", "And", "The", "In", "A", "An", "Of", "On", "Paper", "Proceedings"}


def parse_paper(claim: Claim) -> PaperRef:
    """Extract title, author surnames and year.

    Handles the shapes the extractor produces, e.g.
        quote:      Lee & Park (2022), "Transformer Models for Honeybee Colony Collapse Forecasting"
        normalized: The paper 'Transformer Models for …' exists.
    """
    text = claim.quote
    title = ""
    title_start = len(text)
    for source in (claim.quote, claim.normalized):
        m = _QUOTED.search(source)
        if m:
            title = next(g for g in m.groups() if g)
            if source is claim.quote:
                title_start = m.start()
            break
    if not title:  # no quotes anywhere → best effort: the whole quote is the title
        title = claim.quote

    years = _YEAR.findall(text) or _YEAR.findall(claim.normalized)
    year = int(years[0]) if years else None

    # Authors are written before the title ("Lee & Park (2022), …"). Only look there so words
    # from the title itself ("Transformer", "Models") aren't mistaken for surnames.
    author_part = text[:title_start]
    author_part = _YEAR.sub(" ", author_part)
    surnames = [w for w in _NAME_WORD.findall(author_part) if w not in _NOT_NAMES]
    return PaperRef(title=title.strip().rstrip(".,"), surnames=surnames, year=year)


_URL = re.compile(r"https?://[^\s<>\"'`)\]]+")


def parse_url(claim: Claim) -> str | None:
    for source in (claim.quote, claim.normalized):
        m = _URL.search(source)
        if m:
            return m.group(0).rstrip(".,;:!?")
    return None


def attributed_text(claim: Claim, url: str) -> str | None:
    """A quoted passage the reply says the page contains (≥ 12 chars), if any (FR-C1)."""
    rest = claim.quote.replace(url, " ")
    m = _QUOTED.search(rest)
    if m:
        passage = next(g for g in m.groups() if g)
        if len(passage) >= 12:
            return passage
    return None


# pip/npm install lines, backticks, or quotes around the package name.
_INSTALL = re.compile(
    r"\b(?:pip3?|npm|yarn|pnpm|poetry|uv(?: pip)?)\s+(?:install|add|i)\s+([^\n;&|`'\"]+)"
)
# Words that end a package list in prose: "pip install requests to fetch the page".
_STOPWORDS = {"to", "and", "then", "or", "for", "with", "in", "first", "if", "so", "via", "from"}
_NAMED_LIB = re.compile(
    r"\b([a-z][a-z0-9_.\-]*[a-z0-9])\s+(?:library|package|module|lib)\b", re.IGNORECASE
)
_TICKED = re.compile(r"`([^`\s]+)`")
_PKG_NAME = re.compile(r"^@?[A-Za-z0-9][A-Za-z0-9._\-]*(/[A-Za-z0-9._\-]+)?$")
_JS_HINT = re.compile(
    r"\b(npm|yarn|pnpm|node(?:\.js)?|javascript|typescript|js|ts)\b", re.IGNORECASE
)


def parse_packages(claim: Claim) -> tuple[list[str], str]:
    """Return (package names, ecosystem) where ecosystem is "pypi" or "npm"."""
    text = claim.quote
    names: list[str] = []
    m = _INSTALL.search(text)
    if m:
        for tok in m.group(1).split():
            tok = tok.strip(".,:;()")
            if tok.lower() in _STOPWORDS:
                break
            if tok and not tok.startswith("-"):  # skip flags like -U, --upgrade
                names.append(tok)
    if not names:
        names = _TICKED.findall(text)
    if not names:
        # Prose like "the boto3 library's upload_file" or "the httpx package".
        names = _NAMED_LIB.findall(text)
    if not names:
        q = _QUOTED.search(text) or _QUOTED.search(claim.normalized)
        if q:
            names = [next(g for g in q.groups() if g)]
    # strip version pins / extras: requests==2.0, fastapi[all], lodash@4
    cleaned = []
    for n in names:
        n = re.split(r"[=<>~!\[;]", n)[0]
        if n.startswith("@"):  # npm scoped: keep @scope/name, drop a trailing @version
            n = "@" + n[1:].split("@")[0]
        else:
            n = n.split("@")[0]
        if _PKG_NAME.match(n) and n not in cleaned:
            cleaned.append(n)
    hints = f"{text} {claim.normalized} {claim.context}"
    ecosystem = "npm" if _JS_HINT.search(hints) else "pypi"
    return cleaned, ecosystem


# =============================================================================================
# Lookups — each returns plain data so it can live in session.cache (FR-C5)
# =============================================================================================
@dataclass
class Candidate:
    source: str  # "Crossref" | "Semantic Scholar"
    title: str
    surnames: list[str]
    year: int | None
    url: str | None


def _crossref_candidates(data: dict[str, Any]) -> list[Candidate]:
    out = []
    for item in data.get("message", {}).get("items", []):
        titles = item.get("title") or []
        if not titles:
            continue
        parts = (item.get("issued") or {}).get("date-parts") or [[None]]
        year = parts[0][0] if parts and parts[0] else None
        doi = item.get("DOI")
        out.append(
            Candidate(
                source="Crossref",
                title=titles[0],
                surnames=[a["family"] for a in item.get("author", []) if a.get("family")],
                year=year if isinstance(year, int) else None,
                url=f"https://doi.org/{doi}" if doi else None,
            )
        )
    return out


def _s2_candidates(data: dict[str, Any]) -> list[Candidate]:
    out = []
    for p in data.get("data") or []:
        if not p.get("title"):
            continue
        doi = (p.get("externalIds") or {}).get("DOI")
        out.append(
            Candidate(
                source="Semantic Scholar",
                title=p["title"],
                # S2 gives full names ("Ashish Vaswani"); the surname is the last word.
                surnames=[a["name"].split()[-1] for a in p.get("authors", []) if a.get("name")],
                year=p.get("year"),
                url=f"https://doi.org/{doi}" if doi else p.get("url"),
            )
        )
    return out


def _join_or(names: list[str]) -> str:
    """["A"] → "A";  ["A", "B"] → "A or B";  ["A", "B", "C"] → "A, B or C"."""
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def _openalex_candidates(data: dict[str, Any]) -> list[Candidate]:
    out = []
    for w in data.get("results") or []:
        if not w.get("display_name"):
            continue
        names = [
            (a.get("author") or {}).get("display_name", "") for a in w.get("authorships") or []
        ]
        out.append(
            Candidate(
                source="OpenAlex",
                title=w["display_name"],
                surnames=[n.split()[-1] for n in names if n and n.split()],
                year=w.get("publication_year"),
                url=w.get("doi") or w.get("id"),
            )
        )
    return out


# One Semaphore(1) per (event loop, API). Keyed by loop because an asyncio primitive can only
# be used on the loop it was first used on, and tests start a new loop per test.
_LOCKS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _api_lock(api: str) -> asyncio.Semaphore:
    per_loop = _LOCKS.setdefault(asyncio.get_running_loop(), {})
    if api not in per_loop:
        per_loop[api] = asyncio.Semaphore(1)
    return per_loop[api]


def _retry_after(resp: httpx.Response) -> float:
    """Seconds to wait before retrying a 429: the Retry-After header, capped at 2 s."""
    try:
        wait = float(resp.headers.get("Retry-After", "1"))
    except ValueError:  # Retry-After can also be an HTTP date; don't bother parsing it
        wait = 1.0
    return max(0.0, min(wait, MAX_RETRY_WAIT_S))


def _is_private_host(host: str) -> bool:
    """Never let an AI-written URL make the engine probe localhost / the LAN (SSRF guard)."""
    if host in ("localhost",) or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved


# =============================================================================================
# The detector
# =============================================================================================
class ReferenceAuditor(BaseDetector):
    name = "reference_auditor"

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        # Tests inject an httpx.MockTransport here so they run offline and deterministically.
        self.transport = transport

    def _client(self) -> httpx.AsyncClient:
        return http_client(transport=self.transport) if self.transport else http_client()

    async def _check(self, claim: Claim, session: SessionContext) -> DetectorResult | None:
        if claim.type == "paper":
            return await self._check_paper(claim, session)
        if claim.type == "url":
            return await self._check_url(claim, session)
        if claim.type == "package":
            return await self._check_package(claim, session)
        return self.result("unverified", 0.0, "Not a reference claim, so it wasn't checked.")

    # ---------------------------------------------------------------------- papers
    async def _check_paper(self, claim: Claim, session: SessionContext) -> DetectorResult | None:
        ref = parse_paper(claim)
        if len(normalize_text(ref.title)) < 6:
            return None  # can't tell what to look up → say nothing (long-chat fix)

        # All three searches at once; each is cached per title so a paper cited twice is looked
        # up once. return_exceptions=True: one API failing must not sink the others.
        key = normalize_text(f"{ref.title} {' '.join(ref.surnames)}")
        searches = {
            "Crossref": self.cached(session, f"crossref:{key}", lambda: self._search_crossref(ref)),
            "Semantic Scholar": self.cached(session, f"s2:{key}", lambda: self._search_s2(ref)),
            "OpenAlex": self.cached(session, f"openalex:{key}", lambda: self._search_openalex(ref)),
        }
        results = await asyncio.gather(*searches.values(), return_exceptions=True)
        answered: dict[str, list[Candidate]] = {
            name: res for name, res in zip(searches, results) if not isinstance(res, BaseException)
        }
        if not answered:
            raise ConnectionError(f"all paper databases unavailable ({results[0]!r})")

        candidates = [c for cands in answered.values() for c in cands]
        scored = sorted(
            ((similarity(ref.title, c.title), c) for c in candidates),
            key=lambda pair: pair[0],
            reverse=True,
        )

        # 1) A full match anywhere → supported.
        for score, c in scored:
            if score >= TITLE_MATCH and self._authors_ok(ref, c) and self._year_ok(ref, c):
                return self.result(
                    "supported",
                    0.98 if ref.surnames and ref.year else 0.85,
                    f"Found in {c.source} with matching title"
                    + (", authors" if ref.surnames else "")
                    + (" and year." if ref.year else "."),
                    [Evidence(source=c.source, url=c.url, snippet=snippet(self._cite(c)))],
                )

        # 1b) Title + authors match, but EVERY matching record is dated later than the cited
        #     year (re-registrations, later editions, journal versions of preprints — live run:
        #     Crossref lists the 2017 "Attention Is All You Need" as 2025) → still the real
        #     paper. One-way: citing a year after the paper appeared stays amber.
        matching = [
            c
            for score, c in scored
            if score >= TITLE_MATCH and ref.surnames and self._authors_ok(ref, c) and c.year
        ]
        if (
            matching
            and ref.year is not None
            and all(0 < c.year - ref.year <= MAX_LATER_RECORD_YEARS for c in matching)
        ):
            c = min(matching, key=lambda m: m.year)
            return self.result(
                "supported",
                0.85,
                f"Found in {c.source} with matching title and authors (records are dated "
                f"{c.year} or later, likely a later edition of the {ref.year} paper).",
                [Evidence(source=c.source, url=c.url, snippet=snippet(self._cite(c)))],
            )

        best_score, best = scored[0] if scored else (0.0, None)
        best_words = word_overlap(ref.title, best.title) if best is not None else 0.0

        # 2) Title matches but authors or year don't → the paper exists but was mis-cited.
        if best is not None and best_score >= TITLE_MATCH:
            problems = []
            if not self._authors_ok(ref, best):
                problems.append(f"authors are {', '.join(best.surnames[:3]) or 'unknown'}")
            if not self._year_ok(ref, best):
                problems.append(f"year is {best.year}")
            return self.result(
                "unverified",
                0.6,
                f"A paper with this title exists, but its {' and '.join(problems)}.",
                [Evidence(source=best.source, url=best.url, snippet=snippet(self._cite(best)))],
            )

        # 3) Something similar but not the same title → can't say it's fake (amber).
        if best is not None and best_score >= TITLE_PARTIAL and best_words >= WORDS_PARTIAL:
            return self.result(
                "unverified",
                0.5,
                f'Closest match is a different title: "{snippet(best.title, 120)}".',
                [Evidence(source=best.source, url=best.url, snippet=snippet(self._cite(best)))],
            )

        # 4) Every source that answered found nothing close → no such paper.
        evidence = [self._not_found_evidence(src, ref, cands) for src, cands in answered.items()]
        # More independent databases agreeing "no such paper" → more confident.
        confidence = {1: 0.85, 2: 0.92}.get(len(answered), 0.95)
        return self.result(
            "contradicted",
            confidence,
            f'No paper titled "{snippet(ref.title, 120)}" exists in '
            + _join_or(list(answered))
            + ".",
            evidence,
        )

    @staticmethod
    def _authors_ok(ref: PaperRef, c: Candidate) -> bool:
        if not ref.surnames:  # the reply named no authors → nothing to contradict
            return True
        theirs = {normalize_text(s) for s in c.surnames}
        return any(normalize_text(s) in theirs for s in ref.surnames)

    @staticmethod
    def _year_ok(ref: PaperRef, c: Candidate) -> bool:
        if ref.year is None or c.year is None:
            return True
        return abs(ref.year - c.year) <= YEAR_TOLERANCE

    @staticmethod
    def _cite(c: Candidate) -> str:
        authors = ", ".join(c.surnames[:3]) + (" …" if len(c.surnames) > 3 else "")
        return f"{c.title} — {authors or 'unknown authors'} ({c.year or 'n.d.'})"

    @staticmethod
    def _not_found_evidence(source: str, ref: PaperRef, cands: list[Candidate]) -> Evidence:
        best = max((similarity(ref.title, c.title) for c in cands), default=0.0)
        if source == "Crossref":
            url = f"{CROSSREF_URL}?{urlencode({'query.bibliographic': ref.title})}"
            text = "No matching work found" + (f" (best title match {best:.2f})" if cands else "")
        elif source == "OpenAlex":
            url = f"{OPENALEX_URL}?{urlencode({'search': ref.title})}"
            text = f"No matching work (best title match {best:.2f})"
        else:
            url = None
            text = f"No paper with a similar title (best match {best:.2f})"
        return Evidence(source=source, url=url, snippet=text)

    async def _search_crossref(self, ref: PaperRef) -> list[Candidate]:
        params: dict[str, Any] = {
            # Crossref's bibliographic query is built for exactly this: free-text citations.
            "query.bibliographic": " ".join([ref.title, *ref.surnames]),
            "rows": SEARCH_ROWS,
            "select": "DOI,title,author,issued",
        }
        mailto = os.environ.get("CROSSREF_MAILTO")
        if mailto:
            params["mailto"] = mailto
        resp = await self._api_get("crossref", CROSSREF_URL, params=params)
        return _crossref_candidates(resp.json())

    async def _search_s2(self, ref: PaperRef) -> list[Candidate]:
        headers = {}
        key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
        if key:  # without a key S2 shares a small public rate limit → expect some 429s
            headers["x-api-key"] = key
        params = {
            "query": ref.title,
            "limit": SEARCH_ROWS,
            "fields": "title,authors,year,externalIds,url",
        }
        resp = await self._api_get("s2", S2_URL, params=params, headers=headers)
        return _s2_candidates(resp.json())

    async def _search_openalex(self, ref: PaperRef) -> list[Candidate]:
        """Two queries, merged:

        1. relevance search (finds niche papers by their exact words)
        2. title-only search sorted by citation count (finds famous papers). Live run: for
           "Attention Is All You Need" the top-5 by relevance were all "Attention is all you
           need in <X>" follow-ups, so the real 100k-citation paper never showed up.
        """
        base: dict[str, Any] = {
            "per-page": SEARCH_ROWS,
            "select": "id,display_name,publication_year,authorships,doi",
        }
        mailto = os.environ.get("CROSSREF_MAILTO")  # OpenAlex has the same "polite pool" idea
        if mailto:
            base["mailto"] = mailto
        title_filter = re.sub(r"[,:|]", " ", ref.title)  # these characters break filter syntax
        queries = [
            {**base, "search": ref.title},
            {**base, "filter": f"title.search:{title_filter}", "sort": "cited_by_count:desc"},
        ]
        candidates: list[Candidate] = []
        for params in queries:
            resp = await self._api_get("openalex", OPENALEX_URL, params=params)
            candidates.extend(_openalex_candidates(resp.json()))
        return candidates

    async def _api_get(self, api: str, url: str, **kwargs: Any) -> httpx.Response:
        """GET with one-at-a-time access per API and a single retry on 429."""
        async with _api_lock(api), self._client() as client:
            resp = await client.get(url, **kwargs)
            if resp.status_code == 429:
                await asyncio.sleep(_retry_after(resp))
                resp = await client.get(url, **kwargs)
            resp.raise_for_status()
            return resp

    # ---------------------------------------------------------------------- URLs
    async def _check_url(self, claim: Claim, session: SessionContext) -> DetectorResult | None:
        url = parse_url(claim)
        if not url:
            return None  # can't tell what to look up → say nothing (long-chat fix)
        host = urlparse(url).hostname or ""
        if not host or _is_private_host(host):
            return self.result("unverified", 0.3, "Local or private link — not checked.")

        page = await self.cached(session, f"url:{url}", lambda: self._fetch(url))
        ev_url = page.get("final_url") or url

        if page["kind"] == "dns":
            return self.result(
                "contradicted",
                0.9,
                f"The site {host} doesn't exist (domain not found).",
                [Evidence(source="HTTP", url=url, snippet=f"DNS lookup failed for {host}")],
            )
        if page["kind"] == "unreachable":
            return self.result(
                "unverified",
                0.4,
                "Couldn't reach the link to check it.",
                [Evidence(source="HTTP", url=url, snippet=page["detail"])],
            )
        status = page["status"]
        if status in (404, 410):
            return self.result(
                "contradicted",
                0.9,
                f"The link returns {status} Not Found — the page doesn't exist.",
                [Evidence(source="HTTP", url=ev_url, snippet=f"HTTP {status}")],
            )
        if status >= 400:  # 401/403/429/5xx: bot blocking or outages, not proof of fakeness
            return self.result(
                "unverified",
                0.4,
                f"The site refused the check (HTTP {status}).",
                [Evidence(source="HTTP", url=ev_url, snippet=f"HTTP {status}")],
            )

        passage = attributed_text(claim, url)
        if passage and not self._page_contains(page.get("text", ""), passage):
            return self.result(
                "unverified",
                0.6,
                "The page exists but doesn't seem to contain the quoted text.",
                [Evidence(source="HTTP", url=ev_url, snippet=f'Not found on page: "{passage}"')],
            )
        return self.result(
            "supported",
            0.9 if passage else 0.8,
            "The link works" + (" and the page contains the quoted text." if passage else "."),
            [Evidence(source="HTTP", url=ev_url, snippet=f"HTTP {status}")],
        )

    async def _fetch(self, url: str) -> dict[str, Any]:
        """GET the page (5 s timeout, redirects followed). Returns a cacheable dict."""
        try:
            async with self._client() as client:
                resp = await client.get(url)
        except httpx.ConnectError as exc:
            # httpx reports DNS failures as ConnectError; the message names the resolver error.
            msg = str(exc).lower()
            if any(
                s in msg for s in ("name or service", "nodename", "getaddrinfo", "name resolution")
            ):
                return {"kind": "dns", "detail": str(exc)}
            return {"kind": "unreachable", "detail": snippet(str(exc) or "connection failed", 120)}
        except httpx.HTTPError as exc:  # timeouts, TLS errors, too many redirects …
            return {"kind": "unreachable", "detail": type(exc).__name__}
        ctype = resp.headers.get("content-type", "")
        text = (
            resp.text[:500_000] if ("text" in ctype or "html" in ctype or "json" in ctype) else ""
        )
        return {
            "kind": "http",
            "status": resp.status_code,
            "final_url": str(resp.url),
            "text": text,
        }

    @staticmethod
    def _page_contains(page_text: str, passage: str) -> bool:
        """Fuzzy containment: exact after normalizing, else ≥ 85 % of the passage in one run."""
        page = normalize_text(re.sub(r"<[^>]+>", " ", page_text))  # crude tag strip is enough
        want = normalize_text(passage)
        if not want or want in page:
            return bool(want)
        from difflib import SequenceMatcher

        m = SequenceMatcher(None, page, want, autojunk=False).find_longest_match(
            0, len(page), 0, len(want)
        )
        return m.size >= 0.85 * len(want)

    # ---------------------------------------------------------------------- packages
    async def _check_package(self, claim: Claim, session: SessionContext) -> DetectorResult | None:
        names, ecosystem = parse_packages(claim)
        if not names:
            return None  # can't tell what to look up → say nothing (long-chat fix)
        registry = "npm" if ecosystem == "npm" else "PyPI"
        found: list[str] = []
        missing: list[str] = []
        for name in names[:5]:
            exists = await package_exists(name, ecosystem, session, self._client, self.name)
            (found if exists else missing).append(name)
        page = (
            "https://www.npmjs.com/package/{}"
            if ecosystem == "npm"
            else "https://pypi.org/project/{}/"
        )
        if missing:
            return self.result(
                "contradicted",
                0.95,
                f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} not on {registry}.",
                [
                    Evidence(source=registry, url=page.format(n), snippet=f"No package named '{n}'")
                    for n in missing
                ],
            )
        return self.result(
            "supported",
            0.95,
            f"Found on {registry}.",
            [Evidence(source=registry, url=page.format(n), snippet=f"'{n}' exists") for n in found],
        )


async def package_exists(
    name: str,
    ecosystem: str,
    session: SessionContext,
    client_factory=http_client,
    namespace: str = "reference_auditor",
) -> bool:
    """True/False from PyPI or npm. Raises on network errors (caller's check() handles it).

    Shared with the Code API Checker (FR-C7: "not installed → check PyPI existence only").
    Cached in session.cache under the given namespace.
    """
    key = f"{namespace}:pkg:{ecosystem}:{name.lower()}"
    if key in session.cache:
        return session.cache[key]
    if ecosystem == "npm":
        url = NPM_URL.format(name=urlquote(name, safe="@"))  # @scope/pkg → @scope%2Fpkg
    else:
        url = PYPI_URL.format(name=name)
    async with client_factory() as client:
        resp = await client.get(url)
    if resp.status_code == 404:
        exists = False
    else:
        resp.raise_for_status()
        exists = True
    session.cache[key] = exists
    return exists


detector = ReferenceAuditor()
