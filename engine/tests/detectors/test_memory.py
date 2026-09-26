"""Tests for the Truth Ledger (app/learning/memory.py). Owner: Role C.

Offline: the extraction LLM is scripted, embeddings are off (no VOYAGE_API_KEY), and every
test gets its own SQLite file.
"""

from __future__ import annotations

import pytest

from app.learning import memory
from app.learning.memory import (
    MemoryStore,
    add_card,
    cards_from_verdicts,
    clean_text,
    extract_user_cards,
    ledger_summary,
    list_cards,
    relevant,
    user_id_for,
)
from app.models import ChatMessage, Claim, ClaimVerdict, DetectorResult, Evidence, SessionContext


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Own database per test; no embeddings, no cloud mirror, default user."""
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("REIGNS_MEMORY_SYNC", raising=False)
    monkeypatch.delenv("REIGNS_USER", raising=False)
    store = MemoryStore(str(tmp_path / "mem.db"))
    memory.set_store(store)
    monkeypatch.setenv("REIGNS_MEMORY_DB", store.path)
    yield store
    memory.set_store(None)


def scripted(cards):
    calls = []

    async def judge(system, user, **kw):
        calls.append((user, kw))
        return {"cards": cards}

    judge.calls = calls  # type: ignore[attr-defined]
    return judge


def session_with(text: str, sid: str = "s") -> tuple[SessionContext, ChatMessage]:
    s = SessionContext(session_id=sid)
    m = ChatMessage(message_id="u1", role="user", text=text, position=0)
    s.messages.append(m)
    return s, m


def verdict(claim_id, final, detector, status, conf=0.95, expl="", evidence=True):
    return ClaimVerdict(
        claim_id=claim_id,
        quote="q",
        type="fact",
        risk="high",
        final=final,
        detector_results=[
            DetectorResult(
                detector=detector,
                status=status,
                confidence=conf,
                explanation=expl or "x",
                evidence=[Evidence(source="Wikipedia", snippet="s")] if evidence else [],
            )
        ],
    )


def claim(cid, normalized, type_="fact"):
    return Claim(
        claim_id=cid,
        message_id="m",
        quote=normalized,
        normalized=normalized,
        type=type_,
        risk="high",
    )


# --------------------------------------------------------------------------- identity
def test_default_user_is_local_and_env_overrides(monkeypatch):
    assert user_id_for(SessionContext(session_id="x")) == "local"
    monkeypatch.setenv("REIGNS_USER", "demo")
    assert user_id_for(SessionContext(session_id="x")) == "demo"


# --------------------------------------------------------------------------- user cards
async def test_user_facts_and_constraints_are_stored():
    s, m = session_with("I'm on pandas 1.5, and our API allows 100 requests per minute.")
    judge = scripted(
        [
            {"kind": "user_fact", "text": "Uses pandas 1.5", "subject": "pandas version"},
            {
                "kind": "constraint",
                "text": "API rate limit is 100 requests per minute",
                "subject": "api rate limit",
            },
        ]
    )
    stored = await memory.on_user_message(s, m, judge=judge)
    assert [c.kind for c in stored] == ["user_fact", "constraint"]
    assert len(await list_cards("local")) == 2


async def test_questions_skip_the_llm_entirely():
    s, m = session_with("Who was the first mayor of Tórshavn?")
    judge = scripted([{"kind": "user_fact", "text": "should never be stored", "subject": "x"}])
    assert await memory.on_user_message(s, m, judge=judge) == []
    assert judge.calls == []  # pre-filter: no first person / constraint words → no cost


async def test_pasted_documents_are_not_memorised():
    s, m = session_with("I pasted this: " + "lorem ipsum dolor sit amet " * 60)
    judge = scripted([{"kind": "user_fact", "text": "doc fact", "subject": "x"}])
    await memory.on_user_message(s, m, judge=judge)
    assert "lorem ipsum dolor sit amet lorem" not in judge.calls[0][0]


ARTICLE = ("The Eiffel Tower is a wrought-iron lattice tower on the Champ de Mars. " * 12).strip()


@pytest.mark.parametrize(
    ("message", "kept", "dropped", "attachments"),
    [
        (
            f"Keep it under 200 words for my class.\n\n{ARTICLE}\n\nWe must cite pages.",
            ["under 200 words", "must cite pages"],
            ["lattice tower"],
            [],
        ),
        (
            "My app must run on Python 3.8. Here's my code:\n```python\nmatch x:\n  case 1: pass\n```",
            ["Python 3.8"],
            ["match x"],
            [],
        ),
        (
            "quarterly_report.pdf\nOur budget is under $500.",
            ["budget is under $500"],
            [],
            ["quarterly_report.pdf"],
        ),
        (
            "> The API allows 5000 requests\nThat is outdated, our limit is 100/min.",
            ["limit is 100/min"],
            ["5000"],
            [],
        ),
    ],
)
def test_split_authored_keeps_only_the_users_own_words(message, kept, dropped, attachments):
    authored, found = memory.split_authored(message)
    assert all(k in authored for k in kept)
    assert not any(d in authored for d in dropped)
    assert found == attachments


def test_long_authored_text_keeps_start_and_end():
    text = "We must use Postgres. " + ("I think about many things. " * 400) + "Deadline is Friday."
    authored, _ = memory.split_authored(text)
    assert "Postgres" in authored and "Deadline is Friday" in authored
    assert len(authored) < len(text)


@pytest.mark.parametrize(
    ("text", "worth"),
    [
        ("The server runs Ubuntu 20.04.", True),  # no I/we/my — used to be skipped
        ("Deadline is Friday.", True),
        ("I'm on pandas 1.5, how do I pivot?", True),
        ("Who was the first mayor of Tórshavn?", False),
        ("ok thanks!", False),
        ("cool, got it", False),
    ],
)
def test_worth_extracting(text, worth):
    assert memory.worth_extracting(text) is worth


async def test_bad_llm_output_is_ignored_safely():
    s, m = session_with("I use Flask on AWS Lambda for my project.")
    judge = scripted(
        [
            {"kind": "verified_fact", "text": "LLM may not invent this kind"},
            "garbage",
            {"kind": "user_fact", "text": "short"},
        ]
    )
    assert await memory.on_user_message(s, m, judge=judge) == []


async def test_extraction_errors_never_raise():
    async def broken(*a, **k):
        raise RuntimeError("model down")

    s, m = session_with("I use Flask on AWS Lambda for my project.")
    assert await memory.on_user_message(s, m, judge=broken) == []


async def test_extraction_uses_fast_model(monkeypatch):
    monkeypatch.setenv("REIGNS_FAST_MODEL", "fast-x")
    judge = scripted([])
    await extract_user_cards("We must not use any external libraries.", judge=judge)
    assert judge.calls[0][1]["model"] == "fast-x"


# --------------------------------------------------------------------------- dedupe / supersede
async def test_same_fact_twice_is_refreshed_not_duplicated():
    _, a1 = await add_card(
        "u", "constraint", "API rate limit is 100 requests per minute", "api rate limit"
    )
    card, a2 = await add_card(
        "u", "constraint", "API rate limit is 100 requests per minute.", "api rate limit"
    )
    assert (a1, a2) == ("added", "refreshed")
    assert len(await list_cards("u")) == 1 and card.uses == 0


async def test_changed_fact_replaces_the_old_one(isolated):
    await add_card("u", "constraint", "API rate limit is 100 requests per minute", "api rate limit")
    new, action = await add_card(
        "u", "constraint", "API rate limit is now 200 requests per minute", "API rate limit"
    )
    assert action == "replaced"
    active = await list_cards("u")
    assert [c.text for c in active] == [new.text]
    everything = await isolated.all("u", include_superseded=True)
    assert len(everything) == 2  # history kept, just not used


async def test_users_are_isolated():
    await add_card("alice", "user_fact", "Uses Rust for the backend", "backend language")
    assert await list_cards("bob") == []


# --------------------------------------------------------------------------- confirmed-only rule
def test_only_evidence_backed_verdicts_become_cards():
    claims = [
        claim("a", "The Eiffel Tower was completed in 1889."),
        claim("b", "The Eiffel Tower was completed in 1899."),
        claim("c", "The first mayor of Tórshavn was Jógvan Poulsen."),
        claim("d", "Summary says revenue grew 12%.", type_="source_summary"),
        claim("e", "Low-confidence support."),
    ]
    verdicts = [
        verdict("a", "green", "claim_verifier", "supported"),
        verdict(
            "b",
            "red",
            "claim_verifier",
            "contradicted",
            expl="Wikipedia says it was completed in 1889, not 1899.",
        ),
        verdict("c", "amber", "claim_verifier", "unverified", evidence=False),  # NOT stored
        verdict("d", "green", "claim_verifier", "supported"),  # pasted-doc fact: NOT stored
        verdict("e", "green", "claim_verifier", "supported", conf=0.6),  # too unsure: NOT stored
    ]
    cards = cards_from_verdicts(claims, verdicts)
    assert [(c["kind"], c["text"]) for c in cards] == [
        ("verified_fact", "The Eiffel Tower was completed in 1889."),
        ("correction", "Wikipedia says it was completed in 1889, not 1899."),
    ]


async def test_on_verdicts_stores_them():
    s = SessionContext(session_id="s")
    stored = await memory.on_verdicts(
        s,
        [claim("a", "Water boils at 100 °C at sea level.")],
        [verdict("a", "green", "claim_verifier", "supported")],
    )
    assert [c.kind for c in stored] == ["verified_fact"]


# --------------------------------------------------------------------------- retrieval
async def test_relevant_finds_the_related_card_and_skips_unrelated():
    await add_card("u", "constraint", "API rate limit is 100 requests per minute", "api rate limit")
    await add_card("u", "user_fact", "Uses pandas 1.5 on Python 3.8", "pandas version")
    await add_card("u", "verified_fact", "The Eiffel Tower was completed in 1889.")
    hits = await relevant("u", "Send 500 requests per minute to the API to finish faster.")
    assert hits and hits[0][0].text.startswith("API rate limit")
    assert await relevant("u", "Photosynthesis happens in chloroplasts.") == []


async def test_relevant_marks_cards_as_used():
    await add_card("u", "user_fact", "Uses pandas 1.5 on Python 3.8", "pandas version")
    await relevant("u", "This needs pandas 1.5 to run")
    assert (await list_cards("u"))[0].uses == 1


async def test_embeddings_are_used_when_available(monkeypatch):
    vecs = {
        "API rate limit is 100 requests per minute.": [1.0, 0.0],
        "Throttle calls to stay safe.": [0.9, 0.1],
    }

    async def fake_embed(text, input_type="document"):
        return vecs.get(text, [0.0, 1.0])

    monkeypatch.setattr(memory, "embed", fake_embed)
    await add_card("u", "constraint", "API rate limit is 100 requests per minute")
    hits = await relevant("u", "Throttle calls to stay safe.")  # no shared words at all
    assert hits and hits[0][1] > 0.9


# --------------------------------------------------------------------------- summary / delete
async def test_ledger_summary_groups_by_kind():
    await add_card("u", "constraint", "API rate limit is 100 requests per minute")
    await add_card("u", "correction", "The paper 'HiveFormer' does not exist.")
    text = await ledger_summary("u")
    assert "Rules you must follow:" in text and "- API rate limit" in text
    assert "Corrections (do not repeat these mistakes):" in text
    assert await ledger_summary("nobody") == ""


async def test_delete_card():
    card, _ = await add_card("u", "user_fact", "Uses Rust for the backend")
    assert await memory.delete_card("u", card.card_id)
    assert await list_cards("u") == []
    assert not await memory.delete_card("other-user", card.card_id)


def test_tokens_keep_versions_whole():
    assert {"pandas", "1.5", "python", "3.8"} <= memory.tokens("Uses pandas 1.5 on Python 3.8.")
    assert "100" in memory.tokens("limit is 100 requests/min.")


def test_clean_text_caps_length_and_ends_sentence():
    assert clean_text("  uses   pandas 1.5 ") == "uses pandas 1.5."
    assert len(clean_text("word " * 100).split()) == memory.MAX_CARD_WORDS


async def test_embed_without_key_is_none():
    assert await memory.embed("anything") is None
