"""Role C test settings. Unit tests never call the real Claim Gate model — even when your shell
has ANTHROPIC_API_KEY loaded — so they stay offline, fast and deterministic. Same for the Session Brief
and experience memory (Atlas).
Tests that exercise either inject a scripted judge instead."""
import pytest


@pytest.fixture(autouse=True)
def _no_live_claim_gate(monkeypatch):
    monkeypatch.setenv("REIGNS_CLAIM_GATE", "0")
    monkeypatch.setenv("REIGNS_SESSION_BRIEF", "0")
    monkeypatch.setenv("REIGNS_EXPERIENCE", "0")  # no Atlas calls; tests inject a fake db
