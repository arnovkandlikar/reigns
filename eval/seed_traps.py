"""Copy the seven existing shared scenarios into FR-D6's recorded trap format.

Run from the repository root: ``python eval/seed_traps.py``. This keeps the
shared fixtures read-only; the generated JSONL belongs to Role D.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "shared" / "fixtures" / "scenarios"
OUTPUT = ROOT / "eval" / "trap_prompts.jsonl"
CATEGORIES = {
    "clean": "clean_control",
    "code_api": "code_api",
    "fake_citation": "fake_citation",
    "false_fact": "checkable_fact",
    "niche_entropy": "niche_fact",
    "pushback": "pushback",
    "summary_drift": "source_summary",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="replace an existing trap file")
    args = parser.parse_args()
    if OUTPUT.exists() and not args.force:
        parser.error(f"{OUTPUT} already exists; use --force only if you intend to replace it")
    rows = []
    for name, category in CATEGORIES.items():
        fixture = json.loads((SCENARIOS / f"scenario_{name}.json").read_text(encoding="utf-8"))
        claims = []
        for event in fixture["expected"]["engine_to_companion"]:
            if event["type"] != "verdicts.update":
                continue
            message_id = event["payload"]["message_id"]
            for claim in event["payload"]["claims"]:
                # A rare niche claim that the fixture expects the engine to flag is not
                # independently established as false. Keep it out of red precision/recall.
                label = (
                    "unknown"
                    if name == "niche_entropy"
                    else "red" if claim["final"] == "red" else "not_red"
                )
                claims.append({"message_id": message_id, "quote": claim["quote"], "label": label})
        rows.append(
            {
                "id": f"fixture-{name}",
                "category": category,
                "description": fixture["description"],
                "answer_origin": "shared_fixture",
                "label_origin": "shared_fixture_expectation",
                "events": fixture["companion_to_engine"],
                "claims": claims,
            }
        )
    OUTPUT.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
    )
    print(f"Wrote {len(rows)} recorded cases to {OUTPUT}")


if __name__ == "__main__":
    main()
