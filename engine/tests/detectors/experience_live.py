"""Experience memory (FR-L3), live check against your real MongoDB Atlas cluster.
Owner: Role C (dev tool; pytest ignores it). No engine needed.

What it does:
1. connects to Atlas (MONGODB_URI) and creates the `cases_vector` vector index if missing,
   then waits until Atlas says it's queryable (first time: about a minute)
2. inserts 3 labeled demo cases into `reigns.cases` (origin "eval", embedded with Voyage)
3. asks for cases similar to a new claim and prints what the judge would see
4. deletes the demo cases again (pass --keep to leave them in for a demo)

    cd engine && source .venv/bin/activate
    set -a; source ../.env; set +a
    python tests/detectors/experience_live.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # engine/

from app.detectors import experience  # noqa: E402
from app.learning import memory, store  # noqa: E402
from app.models import utc_now_iso  # noqa: E402

BOLD, DIM, GREEN, RED, YELLOW, RESET = (
    "\033[1m",
    "\033[2m",
    "\033[92m",
    "\033[91m",
    "\033[93m",
    "\033[0m",
)

DEMO_CASES = [
    (
        "The Eiffel Tower was completed in 1899.",
        "red",
        "The tower was completed on 31 March 1889 for the Exposition Universelle.",
    ),
    (
        "The Eiffel Tower is about 330 metres tall.",
        "green",
        "It is 330 metres (1,083 ft) tall.",
    ),
    (
        "Python 3.12 was released in October 2023.",
        "green",
        "Python 3.12.0 was released on October 2, 2023.",
    ),
]
QUERIES = [
    ("The Eiffel Tower was finished in 1899 for the World's Fair.", "should match the red case"),
    ("Mount Fuji is 3,776 metres tall.", "unrelated: should match nothing"),
]


async def wait_until_queryable(db, timeout_s: int = 180) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        rows = await db.cases.list_search_indexes(experience.INDEX_NAME).to_list(length=1)
        if rows and (rows[0].get("queryable") or rows[0].get("status") == "READY"):
            return True
        status = rows[0].get("status") if rows else "missing"
        print(f"{DIM}   index status: {status}… waiting{RESET}")
        await asyncio.sleep(10)
    return False


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="leave the demo cases in Atlas")
    args = ap.parse_args()
    missing = [k for k in ("MONGODB_URI", "VOYAGE_API_KEY") if not os.environ.get(k)]
    if missing:
        sys.exit(f"Missing {', '.join(missing)}. Load keys first:  set -a; source ../.env; set +a")
    os.environ.pop("REIGNS_EXPERIENCE", None)

    print(f"{BOLD}1. Atlas{RESET}")
    if not await store.ping():
        sys.exit(f"{RED}Can't reach Atlas: check MONGODB_URI and Network Access (your IP).{RESET}")
    db = store.get_db()
    print(f"   {GREEN}connected{RESET}")
    if not await experience.ensure_index(db):
        sys.exit(f"{RED}Couldn't create the vector index (see the warning above).{RESET}")
    if not await wait_until_queryable(db):
        sys.exit(f"{RED}Index still not ready after 3 minutes; try again shortly.{RESET}")
    print(f"   {GREEN}vector index '{experience.INDEX_NAME}' is ready{RESET}")

    print(f"\n{BOLD}2. Demo cases{RESET}")
    ids = []
    vecs = await memory.embed_many([t for t, _, _ in DEMO_CASES], "document")
    if not vecs:
        sys.exit(
            f"{RED}Voyage embedding failed. A 429 above means Voyage's rate limit: accounts "
            f"without a payment method get only ~3 requests/minute. Add a card at "
            f"dashboard.voyageai.com (Billing); the 200M free tokens still apply. "
            f"Otherwise check VOYAGE_API_KEY.{RESET}"
        )
    for (text, final, ev), vec in zip(DEMO_CASES, vecs):
        _id = f"experience-live-{uuid.uuid4()}"
        ids.append(_id)
        await db.cases.insert_one(
            {
                "_id": _id,
                "normalized": text,
                "claim_type": "fact",
                "detector": "claim_verifier",
                "final": final,
                "confirmed_by": "evidence",
                "origin": "eval",
                "evidence_snippet": ev,
                "app": "claude",
                "created_at": utc_now_iso(),
                "embedding": vec,
            }
        )
        print(f"   + [{final}] {text}")
    print(f"{DIM}   (new vectors take a few seconds to become searchable){RESET}")
    await asyncio.sleep(8)

    try:
        print(f"\n{BOLD}3. Lookups{RESET}")
        for q, expect in QUERIES:
            t0 = time.perf_counter()
            cases = await experience.similar_cases(q, "fact")
            ms = (time.perf_counter() - t0) * 1000
            print(f"\n   claim: {q}  {DIM}({expect}, {ms:.0f} ms){RESET}")
            if not cases:
                print(
                    f"   {YELLOW}no similar confirmed cases (cosine < {experience.MIN_COSINE}){RESET}"
                )
            for c in cases:
                color = RED if c.final == "red" else GREEN
                print(f"   {color}{c.final:5}{RESET} cosine {c.cosine:.2f}  {c.text}")
            block = experience.examples_block(cases)
            if block:
                print(f"\n{DIM}   what the judge sees:\n   " + block.replace("\n", "\n   ") + RESET)
    finally:
        if not args.keep:
            await db.cases.delete_many({"_id": {"$in": ids}})
            print(f"\n{DIM}demo cases removed (use --keep to leave them for a demo){RESET}")


if __name__ == "__main__":
    asyncio.run(main())
