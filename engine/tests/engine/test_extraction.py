"""FR-B3: hedges / self-talk are never extracted as claims (they would keep heat high)."""

import pytest

from app import extraction
from app.extraction import extract_claims, is_hedge
from app.models import ChatMessage, SessionContext

HEDGES = [
    "I'm not confident those first three papers exist, and I shouldn't have listed them.",
    "I don't know of peer-reviewed work that applies transformers to colony collapse.",
    "You're right to check — I made a mistake.",
    "Sorry, I can't verify that citation.",
    "Good catch, I apologize for the confusion.",
    "I cannot confirm the exact date.",
]
CLAIMS = [
    "The Eiffel Tower was completed in 1889.",
    "The capital of Australia is Sydney, its largest city.",
    "I recommend using pandas 2.2 for this.",  # advice, but not a hedge
    "Vaswani et al. (2017), \"Attention Is All You Need\"",
]


@pytest.mark.parametrize("q", HEDGES)
def test_hedges_detected(q):
    assert is_hedge(q)


@pytest.mark.parametrize("q", CLAIMS)
def test_real_claims_kept(q):
    assert not is_hedge(q)


def session_with(text):
    s = SessionContext(session_id="s")
    s.messages.append(ChatMessage(message_id="u", role="user", text="list papers", position=0))
    s.messages.append(ChatMessage(message_id="a", role="assistant", text=text, position=1))
    return s


async def test_heuristic_skips_hedges_but_keeps_reference(monkeypatch, load_scenario):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fixed = [m for m in load_scenario("fake_citation")["companion_to_engine"]
             if m["type"] == "message.new" and m["payload"]["role"] == "assistant"][-1]
    text = fixed["payload"]["text"]
    claims = await extract_claims(session_with(text), "a", text)
    assert claims, "the real paper should still be extracted"
    assert all(not is_hedge(c.quote) for c in claims)
    assert any("Vaswani" in c.quote for c in claims)


async def test_llm_path_drops_hedges(monkeypatch):
    text = "I'm not confident those papers exist. The Eiffel Tower was completed in 1889."
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")

    async def fake_json(*a, **k):
        return [
            {"quote": "I'm not confident those papers exist.", "normalized": "x", "type": "fact",
             "risk": "high"},
            {"quote": "The Eiffel Tower was completed in 1889.", "normalized": "y",
             "type": "number", "risk": "high"},
        ]

    monkeypatch.setattr(extraction, "complete_json", fake_json)
    claims = await extract_claims(session_with(text), "a", text)
    assert [c.quote for c in claims] == ["The Eiffel Tower was completed in 1889."]


async def test_private_context_claims_are_not_checked(monkeypatch):
    """docs/requests.md 04:55: claims about the user's own repo/project are skipped."""
    text = ("Your repo has 19 new commits and Role C added the Consistency Probe. "
            "The Eiffel Tower was completed in 1889. See Vaswani et al. (2017).")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")

    async def fake_json(*a, **k):
        return [
            {"quote": "Your repo has 19 new commits", "normalized": "a", "type": "number",
             "risk": "high", "scope": "private"},
            {"quote": "Role C added the Consistency Probe", "normalized": "b", "type": "fact",
             "risk": "high", "scope": "private"},
            {"quote": "The Eiffel Tower was completed in 1889.", "normalized": "c",
             "type": "number", "risk": "high", "scope": "public"},
            {"quote": "Vaswani et al. (2017)", "normalized": "d", "type": "paper",
             "risk": "high", "scope": "private"},  # references are always checked
        ]

    monkeypatch.setattr(extraction, "complete_json", fake_json)
    claims = await extract_claims(session_with(text), "a", text)
    assert sorted(c.quote for c in claims) == ["The Eiffel Tower was completed in 1889.",
                                               "Vaswani et al. (2017)"]


def test_code_identifiers_and_describes_code():
    code = "import requests\nr = requests.get(url, timeout=10, retries=3)\ndf = pd.read_csv(x)\n"
    ids = extraction.code_identifiers(code)
    assert {"get", "retries", "timeout", "read_csv"} <= ids
    assert "requests" not in ids  # module names are not identifiers here
    assert extraction.describes_code("The `retries` argument makes requests retry.", ids)
    assert extraction.describes_code("Call pd.read_csv() on the text.", ids)
    assert not extraction.describes_code("Requests was created by Kenneth Reitz in 2011.", ids)


async def test_prose_about_the_code_is_not_double_counted(monkeypatch, load_scenario):
    reply = [m for m in load_scenario("code_api")["companion_to_engine"]
             if m["type"] == "message.new" and m["payload"]["role"] == "assistant"][0]
    text = reply["payload"]["text"]
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")

    async def fake_json(*a, **k):
        return [{"quote": "The `retries` argument makes requests retry failed downloads "
                          "automatically.", "normalized": "x", "type": "fact", "risk": "high"}]

    monkeypatch.setattr(extraction, "complete_json", fake_json)
    claims = await extract_claims(session_with(text), "a", text)
    assert [c.type for c in claims] == ["code_api"]


def _c(quote, ctype="fact"):
    from app.models import Claim
    return Claim(claim_id="x", message_id="m", quote=quote, normalized=quote, type=ctype,
                 risk="high")


def test_fragments_are_dropped_but_short_facts_with_numbers_kept():
    assert extraction.is_fragment(_c("for the World's Fair"))
    assert not extraction.is_fragment(_c("completed in 1899", "number"))
    assert not extraction.is_fragment(_c("The Eiffel Tower is in Paris, France."))
    assert not extraction.is_fragment(_c("Vaswani et al.", "paper"))
    assert not extraction.is_fragment(_c("Sydney is the capital."))  # short but a real claim
    assert extraction.is_fragment(_c("in Paris"))
