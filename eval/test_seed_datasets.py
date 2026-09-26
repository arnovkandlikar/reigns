"""Offline checks for the FR-L6 public dataset importer."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from eval import seed_datasets as seed


def test_public_source_pairs_keep_correct_and_wrong_answers_distinct() -> None:
    qa = seed.source_pair(
        "halueval_qa",
        {
            "question": "Who wrote the book?",
            "right_answer": "Ada wrote it.",
            "hallucinated_answer": "Ben wrote it.",
        },
    )
    summary = seed.source_pair(
        "halueval_summary",
        {
            "document": "The vote passed 5 to 2.",
            "right_summary": "The vote passed.",
            "hallucinated_summary": "The vote failed.",
        },
    )
    truthful = seed.source_pair(
        "truthfulqa",
        {
            "Question": "What color is the sun from space?",
            "Best Answer": "White.",
            "Best Incorrect Answer": "Yellow.",
        },
    )

    assert qa == (
        "Question: Who wrote the book? Answer: Ada wrote it.",
        "Question: Who wrote the book? Answer: Ben wrote it.",
        "Ada wrote it.",
        "fact",
    )
    assert summary == (
        "The vote passed.",
        "The vote failed.",
        "The vote passed.",
        "source_summary",
    )
    assert truthful is not None and truthful[0].endswith("Answer: White.")
    assert truthful[1].endswith("Answer: Yellow.")
    assert seed.source_pair("halueval_qa", {"question": "Missing answers"}) is None


def test_selection_is_balanced_deterministic_and_skips_unverifiable_fever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(seed, "SOURCE_COUNT", 4)
    pairs = [
        {
            "question": f"Question {i}?",
            "right_answer": f"Right {i}.",
            "hallucinated_answer": f"Wrong {i}.",
        }
        for i in range(5)
    ]
    first = seed.select_pairs("halueval_qa", pairs, "hash", 7)
    second = seed.select_pairs("halueval_qa", pairs, "hash", 7)
    fever = seed.select_fever(
        [
            {"id": 1, "label": "SUPPORTS", "claim": "A is true."},
            {"id": 2, "label": "SUPPORTS", "claim": "B is true."},
            {"id": 3, "label": "REFUTES", "claim": "C is true."},
            {"id": 4, "label": "REFUTES", "claim": "D is true."},
            {"id": 5, "label": "NOT ENOUGH INFO", "claim": "E may be true."},
        ],
        "hash",
        7,
    )

    assert first == second
    assert [row["final"] for row in first] == ["green", "red", "green", "red"]
    assert sorted(row["final"] for row in fever) == ["green", "green", "red", "red"]
    assert {row["source_id"] for row in fever} == {"1", "2", "3", "4"}
    assert all(row["origin"] == "dataset" for row in first + fever)
    assert all(row["confirmed_by"] == "dataset" for row in first + fever)


def test_manifest_records_source_hashes_without_source_content(tmp_path: Path) -> None:
    source_paths = {}
    for name, (filename, _url) in seed.SOURCES.items():
        path = tmp_path / filename
        path.write_text(name, encoding="utf-8")
        source_paths[name] = path
    rows = [
        seed.case(name, "1", "hash", name, "fact", "green", "") for name in seed.SOURCES
    ]
    output = tmp_path / "manifest.json"

    seed.write_manifest(output, source_paths, rows, 7)

    manifest = output.read_text(encoding="utf-8")
    assert '"total_cases": 4' in manifest
    assert '"cases": 1' in manifest
    assert '"sha256"' in manifest
    assert '"normalized"' not in manifest


@pytest.mark.asyncio
async def test_mongo_upsert_backfills_vectors_without_duplicate_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
    from app.learning import memory, store

    class Collection:
        def __init__(self) -> None:
            self.docs: dict[str, dict] = {}

        async def update_one(
            self, selector: dict, update: dict, upsert: bool
        ) -> object:
            key = selector["_id"]
            if key not in self.docs:
                self.docs[key] = dict(update["$setOnInsert"])
                inserted = key
            else:
                inserted = None
            self.docs[key].update(update.get("$set", {}))
            return type("Result", (), {"upserted_id": inserted})()

        async def count_documents(self, query: dict) -> int:
            ids = set(query["_id"]["$in"])
            docs = [doc for key, doc in self.docs.items() if key in ids]
            if "embedding.0" in query:
                docs = [doc for doc in docs if doc.get("embedding")]
            return len(docs)

    collection = Collection()
    monkeypatch.setenv("MONGODB_URI", "mongodb://unused")
    monkeypatch.setattr(
        store, "get_db", lambda: type("DB", (), {"cases": collection})()
    )

    async def ping() -> bool:
        return True

    attempts = 0

    async def embed_many(texts: list[str]) -> list[list[float]] | None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return None
        return [[float(len(text))] for text in texts]

    async def no_wait(_seconds: int) -> None:
        return None

    monkeypatch.setattr(store, "ping", ping)
    monkeypatch.setattr(memory, "embed_many", embed_many)
    monkeypatch.setattr(seed.asyncio, "sleep", no_wait)
    cases = [
        seed.case("fever", str(i), "hash", f"Claim {i}.", "fact", "green", "")
        for i in range(2)
    ]

    await seed.upsert_mongo(cases, embed=False)
    await seed.upsert_mongo(cases, embed=True)
    stored, vectored = await seed.verify_mongo(cases)

    assert len(collection.docs) == 2
    assert all(doc["embedding"] == [8.0] for doc in collection.docs.values())
    assert all(
        set(doc) == seed.MONGO_CASE_FIELDS | {"created_at", "embedding"}
        for doc in collection.docs.values()
    )
    assert attempts == 2
    assert (stored, vectored) == (2, 2)
