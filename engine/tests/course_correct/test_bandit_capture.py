"""FR-L7 controlled captures select the requested prompt variant."""

from __future__ import annotations

from app.course_correct import bandit


async def test_capture_override_selects_variant_before_mongo(monkeypatch) -> None:
    monkeypatch.setenv("WITNESS_PROMPT_VARIANT", "v3")

    def unexpected_database_call():
        raise AssertionError("the controlled capture must not sample Mongo counts")

    monkeypatch.setattr(bandit, "get_db", unexpected_database_call)

    variant, variant_id = await bandit.choose_variant(
        "fabricated_sources", "diagnostic_reset"
    )

    assert variant == "v3"
    assert variant_id == "fabricated_sources:diagnostic_reset:v3"
