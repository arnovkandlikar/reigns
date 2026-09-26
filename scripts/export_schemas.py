#!/usr/bin/env python3
"""Regenerate /shared/schemas/*.json from engine/app/models.py (FR-B2).

models.py is the single source; the JSON Schemas are generated from it so the two can never
drift apart. Run after any §12 change:  python scripts/export_schemas.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))

from app import models as m  # noqa: E402

OUT = ROOT / "shared" / "schemas"

EXTRA = {
    "envelope": m.Envelope,
    "claim": m.Claim,
    "source_doc": m.SourceDoc,
    "detector_result": m.DetectorResult,
    "drift_profile": m.DriftProfile,
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.json"):
        old.unlink()
    written = []
    for type_name, model in {**m.PAYLOAD_MODELS, **EXTRA}.items():
        schema = model.model_json_schema()
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        if type_name in m.PAYLOAD_MODELS:
            schema["description"] = f"Payload of the '{type_name}' WebSocket message (PRD §12)."
        path = OUT / f"{type_name.replace('.', '_')}.json"
        schema["$id"] = f"reigns/{path.name}"
        path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
        written.append(path.name)
    print(f"wrote {len(written)} schemas to {OUT.relative_to(ROOT)}: {', '.join(sorted(written))}")


if __name__ == "__main__":
    main()
