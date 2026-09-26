"""Shared pytest fixtures (Role B owns this file). Roles C/D: use these, don't edit.

    def test_x(load_scenario):
        sc = load_scenario("fake_citation")
        claim = Claim(**sc["expected"]["claims"][0])
"""
import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[2] / "shared" / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def load_scenario():
    def _load(name: str) -> dict:
        name = name if name.startswith("scenario_") else f"scenario_{name}"
        return json.loads((FIXTURES / "scenarios" / f"{name}.json").read_text())

    return _load


@pytest.fixture
def all_scenarios() -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted((FIXTURES / "scenarios").glob("*.json"))]
