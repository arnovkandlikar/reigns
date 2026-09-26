"""FR-B1, FR-B2, FR-B8, FR-B9 end to end, with fake detectors (no API keys needed)."""
import pytest
from fastapi.testclient import TestClient

from app import plugins
from app.main import app
from app.models import DetectorResult, Evidence


class FakeReferenceAuditor:
    name = "reference_auditor"

    async def check(self, claim, session):
        if "Vaswani" in claim.quote:
            return DetectorResult(detector="reference_auditor", status="supported",
                                  confidence=0.98, evidence=[], explanation="Found.")
        return DetectorResult(detector="reference_auditor", status="contradicted",
                              confidence=0.95,
                              evidence=[Evidence(source="Crossref", url=None,
                                                 snippet="No matching work found")],
                              explanation="No such paper found.")


class Boom:
    name = "claim_verifier"

    async def check(self, claim, session):
        raise RuntimeError("kaboom")


@pytest.fixture(autouse=True)
def fake_detectors(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(plugins, "_detectors",
                        {"reference_auditor": FakeReferenceAuditor(), "claim_verifier": Boom()})
    monkeypatch.setattr(plugins, "get_detector", lambda n: plugins._detectors.get(n))


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_health(client):
    assert client.get("/health").json() == {"ok": True, "version": "1.0"}


def test_ws_invalid_message_gets_error(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_text('{"type": "message.new", "session_id": "s", "payload": {"role": "user"}}')
        out = ws.receive_json()
        assert out["type"] == "error" and out["payload"]["code"] == "invalid_message"
        ws.send_text("not json")
        assert ws.receive_json()["type"] == "error"


def test_ws_fake_citation_flow(client, load_scenario):
    sc = load_scenario("fake_citation")
    msgs = sc["companion_to_engine"]
    with client.websocket_connect("/ws") as ws:
        ws.send_json(msgs[0])  # session.start
        assert ws.receive_json()["type"] == "heat.update"
        ws.send_json(msgs[1])  # user
        ws.send_json(msgs[2])  # assistant with 3 fake papers + 1 real
        types = []
        while len(types) < 3:
            out = ws.receive_json()
            if out["type"] == "heat.update" and not types:
                continue
            types.append(out)
        assert [o["type"] for o in types] == ["verdicts.update", "heat.update", "bubble.content"]
        finals = sorted(c["final"] for c in types[0]["payload"]["claims"])
        assert finals == ["green", "red", "red", "red"]
        assert types[1]["payload"]["heat"] == 75
        bubble = types[2]["payload"]
        # Bubble level/prompt are Course Correct's call (Role D, FR-D4, #46): lookup absence alone
        # gets a cautious verify nudge. The engine only guarantees a bubble with a correction.
        assert bubble["level"] >= 1 and bubble["problems"] and bubble["correction"]


def test_debug_scenario_and_detector_crash_is_contained(client):
    res = client.post("/debug/scenario/scenario_fake_citation").json()
    assert res["elapsed_ms"] < 8000
    first_verdicts = [o for o in res["outputs"] if o["type"] == "verdicts.update"][0]
    assert sorted(c["final"] for c in first_verdicts["payload"]["claims"]).count("red") == 3
    # second reply is clean + follows an inserted correction → verified fix → recovered
    heats = [o["payload"] for o in res["outputs"] if o["type"] == "heat.update"]
    # FR-D5: the 3 fixed papers are resolved (no longer count) and the fix gives −20 → calm
    assert heats[-1]["recovered"] is True and heats[-1]["heat"] == 0
    assert heats[-1]["red_count"] == 0 and heats[-1]["amber_count"] == 0

    r = client.post("/debug/message?session_id=t1", json={
        "message_id": "m1", "role": "assistant", "position": 0,
        "text": "The Eiffel Tower was completed in 1899 in Paris, France."}).json()
    v = r["outputs"][0]["payload"]["claims"][0]
    assert v["detector_results"][0]["status"] == "error" and v["final"] == "skipped"


def test_debug_message_validation(client):
    assert client.post("/debug/message", json={"role": "assistant"}).status_code == 422


def test_disagree_lowers_heat(client, load_scenario):
    msgs = load_scenario("fake_citation")["companion_to_engine"]
    sid = "dis"
    for m in msgs[1:3]:
        out = client.post(f"/debug/message?session_id={sid}", json=m["payload"]).json()
    red = [c for c in out["outputs"][0]["payload"]["claims"] if c["final"] == "red"][0]
    from app.main import debug_sessions
    from app.models import FeedbackDisagree
    import asyncio
    s = debug_sessions[sid]
    res = asyncio.run(s.on_disagree(FeedbackDisagree(claim_id=red["claim_id"])))
    assert res[0].payload["heat"] == 50
