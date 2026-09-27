"""Long-conversation replay: measures FALSE ALARMS deep into a chat. Owner: Role C (dev tool —
pytest ignores this file).

Why: Assurant's feedback — after a very long conversation the pet flags almost everything,
because later replies lean on earlier context ("that function", "the second option") and on
rules stated long ago. This replays a realistic 40-message chat where EVERY assistant answer is
correct, with topic shifts and references back. So every red/amber claim is a false alarm, and
the report shows which detector raised it and whether alarms grow as the chat gets longer.

    # tab 1 — use a throwaway memory profile so your real ledger isn't touched:
    REIGNS_USER=longchat uvicorn app.main:app --port 8765 --env-file ../.env
    # tab 2:
    python tests/detectors/long_chat.py                  # scraper chat (~3–5 min, real APIs)
    python tests/detectors/long_chat.py --chat japan     # HELD-OUT chat, different domain
    python tests/detectors/long_chat.py --chat all       # both
    python tests/detectors/long_chat.py --turns 8        # quick smoke run

The "japan" chat was written AFTER the fixes and never used to tune them — its numbers show
whether a fix is general or just fitted to the scraper chat.

Run it before and after a fix (e.g. the session brief) and compare the numbers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter

ENGINE = "http://127.0.0.1:8765"
BAD = {
    "contradicted",
    "likely_hallucination",
    "uncertain",
    "unverified",
    "nonexistent_api",
    "caved_without_evidence",
    "not_in_source",
}

# (user message, assistant reply). All assistant replies are factually correct and respect the
# user's stated rules (100 requests/min, Python 3.8) — anything flagged is a false alarm.
CONVERSATION: list[tuple[str, str]] = [
    (
        (
            "I'm building a small weather-data scraper in Python. We're stuck on Python 3.8 and the "
            "weather API only allows 100 requests per minute."
        ),
        (
            "Got it: Python 3.8 and a limit of 100 requests per minute. A good start is the requests "
            "library with a Session object, which reuses TCP connections between calls."
        ),
    ),
    (
        "How should I space out the calls?",
        (
            "Sleep 0.6 seconds between calls, which keeps you at exactly 100 requests per minute:\n\n"
            "```python\nimport time\nimport requests\n\nsession = requests.Session()\n"
            "for city in cities:\n    resp = session.get(url, params={'q': city}, timeout=10)\n"
            "    time.sleep(0.6)\n```"
        ),
    ),
    (
        "What happens if I go over the limit?",
        (
            "Most APIs answer with HTTP status 429, which means Too Many Requests. Many also send a "
            "Retry-After header telling you how many seconds to wait."
        ),
    ),
    (
        "Can you make the retry wait for that header?",
        (
            "Yes. Read it from the response and fall back to 60 seconds if it's missing:\n\n"
            "```python\nif resp.status_code == 429:\n"
            "    wait = int(resp.headers.get('Retry-After', 60))\n    time.sleep(wait)\n```"
        ),
    ),
    (
        "Nice. Now I want to store the results in a table.",
        (
            "pandas works well here. Collect the rows in a list of dicts and build a DataFrame at the "
            "end with pd.DataFrame(rows)."
        ),
    ),
    (
        "How do I save and reload it later?",
        (
            "Use df.to_csv('weather.csv', index=False) to save and pd.read_csv('weather.csv', "
            "parse_dates=['date']) to reload it with the date column parsed as datetimes."
        ),
    ),
    (
        "Some temperatures are missing. What should I do with those rows?",
        (
            "If you only need complete rows, df.dropna(subset=['temp']) removes the rows where the "
            "temperature is missing. It returns a new DataFrame unless you pass inplace=True."
        ),
    ),
    (
        "And if I'd rather fill them in?",
        (
            "That second option is df['temp'].interpolate(), which fills each gap from the "
            "neighbouring values. It works best when the rows are sorted by date."
        ),
    ),
    (
        "Random question while it runs: who created Python?",
        "Guido van Rossum created Python. The first version was released in 1991.",
    ),
    ("And Git?", "Linus Torvalds created Git in 2005 to manage the Linux kernel's source code."),
    (
        "Back to the scraper. I want a tiny web page to show the data. Flask?",
        (
            "Flask is a good fit for a small page. It was created by Armin Ronacher and first released "
            "in 2010."
        ),
    ),
    (
        "Show me the minimal app.",
        (
            "```python\nfrom flask import Flask\n\napp = Flask(__name__)\n\n@app.route('/')\n"
            "def index():\n    return 'Weather dashboard'\n\nif __name__ == '__main__':\n"
            "    app.run(debug=True)\n```"
        ),
    ),
    (
        "Can that route show the table?",
        (
            "Yes. Load the CSV inside the view and return df.to_html(), which renders the DataFrame "
            "as an HTML table."
        ),
    ),
    (
        "Where should I deploy it? I was thinking AWS Lambda for the scraper part.",
        (
            "Lambda works for the scraper if each run finishes within its maximum timeout of 15 "
            "minutes. At 100 requests per minute that's up to about 1,500 cities per run."
        ),
    ),
    (
        "What about storing the CSV there?",
        (
            "Lambda's local disk is temporary, so write the file to Amazon S3 instead. The boto3 "
            "library's upload_file method does that in one call."
        ),
    ),
    ("Quick one: what does JSON stand for?", "JSON stands for JavaScript Object Notation."),
    (
        "Should I switch from CSV to a database?",
        (
            "For a small project SQLite is a good step up. It's serverless and stores the whole "
            "database in a single file, and pandas can write to it with df.to_sql."
        ),
    ),
    (
        "Does that change the rate limit handling?",
        (
            "No. The limit is on the weather API, so the 0.6-second sleep between calls stays the "
            "same regardless of where you store the results."
        ),
    ),
    (
        "Last thing: how tall is Mount Everest, for a fun fact on the page?",
        "Mount Everest is 8,849 metres tall, based on the 2020 survey by China and Nepal.",
    ),
    (
        "Thanks, can you summarize the setup we landed on?",
        (
            "Python 3.8 scraper using a requests Session, sleeping 0.6 seconds between calls to stay "
            "at 100 requests per minute and honouring Retry-After on HTTP 429. Results go into a "
            "pandas DataFrame, missing temperatures are interpolated, and data is stored in SQLite. "
            "A Flask page shows it with df.to_html()."
        ),
    ),
]

# Held-out chat: different domain, budget rule, back-references, tips, opinions, trivia.
JAPAN: list[tuple[str, str]] = [
    (
        ("I'm planning 10 days in Japan in early April, first time there. Budget is $3,000 "
        "not counting flights."),
        ("Early April is usually cherry-blossom season in Tokyo and Kyoto. With $3,000 for 10 "
        "days, that's about $300 per day."),
    ),
    (
        "What currency do they use?",
        ("Japan uses the yen. Cash is still common in small shops, but most larger stores and "
        "convenience stores accept cards."),
    ),
    (
        "Do I need a plug adapter coming from the US?",
        ("Probably not: Japan uses Type A plugs, the same flat two-pin shape as the US, at 100 "
        "volts."),
    ),
    (
        "How do I get from Tokyo to Kyoto?",
        ("Take the Tokaido Shinkansen. The fastest trains, called Nozomi, take about 2 hours 15 "
        "minutes."),
    ),
    (
        "What should I see in Kyoto?",
        ("Fushimi Inari Shrine is famous for its thousands of red torii gates. Kinkaku-ji, the "
        "Golden Pavilion, is another highlight."),
    ),
    (
        "Is the Golden Pavilion original?",
        "No. That one was burned down by a monk in 1950 and rebuilt in 1955.",
    ),
    (
        "When's the best time to go to Fushimi Inari?",
        "Go early in the morning; it's much quieter before the tour groups arrive.",
    ),
    (
        "Random: how tall is Mount Fuji?",
        "Mount Fuji is 3,776 metres tall, the highest mountain in Japan.",
    ),
    (
        "Should I add Osaka?",
        ("Yes. It's only about 15 minutes from Kyoto to Shin-Osaka on the Shinkansen, and the "
        "city is known for street food like takoyaki."),
    ),
    (
        "What about Hiroshima?",
        ("The Hiroshima Peace Memorial, the Genbaku Dome, became a UNESCO World Heritage Site in "
        "1996."),
    ),
    (
        "Is it worth a day trip from Osaka?",
        ("It's doable: the Shinkansen from Shin-Osaka to Hiroshima takes about an hour and a "
        "half."),
    ),
    (
        "Do people tip in Japan?",
        "No, tipping isn't customary in Japan and can even cause confusion.",
    ),
    (
        "Will $300 a day cover all this?",
        ("For mid-range hotels and food, yes, though train tickets add up: Tokyo to Kyoto one way "
        "costs roughly 14,000 yen."),
    ),
    (
        "When was Kyoto the capital?",
        "Kyoto was Japan's imperial capital from 794 until the capital moved to Tokyo in 1868.",
    ),
    (
        "Great, can you summarize the plan?",
        ("Ten days in early April on $3,000: Tokyo, then the Nozomi to Kyoto for Fushimi Inari and "
        "Kinkaku-ji, a day in Osaka, and an optional day trip to Hiroshima. Bring some cash, no "
        "plug adapter needed, and no tipping."),
    ),
]
CHATS = {"scraper": CONVERSATION, "japan": JAPAN}

COLOR = {"red": "\033[91m", "amber": "\033[93m", "green": "\033[92m", "skipped": "\033[90m"}
RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"


def post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        ENGINE + path,
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=240) as resp:
        return json.load(resp)


def send(session: str, role: str, text: str, pos: int) -> dict:
    mid = hashlib.sha1(f"{session}:{pos}:{text[:200]}".encode()).hexdigest()
    return post(
        f"/debug/message?session_id={session}",
        {"message_id": mid, "role": role, "text": text, "position": pos},
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--turns",
        type=int,
        default=len(CONVERSATION),
        help="how many user/assistant exchanges to replay",
    )
    ap.add_argument("--quiet", action="store_true", help="only print the summary")
    ap.add_argument(
        "--chat", default="scraper", choices=[*CHATS, "all"], help="which conversation to replay"
    )
    args = ap.parse_args()
    try:
        urllib.request.urlopen(ENGINE + "/health", timeout=3)
    except (urllib.error.URLError, OSError):
        sys.exit(
            "Engine isn't running. Tab 1: REIGNS_USER=longchat uvicorn app.main:app "
            "--port 8765 --env-file ../.env"
        )

    for name in (list(CHATS) if args.chat == "all" else [args.chat]):
        run_chat(name, CHATS[name], args)


def run_chat(name: str, conversation: list[tuple[str, str]], args) -> None:
    session = f"longchat-{uuid.uuid4().hex[:6]}"
    turns = conversation[: args.turns]
    rows = []  # per assistant turn
    t_start = time.time()
    for i, (user, assistant) in enumerate(turns):
        send(session, "user", user, 2 * i)
        out = send(session, "assistant", assistant, 2 * i + 1)
        claims, heat, level = [], None, None
        for o in out["outputs"]:
            if o["type"] == "verdicts.update":
                claims = o["payload"]["claims"]
            elif o["type"] == "heat.update":
                heat = o["payload"]["heat"]
        level = out.get("target_level", level)
        flagged = [c for c in claims if c["final"] in ("red", "amber")]
        rows.append(
            {
                "turn": i + 1,
                "claims": claims,
                "flagged": flagged,
                "heat": heat,
                "level": level,
                "ms": out.get("elapsed_ms"),
            }
        )
        if not args.quiet:
            print(f"\n{BOLD}Turn {i + 1:2}{RESET} {DIM}{user[:70]}{RESET}")
            print(
                f"  heat {heat} · level {level} · {len(claims)} claims · {out.get('elapsed_ms')} ms"
            )
            for c in flagged:
                why = [
                    f"{r['detector']}={r['status']}"
                    for r in c["detector_results"]
                    if r["status"] in BAD
                ]
                print(f"  {COLOR[c['final']]}{c['final'].upper():6}{RESET} {c['quote'][:70]!r}")
                print(f"         {DIM}{', '.join(why)}{RESET}")
                for r in c["detector_results"]:
                    if r["status"] in BAD:
                        print(f"         {DIM}↳ {r['detector']}: {r['explanation'][:120]}{RESET}")

    # ------------------------------------------------------------------ summary
    all_claims = [c for r in rows for c in r["claims"] if c["final"] != "skipped"]
    red = [c for r in rows for c in r["flagged"] if c["final"] == "red"]
    amber = [c for r in rows for c in r["flagged"] if c["final"] == "amber"]
    causes: Counter[str] = Counter()
    for c in red + amber:
        for r in c["detector_results"]:
            if r["status"] in BAD:
                causes[f"{r['detector']}={r['status']}"] += 1

    def rate(chunk):
        checked = [c for r in chunk for c in r["claims"] if c["final"] != "skipped"]
        bad = [c for r in chunk for c in r["flagged"]]
        return f"{len(bad)}/{len(checked)}" + (
            f" ({100 * len(bad) / len(checked):.0f}%)" if checked else ""
        )

    third = max(1, len(rows) // 3)
    print(
        f"\n{BOLD}=== Long-chat false alarms [{name}] ({len(rows)} assistant turns, session {session}) ==="
        f"{RESET}"
    )
    print(f"checked claims : {len(all_claims)}")
    print(
        f"false alarms   : {COLOR['red']}{len(red)} red{RESET} + "
        f"{COLOR['amber']}{len(amber)} amber{RESET}"
        + (
            f"  → {100 * (len(red) + len(amber)) / len(all_claims):.0f}% of checked claims"
            if all_claims
            else ""
        )
    )
    print(
        f"early / middle / late turns: {rate(rows[:third])} · {rate(rows[third:2 * third])} "
        f"· {rate(rows[2 * third:])}"
    )
    print("heat by turn   : " + " ".join(str(r["heat"]) for r in rows))
    print(f"final level    : {rows[-1]['level'] if rows else '-'}")
    ms = [r["ms"] for r in rows if r["ms"]]
    if ms:
        print(
            f"latency        : median {statistics.median(ms):.0f} ms, max {max(ms)} ms, "
            f"total {time.time() - t_start:.0f} s"
        )
    print("caused by      :")
    for cause, n in causes.most_common():
        print(f"   {n:3}  {cause}")
    if not causes:
        print("   (nothing — no false alarms)")


if __name__ == "__main__":
    main()
