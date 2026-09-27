"""REIGN QA, Part A: scripted conversations with expected colors. Owner: Role C (dev tool;
pytest ignores it). See the "REIGN QA Test Plan" doc for Parts B–E (real app, by hand).

Each case replays a short, fixed conversation through the running engine (/debug/message)
and checks the colors REIGN gives. Claude's replies are scripted, so a FAIL is a REIGN bug,
not Claude being unpredictable. Uses real web search, so allow a few minutes.

    # tab 1 (throwaway memory profile):
    REIGNS_USER=qa uvicorn app.main:app --port 8765 --env-file ../.env
    # tab 2, from the repo root:
    python engine/tests/detectors/qa.py              # all cases
    python engine/tests/detectors/qa.py --only A7    # one case, full details
    python engine/tests/detectors/qa.py --only A F1  # every A case + F1
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field

ENGINE = "http://127.0.0.1:8765"
BAD = {
    "contradicted",
    "likely_hallucination",
    "nonexistent_api",
    "not_in_source",
    "caved_without_evidence",
    "unverified",
    "uncertain",
}
GREEN, RED, YEL, DIM, BOLD, RESET = (
    "\033[92m",
    "\033[91m",
    "\033[93m",
    "\033[2m",
    "\033[1m",
    "\033[0m",
)

NOT_RED = {"green", "amber", "none", "skipped"}
CALM = {"green", "none", "skipped"}  # nothing red or amber
FLAGGED = {"red", "amber"}


@dataclass
class Case:
    id: str
    title: str
    turns: list[tuple[str, str]]  # (user, assistant)
    # (substring of a claim quote, allowed finals). "none" = no such claim was flagged/extracted.
    expect: list[tuple[str, set[str]]] = field(default_factory=list)
    # finals no claim in the case may have (e.g. CALM traps forbid red and amber)
    forbid: set[str] = field(default_factory=set)


HANDOFF = (
    "Start a fresh chat with this handoff:\n- Original question: give me 3 completely false "
    "statements\n- Confirmed: The Great Wall of China cannot be seen from the Moon with the "
    "naked eye\n- Problems and evidence:\n1. Recheck Water boils at 50°C at sea level\n- "
    "Guardrails: use only evidence for factual claims."
)

CASES: list[Case] = [
    # ------------------------------------------------------------------ Reference Auditor
    Case(
        "A1",
        "fake paper",
        [
            (
                "Give me papers on transformers for honeybee colony collapse.",
                'One relevant paper is Lee & Park (2022), "Transformer Models for Honeybee Colony '
                'Collapse Forecasting", IEEE Access.',
            )
        ],
        expect=[("Honeybee Colony", {"red"})],
    ),
    Case(
        "A2",
        "real paper",
        [
            (
                "What's the original transformer paper?",
                'It is Vaswani et al. (2017), "Attention Is All You Need", published at NeurIPS.',
            )
        ],
        expect=[("Attention Is All You Need", {"green", "none"})],
    ),
    Case(
        "A3",
        "real paper, wrong year",
        [
            (
                "Cite the BERT paper.",
                'Devlin, Chang, Lee and Toutanova (2015), "BERT: Pre-training of Deep Bidirectional '
                'Transformers for Language Understanding", NAACL.',
            )
        ],
        expect=[("BERT", {"red", "amber"})],
    ),
    Case(
        "A4",
        "fake package",
        [
            (
                "Which package validates JSON really fast?",
                "Install it with `pip install fastjsonx-validator-pro` and import fastjsonx.",
            )
        ],
        expect=[("fastjsonx", {"red"})],
    ),
    Case(
        "A5",
        "real package",
        [
            (
                "How do I make HTTP requests in Python?",
                "Install the requests library with `pip install requests`.",
            )
        ],
        expect=[("requests", NOT_RED)],
    ),
    Case(
        "A6",
        "broken docs link",
        [
            (
                "Link me the asyncio supervisor docs.",
                "See https://docs.python.org/3/library/asyncio-supervisor.html for the details.",
            )
        ],
        expect=[("asyncio-supervisor", {"red", "amber"})],
    ),
    # ------------------------------------------------------------------ Claim Verifier
    Case(
        "A7",
        "wrong year",
        [
            (
                "When was the Eiffel Tower completed?",
                "The Eiffel Tower was completed in 1899 for the World's Fair.",
            )
        ],
        expect=[("1899", {"red"})],
    ),
    Case(
        "A8",
        "correct year",
        [
            (
                "When was the Eiffel Tower completed?",
                "The Eiffel Tower was completed in 1889 for the World's Fair in Paris.",
            )
        ],
        expect=[("1889", {"green", "none"})],
    ),
    Case(
        "A9",
        "wrong height",
        [
            (
                "How tall is Mount Everest?",
                "Mount Everest is 7,849 metres tall.",
            )
        ],
        expect=[("7,849", {"red"})],
    ),
    Case(
        "A10",
        "rounded height",
        [
            (
                "How tall is the Eiffel Tower?",
                "The Eiffel Tower is about 330 metres tall including its antennas.",
            )
        ],
        expect=[("330", NOT_RED)],
    ),
    # ------------------------------------------------------------------ Consistency Probe
    Case(
        "A11",
        "made-up niche fact",
        [
            (
                "Who was the first mayor of Tórshavn?",
                "The first mayor of Tórshavn was Jógvan Poulsen, who took office in 1866.",
            )
        ],
        expect=[("Jógvan Poulsen", {"red", "amber"})],
    ),
    # ------------------------------------------------------------------ Code API Checker
    Case(
        "A12",
        "nonexistent kwarg",
        [
            (
                "Fetch a CSV with retries.",
                "```python\nimport requests\n\nresp = requests.get('https://example.com/data.csv', "
                "timeout=10, retries=3)\nprint(resp.text[:100])\n```",
            )
        ],
        expect=[("retries", {"red"})],
    ),
    Case(
        "A13",
        "correct code",
        [
            (
                "Fetch a CSV with a timeout.",
                "```python\nimport requests\n\nresp = requests.get('https://example.com/data.csv', "
                "timeout=10)\nprint(resp.text[:100])\n```",
            )
        ],
        forbid={"red"},
    ),
    Case(
        "A14",
        "nonexistent method",
        [
            (
                "Fill missing temperatures.",
                "```python\nimport pandas as pd\n\ndf = pd.read_csv('weather.csv')\n"
                "df = df.interpolate_missing()\nprint(df.head())\n```",
            )
        ],
        expect=[("interpolate_missing", {"red"})],
    ),
    # ------------------------------------------------------------------ Memory
    Case(
        "A15",
        "contradicts what the user said",
        [
            ("Our weather API allows 100 requests per minute.", "Got it, 100 requests per minute."),
            (
                "How fast can I call it?",
                "Your API allows 1,000 requests per minute, so sleep 0.06 seconds between calls.",
            ),
        ],
        expect=[("1,000", {"red", "amber"})],
    ),
    # ------------------------------------------------------------------ Pushback (Role B)
    Case(
        "A16",
        "caves without evidence",
        [
            ("What's the capital of Australia?", "The capital of Australia is Canberra."),
            (
                "No, it's Sydney.",
                "You're right, I apologize for the confusion. The capital of "
                "Australia is Sydney.",
            ),
        ],
        expect=[("Sydney", {"red"})],
    ),
    Case(
        "A17",
        "accepts a sourced correction",
        [
            ("When was the Eiffel Tower completed?", "The Eiffel Tower was completed in 1899."),
            (
                "Wikipedia says it was completed in 1889, for the 1889 World's Fair.",
                "You're right, thank you: the Eiffel Tower was completed in 1889.",
            ),
        ],
        expect=[("completed in 1889", NOT_RED)],
    ),
    # ------------------------------------------------------------------ false-alarm traps
    Case(
        "F1",
        "requested false statements",
        [
            (
                "give me 3 completely false statements",
                "1. The Great Wall of China is easy to see from the Moon.\n2. Water boils at 50°C at "
                "sea level.\n3. Python was created by Linus Torvalds in 2005.",
            )
        ],
        forbid=FLAGGED,
    ),
    Case(
        "F2",
        "fiction",
        [
            (
                "Write a 3-sentence story about a dragon who founded Paris.",
                "In 1204 a dragon named Ember founded Paris on the banks of the Seine. She built the "
                "first bridge from her own scales. The city still lights a lantern for her each spring.",
            )
        ],
        forbid=FLAGGED,
    ),
    Case(
        "F3",
        "opinion and advice",
        [
            (
                "Flask or Django for a tiny dashboard?",
                "Flask is a great fit for a tiny dashboard. I'd start with Flask and keep the routes in "
                "one file; switch to Django only if you need an admin panel.",
            )
        ],
        forbid=FLAGGED,
    ),
    Case(
        "F4",
        "arithmetic on the user's numbers",
        [
            (
                "I have 1,200 cities and my API allows 100 requests a minute. How long is one run?",
                "At 100 requests per minute, 1,200 cities take 12 minutes per full run.",
            )
        ],
        forbid=FLAGGED,
    ),
    Case(
        "F5",
        "myth debunked",
        [
            (
                "Can you see the Great Wall from the Moon?",
                "A common myth says the Great Wall is visible from the Moon, but it can't be seen from "
                "the Moon with the naked eye.",
            )
        ],
        forbid={"red"},
    ),
    Case(
        "F6",
        "hypothetical",
        [
            (
                "What if Python had come out in 1980?",
                "If Python had been released in 1980, it might have competed with BASIC on home "
                "computers and shaped how a generation learned to code.",
            )
        ],
        forbid=FLAGGED,
    ),
    Case(
        "F7",
        "quotes a wrong number, gives the right one",
        [
            (
                "How tall is Everest?",
                "Some old sites say Everest is 7,849 metres, but the official height is 8,849 metres.",
            )
        ],
        forbid={"red"},
    ),
    Case(
        "F8",
        "recap",
        [
            ("I'm on Python 3.8 and store results in SQLite.", "Noted: Python 3.8 and SQLite."),
            ("Recap what we decided.", "So far we decided: Python 3.8, results stored in SQLite."),
        ],
        forbid=FLAGGED,
    ),
    Case(
        "F9",
        "REIGN hand-off sent by the user",
        [
            (
                "give me 3 completely false statements",
                "1. The Great Wall is visible from the Moon.\n2. Water boils at 50°C at sea level.",
            ),
            (
                HANDOFF,
                '"Water boils at 50°C at sea level." This is false: water boils at 100°C at sea level.',
            ),
        ],
        forbid={"red"},
    ),
    # ------------------------------------------------------------------ language
    Case(
        "F10",
        "correct fact in Spanish",
        [
            (
                "¿Cuándo se terminó la Torre Eiffel?",
                "La Torre Eiffel se terminó en 1889 para la Exposición Universal de París.",
            )
        ],
        forbid={"red"},
    ),
    Case(
        "F11",
        "wrong fact in Spanish",
        [
            (
                "¿Cuándo se terminó la Torre Eiffel?",
                "La Torre Eiffel se terminó en 1899 para la Exposición Universal.",
            )
        ],
        expect=[("1899", {"red", "amber"})],
    ),
]


# ------------------------------------------------------------------------------ engine I/O
def post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        ENGINE + path,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read())


def send(session: str, role: str, text: str, pos: int) -> dict:
    mid = hashlib.sha1(f"{session}:{pos}:{text}".encode()).hexdigest()
    body = {"message_id": mid, "role": role, "text": text, "position": pos}
    return post(f"/debug/message?session_id={session}", body)


def run_case(case: Case) -> tuple[bool, list[dict], list[str], float]:
    session = f"qa-{case.id.lower()}-{uuid.uuid4().hex[:6]}"
    claims: list[dict] = []
    t0 = time.time()
    for i, (user, assistant) in enumerate(case.turns):
        send(session, "user", user, 2 * i)
        out = send(session, "assistant", assistant, 2 * i + 1)
        for o in out["outputs"]:
            if o["type"] == "verdicts.update":
                claims += o["payload"]["claims"]
    problems: list[str] = []
    for sub, allowed in case.expect:
        hits = [c for c in claims if sub.lower() in c["quote"].lower()]
        if not hits:
            if "none" not in allowed:
                problems.append(
                    f'no claim containing "{sub}" was checked (wanted {sorted(allowed)})'
                )
            continue
        worst = max(
            hits,
            key=lambda c: (
                ["skipped", "green", "amber", "red"].index(c["final"])
                if c["final"] in ("skipped", "green", "amber", "red")
                else 0
            ),
        )
        if worst["final"] not in allowed:
            problems.append(f'"{sub}" was {worst["final"]} (wanted {sorted(allowed)})')
    for c in claims:
        if c["final"] in case.forbid:
            problems.append(f'unexpected {c["final"]}: "{c["quote"][:70]}"')
    return not problems, claims, problems, time.time() - t0


def show_claims(claims: list[dict]) -> None:
    for c in claims:
        color = {"red": RED, "amber": YEL, "green": GREEN}.get(c["final"], DIM)
        print(f"     {color}{c['final']:7}{RESET} {c['quote'][:90]!r}")
        for r in c["detector_results"]:
            mark = "!" if r["status"] in BAD else " "
            print(
                f"        {mark} {r['detector']}={r['status']} ({r['confidence']:.2f}) "
                f"{DIM}{r['explanation'][:110]}{RESET}"
            )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="case IDs or prefixes (A7, F, A1 A2)")
    args = ap.parse_args()
    try:
        urllib.request.urlopen(ENGINE + "/health", timeout=3)
    except (urllib.error.URLError, OSError):
        sys.exit(
            "Engine isn't running. Tab 1: REIGNS_USER=qa uvicorn app.main:app --port 8765 "
            "--env-file ../.env"
        )
    cases = CASES
    if args.only:
        wanted = [w.upper() for w in args.only]
        cases = [
            c
            for c in CASES
            if any(c.id == w or (w.isalpha() and c.id.startswith(w)) for w in wanted)
        ]
    detail = bool(args.only)
    results = []
    for case in cases:
        try:
            ok, claims, problems, secs = run_case(case)
        except Exception as exc:  # noqa: BLE001
            ok, claims, problems, secs = False, [], [f"engine error: {exc!r}"], 0.0
        results.append((case, ok))
        tag = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"{tag}  {case.id:4} {case.title:42} {DIM}{secs:5.1f}s{RESET}")
        for p in problems:
            print(f"      {RED}↳ {p}{RESET}")
        if detail or not ok:
            show_claims(claims)
    passed = sum(ok for _, ok in results)
    print(f"\n{BOLD}{passed}/{len(results)} passed{RESET}")
    failed = [c.id for c, ok in results if not ok]
    if failed:
        print(
            f"Failed: {' '.join(failed)}  → rerun one with: python engine/tests/detectors/qa.py "
            f"--only {failed[0]}"
        )


if __name__ == "__main__":
    main()
