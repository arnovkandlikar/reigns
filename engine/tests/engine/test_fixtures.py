"""Fixtures (§12.6) are valid against the JSON schemas and consistent with §8.4."""
import json
from pathlib import Path

import jsonschema
import pytest

from app.aggregate import final_status
from app.models import PAYLOAD_MODELS, Claim, DetectorResult, Envelope

SCHEMAS = Path(__file__).resolve().parents[3] / "shared" / "schemas"
SCENARIO_NAMES = ["fake_citation", "false_fact", "niche_entropy", "pushback", "summary_drift",
                  "code_api", "clean"]


def schema(type_: str) -> dict:
    return json.loads((SCHEMAS / f"{type_.replace('.', '_')}.json").read_text())


def check_envelope(e: dict) -> None:
    jsonschema.validate(e, schema("envelope"))
    jsonschema.validate(e["payload"], schema(e["type"]))
    PAYLOAD_MODELS[e["type"]].model_validate(e["payload"])


def test_all_seven_scenarios_exist(fixtures_dir):
    found = {p.stem for p in (fixtures_dir / "scenarios").glob("*.json")}
    assert found == {f"scenario_{n}" for n in SCENARIO_NAMES}


def test_every_message_type_has_an_example(fixtures_dir):
    found = {p.stem for p in (fixtures_dir / "messages").glob("*.json")}
    assert found == {t.replace(".", "_") for t in PAYLOAD_MODELS}


@pytest.mark.parametrize("name", SCENARIO_NAMES)
def test_scenario_valid(name, load_scenario):
    sc = load_scenario(name)
    msgs = {}
    for e in sc["companion_to_engine"] + sc["expected"]["engine_to_companion"]:
        check_envelope(e)
        Envelope.model_validate(e)
        if e["type"] == "message.new":
            msgs[e["payload"]["message_id"]] = e["payload"]["text"]
    for c in sc["expected"]["claims"]:
        claim = Claim.model_validate(c)
        assert claim.quote in msgs[claim.message_id]  # FR-B3 exact substring
        results = [DetectorResult.model_validate(r)
                   for r in sc["expected"]["detector_results"][claim.claim_id]]
        verdicts = [v for e in sc["expected"]["engine_to_companion"]
                    if e["type"] == "verdicts.update" for v in e["payload"]["claims"]
                    if v["claim_id"] == claim.claim_id]
        assert verdicts and verdicts[0]["final"] == final_status(claim, results)


def test_message_examples_valid(fixtures_dir):
    for p in (fixtures_dir / "messages").glob("*.json"):
        check_envelope(json.loads(p.read_text()))
