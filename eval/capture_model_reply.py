"""Record an unedited Claude answer for later independent FR-D6 labeling.

Run from the repository root:
    python eval/capture_model_reply.py --id citation-02 --prompt "Your question"

Drafts are kept out of git until their claims have been checked and copied into
trap_prompts.jsonl. This script never assigns labels or calls the evaluation runner.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "engine"))

from app.llm import complete_text, model_name


async def capture(prompt: str) -> str:
    return await complete_text(
        "Answer the user's question directly.", prompt, max_tokens=2048, model=model_name()
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", required=True, help="Case ID, for example citation-02")
    parser.add_argument("--prompt", required=True, help="Exact user prompt")
    parser.add_argument("--output-dir", type=Path, default=ROOT / ".capture_drafts")
    args = parser.parse_args()
    if not args.id or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for char in args.id):
        parser.error("--id must contain only lowercase letters, digits, hyphens, and underscores")

    output = args.output_dir / f"{args.id}.json"
    if output.exists():
        parser.error(f"capture already exists: {output}")
    load_dotenv(ROOT.parent / ".env")
    reply = asyncio.run(capture(args.prompt))
    if not reply.strip():
        raise RuntimeError("Claude returned an empty reply")
    record = {
        "id": args.id,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "model": model_name(),
        "system": "Answer the user's question directly.",
        "prompt": args.prompt,
        "reply": reply,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Saved unlabelled model reply to {output} ({len(reply)} characters)")


if __name__ == "__main__":
    main()
