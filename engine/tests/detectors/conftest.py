"""Role C test settings. Unit tests never call the real Claim Gate model — even when your shell
has ANTHROPIC_API_KEY loaded — so they stay offline, fast and deterministic. Tests that exercise
the gate inject a scripted `gate_judge` instead."""
import pytest


@pytest.fixture(autouse=True)
def _no_live_claim_gate(monkeypatch):
    monkeypatch.setenv("REIGNS_CLAIM_GATE", "0")
