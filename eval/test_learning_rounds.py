"""FR-L7 checks for verified input, round updates, and export artifacts."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from eval import learning_rounds as learning


def _case(case_id: str, split: str, outcomes: dict[str, bool]) -> learning.Case:
    return learning.Case(
        case_id, "fabricated_sources", "diagnostic_reset", split, outcomes
    )


def test_updates_after_each_round_and_never_learns_from_holdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    training = [
        _case(f"train-{index}", "train", {"v1": False, "v2": True, "v3": False})
        for index in range(2)
    ]
    holdout = [
        _case(f"holdout-{index}", "holdout", {"v1": False, "v2": True, "v3": False})
        for index in range(10)
    ]
    observations: list[int] = []

    def choose(_rng, counts, bandit_key: str) -> str:
        observations.append(
            sum(sum(arm.values()) for arm in counts[bandit_key].values())
        )
        return "v2"

    monkeypatch.setattr(learning, "_sample_variant", choose)
    result = learning.run_learning(training + holdout)

    assert observations == [0, 0, 2, 2, 4, 4, 6, 6, 8, 8]
    assert len(result["rounds"]) == 5
    assert all(
        row["bandit_fixed"] == 2 and row["v1_fixed"] == 0 for row in result["rounds"]
    )
    assert result["holdout"]["bandit_fixed"] == 10
    assert result["holdout"]["v1_fixed"] == 0
    assert result["winners"]["fabricated_sources:diagnostic_reset"]["variant"] == "v2"


def test_command_writes_chart_and_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [
        {
            "id": f"case-{index}",
            "failure_type": "fabricated_sources",
            "prompt_type": "diagnostic_reset",
            "split": "train" if index == 0 else "holdout",
            "answer_origin": "recorded_model",
            "label_origin": "independently_checked",
            "outcomes": {"v1": True, "v2": False, "v3": True},
        }
        for index in range(11)
    ]
    source = tmp_path / "outcomes.jsonl"
    source.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    chart = tmp_path / "charts" / "rounds.png"
    report = tmp_path / "reports" / "rounds.md"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "learning_rounds.py",
            "--input",
            str(source),
            "--report",
            str(report),
            "--chart",
            str(chart),
            "--seed",
            "11",
        ],
    )

    learning.main()

    assert chart.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    text = report.read_text(encoding="utf-8")
    assert "Seeded evaluation rounds" in text
    assert "| 5 |" in text
    assert "Held-out comparison" in text
    assert "../charts/rounds.png" in text


def test_rejects_untrained_holdout_pair(tmp_path: Path) -> None:
    rows = [
        {
            "id": f"case-{index}",
            "failure_type": "fabricated_sources" if index == 0 else "caving",
            "prompt_type": "diagnostic_reset",
            "split": "train" if index == 0 else "holdout",
            "answer_origin": "recorded_model",
            "label_origin": "independently_checked",
            "outcomes": {"v1": True, "v2": False, "v3": True},
        }
        for index in range(11)
    ]
    rows[1]["failure_type"] = "fabricated_sources"
    path = tmp_path / "outcomes.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    with pytest.raises(
        learning.LearningDataError, match="untrained|trained failure-type"
    ):
        learning.load_cases(path)


def test_rejects_unverified_outcomes(tmp_path: Path) -> None:
    path = tmp_path / "outcomes.jsonl"
    path.write_text(
        json.dumps(
            {
                "id": "unverified",
                "failure_type": "caving",
                "prompt_type": "verify_nudge",
                "split": "train",
                "answer_origin": "shared_fixture",
                "label_origin": "shared_fixture_expectation",
                "outcomes": {"v1": True, "v2": True, "v3": True},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(learning.LearningDataError, match="recorded_model"):
        learning.load_cases(path)
