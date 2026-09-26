"""Shared pytest fixtures (Role B owns this file). Roles C/D: use these, don't edit.

    def test_x(load_scenario):
        sc = load_scenario("fake_citation")
        claim = Claim(**sc["expected"]["claims"][0])
"""
import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[2] / "shared" / "fixtures"


@pytest.fixture(autouse=True)
def _no_real_cloud_services(monkeypatch):
    """Tests never touch the real MongoDB Atlas / ElevenLabs, even if your shell has the keys
    loaded from .env. Tests that need a database set their own fake (see test_store.py)."""
    from app.learning import store

    for key in ("MONGODB_URI", "ELEVENLABS_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(store, "_client", None)
    monkeypatch.setattr(store, "_db", None)
    monkeypatch.setattr(store, "_loop", None)
    monkeypatch.setattr(store, "_lock", None)
    store._queue.clear()


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
