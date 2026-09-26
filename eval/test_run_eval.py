"""Claim matching checks for the FR-D6 evaluation runner."""

from pathlib import Path

from eval.run_eval import ledger_normalized, match_claims, write_report


def claim(quote: str, normalized: str, final: str) -> dict:
    return {
        "quote": quote,
        "normalized": normalized,
        "final": final,
        "detector_results": [{"detector": "claim_verifier", "status": "contradicted"}],
    }


def test_false_fact_split_claims_match_right_labels() -> None:
    predicted = [
        claim(
            "The Eiffel Tower was completed in 1899",
            "Eiffel Tower completion year: 1899",
            "red",
        ),
        claim(
            "It stands 330 metres tall", "Eiffel Tower height is 330 metres", "green"
        ),
    ]
    truth = [
        {
            "quote": "The Eiffel Tower was completed in 1899 for the World's Fair.",
            "label": "red",
        },
        {
            "quote": "It stands about 330 metres tall including its antennas.",
            "label": "not_red",
        },
    ]

    counts, rows = match_claims(predicted, truth)

    assert counts["tp"] == 1
    assert counts["tn"] == 1
    assert counts.get("fp", 0) == 0
    assert counts.get("fn", 0) == 0
    assert rows[0]["label_truth"] == "red"
    assert rows[1]["label_truth"] == "not_red"


def test_number_gate_and_unlabelled_red_do_not_create_false_positive() -> None:
    predicted = [
        claim("The tower was completed in 1889", "completion year 1889", "red")
    ]
    truth = [{"quote": "The tower was completed in 1899", "label": "red"}]

    counts, rows = match_claims(predicted, truth)

    assert counts["unlabelled"] == 1
    assert counts["unlabelled_red"] == 1
    assert counts["fn"] == 1
    assert counts.get("fp", 0) == 0
    assert rows[0]["label_truth"] == "unlabelled"
    assert rows[1]["label_truth"] == "red (unmatched)"


def test_numbered_claim_does_not_match_numberless_label() -> None:
    predicted = [claim("The tower is 330 metres tall", "", "red")]
    truth = [{"quote": "The tower is tall", "label": "not_red"}]

    counts, _ = match_claims(predicted, truth)

    assert counts["unlabelled"] == 1
    assert counts.get("fp", 0) == 0


def test_normalized_text_can_rescue_reworded_quote() -> None:
    predicted = [
        claim("Construction ended that year", "Eiffel Tower finished 1899", "red")
    ]
    truth = [{"quote": "The Eiffel Tower was completed in 1899", "label": "red"}]

    counts, _ = match_claims(predicted, truth)

    assert counts["tp"] == 1


def test_greedy_matching_uses_each_label_once() -> None:
    predicted = [
        claim("The Eiffel Tower was completed in 1899", "", "red"),
        claim("Eiffel Tower completed 1899", "", "red"),
    ]
    truth = [{"quote": "The Eiffel Tower was completed in 1899", "label": "red"}]

    counts, rows = match_claims(predicted, truth)

    assert counts["tp"] == 1
    assert counts["unlabelled"] == 1
    assert rows[1]["matched_label"] == "unlabelled"


def test_different_names_do_not_match_even_with_shared_words() -> None:
    predicted = [claim("Paris is the capital city", "", "red")]
    truth = [{"quote": "London is the capital city", "label": "red"}]

    counts, _ = match_claims(predicted, truth)

    assert counts["unlabelled"] == 1
    assert counts["fn"] == 1


def test_ledger_normalized_reads_extracted_claim(tmp_path: Path) -> None:
    import sqlite3

    ledger = tmp_path / "reigns.db"
    with sqlite3.connect(ledger) as db:
        db.execute(
            "CREATE TABLE claims (claim_id TEXT, session_id TEXT, message_id TEXT, normalized TEXT)"
        )
        db.execute(
            "INSERT INTO claims VALUES (?, ?, ?, ?)",
            ("claim-1", "session-1", "message-1", "Eiffel Tower finished 1899"),
        )

    assert ledger_normalized(ledger, "session-1", "message-1") == {
        "claim-1": "Eiffel Tower finished 1899"
    }


def test_report_shows_per_case_matches_and_unlabelled_count(tmp_path: Path) -> None:
    output = tmp_path / "results.md"
    row = {
        "id": "false_fact",
        "category": "checkable_fact",
        "counts": {"tp": 1, "tn": 1, "unlabelled": 1},
        "latencies_ms": [100],
        "claim_rows": [
            {
                "engine_quote": "Eiffel Tower completed in 1899",
                "final": "red",
                "detectors": "claim_verifier:contradicted",
                "matched_label": "Eiffel Tower was completed in 1899",
                "label_truth": "red",
            }
        ],
    }
    write_report(
        output,
        [
            {
                "id": "false_fact",
                "category": "checkable_fact",
                "answer_origin": "shared_fixture",
            }
        ],
        [row],
        [],
        "http://127.0.0.1:8765",
        {"detectors": ["claim_verifier"]},
    )

    report = output.read_text(encoding="utf-8")
    assert "Unlabelled engine claims: 1" in report
    assert (
        "| Engine quote | Final | Detectors | Matched label | Label truth |" in report
    )
    assert "Eiffel Tower completed in 1899" in report
