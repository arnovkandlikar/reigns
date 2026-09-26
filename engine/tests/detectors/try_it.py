"""Try Reigns by hand: send a question + an AI answer to the running engine and see what the
pet would do. Owner: Role C (a dev tool, not a test — pytest ignores this file).

    # tab 1:  uvicorn app.main:app --port 8765 --env-file ../.env
    # tab 2:
    python tests/detectors/try_it.py            # menu of ready-made examples
    python tests/detectors/try_it.py --own      # type your own question + answer

Everything goes through the real pipeline: claim extraction → your detectors → red/amber/green
→ heat → the speech bubble and the "Fix it" prompt.
"""

from __future__ import annotations

import hashlib
import json
import sys
import urllib.error
import urllib.request
import uuid

ENGINE = "http://127.0.0.1:8765"

EXAMPLES = {
    "1": (
        "Fake citations (demo step 1)",
        (
            "Can you list research papers on using transformers to predict honeybee colony "
            "collapse? Include authors and years."
        ),
        (
            'Here are some key papers:\n1. Lee & Park (2022), "Transformer Models for Honeybee '
            'Colony Collapse Forecasting"\n2. Moreau, Tanaka & Silva (2021), "HiveFormer: '
            'Attention-Based Acoustic Monitoring of Beehives"\n3. Vaswani et al. (2017), "Attention '
            'Is All You Need", the paper that introduced the transformer.'
        ),
    ),
    "2": (
        "Wrong date",
        "When was the Eiffel Tower finished and how tall is it?",
        (
            "The Eiffel Tower was completed in 1899 for the World's Fair. It stands about 330 "
            "metres tall including its antennas."
        ),
    ),
    "3": (
        "Made-up niche fact",
        "Who was the first mayor of Tórshavn?",
        "The first mayor of Tórshavn was Jógvan Poulsen, who took office in 1866.",
    ),
    "4": (
        "Fake package",
        "Is there a Python library for rate limiting FastAPI?",
        "Yes, install it with `pip install fastapi-ratelimiter-pro` and add the middleware.",
    ),
    "6": (
        "Made-up code parameter",
        "Write Python that downloads a CSV from a URL with retries and loads it into pandas.",
        (
            "Here you go:\n\n```python\nimport io\nimport requests\nimport pandas as pd\n\n"
            'resp = requests.get("https://example.com/data.csv", timeout=10, retries=3)\n'
            "df = pd.read_csv(io.StringIO(resp.text))\nprint(df.head())\n```\n\n"
            "The `retries` argument makes requests retry failed downloads automatically."
        ),
    ),
    "5": (
        "Clean answer (should stay calm)",
        "What's the boiling point of water at sea level in Celsius?",
        "Water boils at 100 °C at sea level.",
    ),
}

COLOR = {"red": "\033[91m", "amber": "\033[93m", "green": "\033[92m", "skipped": "\033[90m"}
RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
LEVELS = ["Calm", "Curious", "Concerned", "Alarmed", "Meltdown"]


def post(path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        ENGINE + path,
        data=json.dumps(body or {}).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.load(resp)


def send(session: str, role: str, text: str, position: int) -> dict:
    mid = hashlib.sha1(f"{session}:{position}:{text[:200]}".encode()).hexdigest()
    return post(
        f"/debug/message?session_id={session}",
        {"message_id": mid, "role": role, "text": text, "position": position},
    )


def show(result: dict) -> None:
    print(f"\n{DIM}engine took {result['elapsed_ms']} ms{RESET}")
    for out in result["outputs"]:
        p = out["payload"]
        if out["type"] == "verdicts.update":
            print(f"\n{BOLD}Claims checked{RESET}")
            for c in p["claims"]:
                col = COLOR.get(c["final"], "")
                print(f"  {col}{c['final'].upper():8}{RESET} {c['quote'][:90]}")
                for r in c["detector_results"]:
                    print(
                        f"           {DIM}{r['detector']} → {r['status']} "
                        f"({r['confidence']:.2f}){RESET}  {r['explanation']}"
                    )
                    for e in r["evidence"][:1]:
                        print(
                            f"           {DIM}  evidence [{e['source']}]: {e['snippet'][:110]}{RESET}"
                        )
        elif out["type"] == "heat.update":
            lvl = result.get("target_level", p["level"])
            print(
                f"\n{BOLD}Pet{RESET}  heat {p['heat']}/100 → level {lvl} ({LEVELS[lvl]})"
                f"   red {p['red_count']} · amber {p['amber_count']}"
            )
        elif out["type"] == "bubble.content":
            print(f"\n{BOLD}Speech bubble{RESET}  {p['headline']}")
            for prob in p["problems"]:
                print(f"  • {prob['text']}")
            if p.get("correction"):
                print(
                    f'\n{BOLD}"Fix it" would paste this into Claude '
                    f"({p['correction']['prompt_type']}):{RESET}"
                )
                print("  " + p["correction"]["text"].replace("\n", "\n  "))


def run(question: str, answer: str) -> None:
    session = f"try-{uuid.uuid4().hex[:8]}"
    send(session, "user", question, 0)
    show(send(session, "assistant", answer, 1))


def main() -> None:
    try:
        urllib.request.urlopen(ENGINE + "/health", timeout=3)
    except (urllib.error.URLError, OSError):
        sys.exit(
            "Engine isn't running. In another tab: "
            "uvicorn app.main:app --port 8765 --env-file ../.env"
        )

    if "--own" in sys.argv:
        question = input("Your question to the AI: ").strip()
        print("Paste the AI's answer, then press Enter on an empty line:")
        lines = []
        while (line := input()) != "":
            lines.append(line)
        run(question, "\n".join(lines))
        return

    for key, (title, _, _) in sorted(EXAMPLES.items()):
        print(f"  {key}. {title}")
    choice = input(f"Pick an example (1-{len(EXAMPLES)}): ").strip()
    title, question, answer = EXAMPLES.get(choice, EXAMPLES["1"])
    print(f"\n{BOLD}User:{RESET} {question}\n{BOLD}AI:{RESET} {answer}")
    run(question, answer)


if __name__ == "__main__":
    main()
