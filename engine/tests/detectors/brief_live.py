"""Session Brief, live check with the real model. Owner: Role C (dev tool; pytest ignores it).

No engine needed. It does two things:

1. BRIEF: replays a long chat into a session exactly the way the engine would (a background
   update every few turns), then prints the brief and the text the pet would paste into
   Claude's message box.
2. LONG-RANGE REFERENCE: a chat where the database is chosen in turn 1, followed by 6
   unrelated turns, then "what's the max size of a value IT can store?". It runs the Claim
   Gate on the answer twice, WITHOUT and WITH the brief, and shows whether the gate could
   tell what "it" is.

    cd engine && source .venv/bin/activate
    set -a; source ../.env; set +a
    python tests/detectors/brief_live.py                  # scraper chat + reference test
    python tests/detectors/brief_live.py --chat japan     # held-out chat
    python tests/detectors/brief_live.py --ref-only       # just the reference test
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # engine/
sys.path.insert(0, str(Path(__file__).resolve().parent))  # long_chat.py

from app.detectors import claim_gate, session_brief  # noqa: E402
from app.models import ChatMessage, Claim, SessionContext  # noqa: E402
from long_chat import CHATS  # noqa: E402

BOLD, DIM, GREEN, RED, RESET = "\033[1m", "\033[2m", "\033[92m", "\033[91m", "\033[0m"

REFERENCE_CHAT = [
    (
        "I'm building a scraper for my weather app on Python 3.8. I've decided the results "
        "go into SQLite, one row per city per hour.",
        "Sounds good. SQLite is a solid choice for a single-machine scraper.",
    ),
    ("Unrelated, but how do I undo my last git commit?", "Run git reset --soft HEAD~1."),
    ("What does JSON stand for?", "JavaScript Object Notation."),
    ("How do I read an environment variable in Python?", "Use os.environ.get('NAME')."),
    ("What's a good regex for a US zip code?", r"Use ^\d{5}(-\d{4})?$ ."),
    ("How do I pretty-print a dict?", "Use pprint.pprint(d) or json.dumps(d, indent=2)."),
    ("How do I time a function?", "Wrap it with time.perf_counter() before and after."),
]
REF_QUESTION = "Back to storage: what's the max size of a single value it can store?"
# No giveaway words ("BLOB", "PRAGMA"): only the context says which database "it" is.
REF_ANSWER = "By default a single value can be up to 1,000,000,000 bytes."


def build(turns: list[tuple[str, str]], sid: str) -> SessionContext:
    s = SessionContext(session_id=sid)
    for user, reply in turns:
        n = len(s.messages)
        s.messages.append(ChatMessage(message_id=f"u{n}", role="user", position=n, text=user))
        s.messages.append(
            ChatMessage(message_id=f"a{n + 1}", role="assistant", position=n + 1, text=reply)
        )
    return s


async def replay(turns: list[tuple[str, str]], sid: str) -> SessionContext:
    """Add turns one by one and let the brief update in the background, like the engine."""
    s = SessionContext(session_id=sid)
    updates = 0
    for user, reply in turns:
        n = len(s.messages)
        s.messages.append(ChatMessage(message_id=f"u{n}", role="user", position=n, text=user))
        s.messages.append(
            ChatMessage(message_id=f"a{n + 1}", role="assistant", position=n + 1, text=reply)
        )
        task = session_brief.schedule(s)
        if task is not None:
            updates += 1
            await task
    print(f"{DIM}{len(turns)} turns, {updates} background updates{RESET}")
    return s


async def show_brief(chat: str) -> None:
    print(f"\n{BOLD}=== 1. Brief after the {chat!r} chat ==={RESET}")
    t0 = time.perf_counter()
    s = await replay(CHATS[chat], f"brief-{chat}")
    b = await session_brief.refresh(s)
    print(f"{DIM}took {time.perf_counter() - t0:.1f} s{RESET}\n")
    if b is None:
        print(f"{RED}no brief produced; check ANTHROPIC_API_KEY{RESET}")
        return
    print(session_brief.for_claude(s))
    os.environ.setdefault("REIGNS_BRIEF_OFFER_TURNS", "20")
    o = session_brief.offer(s)
    print(f"\n{BOLD}Pet offer:{RESET} {o['headline'] if o else '(none at this length)'}")


async def reference_test() -> None:
    print(f"\n{BOLD}=== 2. Long-range reference: 'it' = a database chosen 7 turns ago ==={RESET}")
    claim = Claim(
        claim_id="ref",
        message_id="",
        quote=REF_ANSWER,
        normalized=REF_ANSWER,
        type="fact",
        risk="high",
    )
    for with_brief in (False, True):
        s = build(REFERENCE_CHAT + [(REF_QUESTION, REF_ANSWER)], f"ref-{with_brief}")
        c = claim.model_copy(update={"message_id": s.messages[-1].message_id})
        if with_brief:
            await session_brief.refresh(s)
        g = await claim_gate.gate(c, s)
        ok = "sqlite" in g.standalone.lower()  # the text detectors search with
        mark = (
            f"{GREEN}knows it's SQLite{RESET}" if ok else f"{RED}doesn't know what 'it' is{RESET}"
        )
        label = "WITH brief   " if with_brief else "WITHOUT brief"
        print(f"{label}: {mark}")
        print(f"   {DIM}standalone: {g.standalone}{RESET}")
        print(f"   {DIM}kind={g.kind} subject={g.subject!r}{RESET}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat", default="scraper", choices=list(CHATS))
    ap.add_argument("--ref-only", action="store_true", help="only the long-range reference test")
    args = ap.parse_args()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("Load keys first:  set -a; source ../.env; set +a")
    os.environ.pop("REIGNS_SESSION_BRIEF", None)
    os.environ.pop("REIGNS_CLAIM_GATE", None)
    if not args.ref_only:
        await show_brief(args.chat)
    await reference_test()


if __name__ == "__main__":
    asyncio.run(main())
