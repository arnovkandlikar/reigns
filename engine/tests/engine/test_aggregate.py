"""FR-B5 / §8.4 aggregation."""
from app.aggregate import final_status
from app.models import Claim, DetectorResult, Evidence

E = [Evidence(source="x", url=None, snippet="y")]


def claim(risk="high", type_="fact"):
    return Claim(claim_id="c", message_id="m", quote="q", normalized="q", type=type_, risk=risk)


def r(det, status, conf=0.9, ev=None):
    return DetectorResult(detector=det, status=status, confidence=conf, evidence=ev or [],
                          explanation="e")


def test_low_risk_skipped():
    assert final_status(claim("low"), [r("claim_verifier", "contradicted", ev=E)]) == "skipped"


def test_errors_only_skipped():
    assert final_status(claim(), [r("claim_verifier", "error")]) == "skipped"
    assert final_status(claim(), []) == "skipped"


def test_contradicted_needs_evidence_for_red():
    assert final_status(claim(), [r("claim_verifier", "contradicted", ev=E)]) == "red"
    assert final_status(claim(), [r("claim_verifier", "contradicted")]) == "amber"


def test_likely_hallucination_red_only_without_support():
    lh = r("consistency_probe", "likely_hallucination")
    assert final_status(claim(), [r("claim_verifier", "unverified"), lh]) == "red"
    assert final_status(claim(), [lh]) == "amber"


def test_not_in_source_threshold():
    assert final_status(claim(), [r("source_faithfulness", "not_in_source", 0.8)]) == "red"
    assert final_status(claim(), [r("source_faithfulness", "not_in_source", 0.79)]) == "amber"
    assert final_status(claim(), [r("source_faithfulness", "not_in_source", 0.82)], 0.85) == "amber"


def test_always_red_statuses():
    assert final_status(claim(), [r("pushback", "caved_without_evidence")]) == "red"
    assert final_status(claim(), [r("code_api_checker", "nonexistent_api")]) == "red"


def test_green():
    assert final_status(claim(), [r("claim_verifier", "supported")]) == "green"
    assert final_status(claim(), [r("consistency_probe", "consistent")]) == "green"
    assert final_status(claim(), [r("claim_verifier", "supported"),
                                  r("consistency_probe", "uncertain")]) == "amber"


def test_scattered_answers_without_evidence_need_high_confidence_for_red():
    """docs/requests.md 04:55: no evidence either way + mildly scattered samples → amber."""
    no_ev = r("claim_verifier", "unverified")
    assert final_status(claim(), [no_ev, r("consistency_probe", "likely_hallucination", 0.9)]) == "red"
    assert final_status(claim(), [no_ev, r("consistency_probe", "likely_hallucination", 0.7)]) == "amber"
