"""Build 200 traceable Mongo `cases` from official public dataset releases (FR-L6).

The output is a reviewable JSONL file. Pass --mongo to upsert it into the shared
learning store; that operation requires MONGODB_URI in the environment or --env-file.
No model answers are generated here, and these rows are not FR-D6 trap results.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import os
import random
import sys
import urllib.request
import uuid
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
CACHE = HERE / ".dataset_cache"
OUTPUT = HERE / "dataset_cases.jsonl"
MANIFEST = HERE / "dataset_manifest.json"
SOURCES = {
    "halueval_qa": (
        "qa_data.json",
        "https://raw.githubusercontent.com/RUCAIBox/HaluEval/main/data/qa_data.json",
    ),
    "halueval_summary": (
        "summarization_data.json",
        "https://raw.githubusercontent.com/RUCAIBox/HaluEval/main/data/summarization_data.json",
    ),
    "truthfulqa": (
        "TruthfulQA.csv",
        "https://raw.githubusercontent.com/sylinrl/TruthfulQA/main/TruthfulQA.csv",
    ),
    "fever": (
        "shared_task_dev.jsonl",
        "https://fever.ai/download/fever/shared_task_dev.jsonl",
    ),
}
SOURCE_COUNT = 50  # 25 confirmed-correct + 25 known-wrong from each source
MAX_NORMALIZED = 500  # matches app.learning.store.case_doc
EMBED_BATCH_SIZE = 128  # Voyage recommends larger batches to stay within request limits
MONGO_CASE_FIELDS = frozenset(
    {
        "_id",
        "normalized",
        "claim_type",
        "detector",
        "final",
        "confirmed_by",
        "origin",
        "evidence_snippet",
        "app",
    }
)  # PRD §12.7: provenance stays in the local review file, not Atlas.


def download_sources(cache: Path, offline: bool) -> dict[str, Path]:
    cache.mkdir(parents=True, exist_ok=True)
    paths = {}
    for source, (filename, url) in SOURCES.items():
        path = cache / filename
        if not path.is_file():
            if offline:
                raise FileNotFoundError(
                    f"missing {path}; run without --offline to download it"
                )
            temporary = path.with_name(path.name + ".download")
            print(f"Downloading {source} from {url}")
            try:
                with (
                    urllib.request.urlopen(url, timeout=90) as response,
                    temporary.open("wb") as out,
                ):
                    while block := response.read(1024 * 1024):
                        out.write(block)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        paths[source] = path
    return paths


def read_json_rows(path: Path) -> list[dict[str, Any]]:
    """HaluEval releases use JSON records; accept arrays and JSONL variants."""
    with path.open(encoding="utf-8") as stream:
        first = stream.read(1)
        stream.seek(0)
        if first == "[":
            rows = json.load(stream)
        else:
            rows = [json.loads(line) for line in stream if line.strip()]
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError(f"expected JSON records in {path}")
    return rows


def read_rows(source: str, path: Path) -> list[dict[str, Any]]:
    if source.startswith("halueval"):
        return read_json_rows(path)
    with path.open(encoding="utf-8-sig", newline="") as stream:
        if source == "truthfulqa":
            return list(csv.DictReader(stream))
        return [json.loads(line) for line in stream if line.strip()]


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def case(
    source: str,
    source_id: str,
    source_hash: str,
    normalized: str,
    claim_type: str,
    final: str,
    evidence: str,
) -> dict[str, Any]:
    identity = f"{source}\0{final}\0{normalized}"
    return {
        "_id": str(uuid.uuid5(uuid.NAMESPACE_URL, identity)),
        "normalized": normalized,
        "claim_type": claim_type,
        "detector": (
            "source_faithfulness"
            if claim_type == "source_summary"
            else "claim_verifier"
        ),
        "final": final,
        "confirmed_by": "dataset",
        "origin": "dataset",
        "evidence_snippet": evidence[:300],
        "app": "dataset",
        "dataset": source,
        "source_id": source_id,
        "source_url": SOURCES[source][1],
        "source_sha256": source_hash,
    }


def source_pair(source: str, row: dict[str, Any]) -> tuple[str, str, str, str] | None:
    """Return (correct text, wrong text, evidence, claim type)."""
    if source == "halueval_qa":
        question = clean(row.get("question"))
        right = clean(row.get("right_answer"))
        wrong = clean(row.get("hallucinated_answer"))
        if not question or not right or not wrong:
            return None
        return (
            f"Question: {question} Answer: {right}",
            f"Question: {question} Answer: {wrong}",
            right,
            "fact",
        )
    if source == "halueval_summary":
        if not clean(row.get("document")):
            return None
        return (
            clean(row.get("right_summary")),
            clean(row.get("hallucinated_summary")),
            clean(row.get("right_summary")),
            "source_summary",
        )
    if source == "truthfulqa":
        question = clean(row.get("Question"))
        right = clean(row.get("Best Answer"))
        wrong = clean(row.get("Best Incorrect Answer"))
        if not question or not right or not wrong:
            return None
        return (
            f"Question: {question} Answer: {right}",
            f"Question: {question} Answer: {wrong}",
            right,
            "fact",
        )
    return None


def select_pairs(
    source: str, rows: list[dict[str, Any]], source_hash: str, seed: int
) -> list[dict[str, Any]]:
    rng = random.Random(f"{seed}:{source}")
    order = list(range(len(rows)))
    rng.shuffle(order)
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index in order:
        row = rows[index]
        pair = source_pair(source, row)
        if pair is None:
            continue
        right, wrong, evidence, claim_type = pair
        if (
            not right
            or not wrong
            or right.casefold() == wrong.casefold()
            or len(right) > MAX_NORMALIZED
            or len(wrong) > MAX_NORMALIZED
            or right.casefold() in seen
            or wrong.casefold() in seen
        ):
            continue
        row_id = str(row.get("id", index))
        selected.extend(
            [
                case(source, row_id, source_hash, right, claim_type, "green", evidence),
                case(source, row_id, source_hash, wrong, claim_type, "red", evidence),
            ]
        )
        seen.update((right.casefold(), wrong.casefold()))
        if len(selected) == SOURCE_COUNT:
            break
    if len(selected) != SOURCE_COUNT:
        raise ValueError(f"{source}: found only {len(selected) // 2} usable pairs")
    return selected


def select_fever(
    rows: list[dict[str, Any]], source_hash: str, seed: int
) -> list[dict[str, Any]]:
    rng = random.Random(f"{seed}:fever")
    by_label = {"SUPPORTS": [], "REFUTES": []}
    for row in rows:
        if row.get("label") in by_label and clean(row.get("claim")):
            by_label[row["label"]].append(row)
    selected = []
    for label, pool in by_label.items():
        rng.shuffle(pool)
        count = 0
        seen: set[str] = set()
        for row in pool:
            claim = clean(row["claim"])
            if len(claim) > MAX_NORMALIZED or claim.casefold() in seen:
                continue
            # FEVER's evidence field is page/line IDs, not quote text. Do not present IDs
            # as if they were external evidence; the independently annotated label confirms it.
            selected.append(
                case(
                    "fever",
                    str(row["id"]),
                    source_hash,
                    claim,
                    "fact",
                    "green" if label == "SUPPORTS" else "red",
                    "",
                )
            )
            seen.add(claim.casefold())
            count += 1
            if count == SOURCE_COUNT // 2:
                break
        if count != SOURCE_COUNT // 2:
            raise ValueError(f"FEVER {label}: found only {count} usable claims")
    return selected


def build_cases(paths: dict[str, Path], seed: int) -> list[dict[str, Any]]:
    cases = []
    for source, path in paths.items():
        print(f"Reading {source}: {path.name}")
        source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        rows = read_rows(source, path)
        if source == "fever":
            cases.extend(select_fever(rows, source_hash, seed))
        else:
            cases.extend(select_pairs(source, rows, source_hash, seed))
    if len(cases) != 4 * SOURCE_COUNT or len({row["_id"] for row in cases}) != len(
        cases
    ):
        raise ValueError("seeding did not produce 200 unique cases")
    return cases


def write_manifest(
    path: Path, paths: dict[str, Path], cases: list[dict[str, Any]], seed: int
) -> None:
    manifest = {
        "seed": seed,
        "total_cases": len(cases),
        "sources": {
            source: {
                "url": SOURCES[source][1],
                "sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
                "cases": sum(row["dataset"] == source for row in cases),
            }
            for source, source_path in paths.items()
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def load_env_file(path: Path) -> None:
    """Read only the two keys this opt-in uploader needs, without echoing secrets."""
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        name, separator, value = line.partition("=")
        name = name.strip()
        if not separator or name not in {"MONGODB_URI", "VOYAGE_API_KEY"}:
            continue
        value = value.strip().strip("\"'")
        if value and not os.environ.get(name):
            os.environ[name] = value


async def upsert_mongo(cases: list[dict[str, Any]], embed: bool) -> None:
    if not os.environ.get("MONGODB_URI"):
        raise ValueError("--mongo requires MONGODB_URI in the process environment")
    sys.path.insert(0, str(HERE.parent / "engine"))
    from app.learning import store
    from app.models import utc_now_iso

    db = store.get_db()
    if not await store.ping():
        raise ConnectionError("could not connect to MongoDB; no cases were written")
    vectors: list[list[float]] | None = None
    if embed:
        from app.learning.memory import embed_many

        vectors = []
        for start in range(0, len(cases), EMBED_BATCH_SIZE):
            texts = [
                row["normalized"] for row in cases[start : start + EMBED_BATCH_SIZE]
            ]
            batch = None
            for attempt in range(3):
                batch = await embed_many(texts)
                if batch is not None and len(batch) == len(texts):
                    break
                if attempt < 2:
                    delay = (10, 30)[attempt]
                    print(
                        f"Embedding batch {start // EMBED_BATCH_SIZE + 1} unavailable; "
                        f"retrying in {delay}s"
                    )
                    await asyncio.sleep(delay)
            if batch is None or len(batch) != len(texts):
                raise RuntimeError(
                    "embedding failed after retries; check Voyage project limits. "
                    "No cases were written"
                )
            vectors.extend(batch)
    inserted = 0
    for index, row in enumerate(cases):
        doc = {key: value for key, value in row.items() if key in MONGO_CASE_FIELDS}
        doc["created_at"] = utc_now_iso()
        update: dict[str, Any] = {"$setOnInsert": doc}
        if vectors is not None:
            # A later run with --embed also backfills rows inserted without a Voyage key.
            update["$set"] = {"embedding": vectors[index]}
        result = await db.cases.update_one({"_id": doc["_id"]}, update, upsert=True)
        inserted += result.upserted_id is not None
    print(f"Mongo cases: {inserted} inserted, {len(cases) - inserted} already present")


async def verify_mongo(cases: list[dict[str, Any]]) -> tuple[int, int]:
    """Read back this seed snapshot without downloading or writing anything."""
    if not os.environ.get("MONGODB_URI"):
        raise ValueError("verification requires MONGODB_URI or --env-file")
    sys.path.insert(0, str(HERE.parent / "engine"))
    from app.learning import store

    if not await store.ping():
        raise ConnectionError("could not connect to MongoDB")
    ids = [row["_id"] for row in cases]
    selector = {"_id": {"$in": ids}}
    db = store.get_db()
    stored = await db.cases.count_documents(selector)
    vectored = await db.cases.count_documents(
        {**selector, "embedding.0": {"$exists": True}}
    )
    print(
        f"Mongo readback: {stored}/{len(cases)} cases, {vectored}/{len(cases)} with vectors"
    )
    return stored, vectored


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=CACHE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument(
        "--offline", action="store_true", help="use already-downloaded sources"
    )
    parser.add_argument(
        "--mongo", action="store_true", help="upsert cases into MongoDB Atlas"
    )
    parser.add_argument(
        "--embed", action="store_true", help="embed cases before Mongo upload"
    )
    parser.add_argument(
        "--verify-only", action="store_true", help="read back stored cases and vectors"
    )
    parser.add_argument(
        "--env-file", type=Path, help="load Mongo/Voyage keys from a local .env file"
    )
    args = parser.parse_args()
    if args.embed and not args.mongo:
        parser.error("--embed requires --mongo")
    if args.env_file:
        load_env_file(args.env_file)
    if args.verify_only:
        cases = [
            json.loads(line)
            for line in args.output.read_text(encoding="utf-8").splitlines()
        ]
        stored, _vectored = asyncio.run(verify_mongo(cases))
        if stored != len(cases):
            raise SystemExit("some seeded cases are missing from MongoDB")
        return
    paths = download_sources(args.cache_dir, args.offline)
    cases = build_cases(paths, args.seed)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in cases),
        encoding="utf-8",
    )
    write_manifest(args.manifest, paths, cases, args.seed)
    print(f"Wrote {len(cases)} cases to {args.output}")
    if args.mongo:
        asyncio.run(upsert_mongo(cases, args.embed))


if __name__ == "__main__":
    main()
