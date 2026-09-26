"""G1 latency: reference checks start before LLM extraction finishes (fast path)."""
import asyncio
import time

from app import extraction, plugins
from app.extraction import fast_reference_claims
from app.ledger import Ledger
from app.models import ChatMessage, DetectorResult, Evidence, MessageNew, SessionContext
from app.session import Session


def ctx_with(text):
    s = SessionContext(session_id="s")
    s.messages.append(ChatMessage(message_id="u", role="user", text="papers?", position=0))
    s.messages.append(ChatMessage(message_id="a", role="assistant", text=text, position=1))
    return s


def reply(load_scenario):
    return [m for m in load_scenario("fake_citation")["companion_to_engine"]
            if m["type"] == "message.new" and m["payload"]["role"] == "assistant"][0]["payload"]


def test_fast_path_finds_the_four_papers(load_scenario):
    text = reply(load_scenario)["text"]
    claims = fast_reference_claims(ctx_with(text), "a", text)
    assert [c.type for c in claims] == ["paper"] * 4
    assert all(c.quote in text and c.risk == "high" for c in claims)
    assert claims[0].quote.startswith("Lee & Park (2022)")
    assert "Transformer Models for Honeybee" in claims[0].normalized


def test_fast_path_is_conservative():
    text = ("The company was founded (2019) in Paris. Revenue grew 20% in 2021. "
            "See https://example.com/report and run pip install requests.")
    claims = fast_reference_claims(ctx_with(text), "a", text)
    assert sorted(c.type for c in claims) == ["package", "url"]  # no fake 'paper' from (2019)


def test_llm_duplicates_of_fast_refs_are_dropped(load_scenario):
    text = reply(load_scenario)["text"]
    fast = fast_reference_claims(ctx_with(text), "a", text)
    dup = fast[0].model_copy(update={"claim_id": "x", "quote": "Lee & Park (2022)"})
    assert extraction.overlaps(dup, fast[0])
    other = fast[0].model_copy(update={"claim_id": "y", "quote": "Bees are insects."})
    assert not extraction.overlaps(other, fast[0])


class SlowAuditor:
    name = "reference_auditor"

    async def check(self, claim, session):
        await asyncio.sleep(0.4)
        return DetectorResult(detector="reference_auditor", status="contradicted", confidence=0.9,
                              evidence=[Evidence(source="Crossref", snippet="none")],
                              explanation="No such paper.")


async def test_reference_checks_overlap_llm_extraction(monkeypatch, load_scenario, tmp_path):
    p = reply(load_scenario)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")

    async def slow_llm(*a, **k):  # LLM extraction takes 0.4 s and re-finds a paper
        await asyncio.sleep(0.4)
        return [{"quote": "Lee & Park (2022)", "normalized": "dup", "type": "paper",
                 "risk": "high"}]

    monkeypatch.setattr(extraction, "complete_json", slow_llm)
    monkeypatch.setattr(plugins, "get_detector",
                        lambda n: SlowAuditor() if n == "reference_auditor" else None)
    ledger = Ledger(str(tmp_path / "t.db"))
    await ledger.open()
    s = Session("s", ledger)
    s.ctx.messages.append(ChatMessage(message_id="u", role="user", text="papers?", position=0))
    t0 = time.perf_counter()
    out = await s.on_message(MessageNew(**p))
    elapsed = time.perf_counter() - t0
    await ledger.close()
    claims = out[0].payload["claims"]
    assert len(claims) == 4  # duplicate from the LLM was dropped
    assert elapsed < 0.7, f"checks did not overlap extraction ({elapsed:.2f}s)"
