# Reigns

macOS desktop pet that watches the Claude desktop app, catches hallucinations, and helps the AI
fix them. **`PRD.md` is the source of truth.**

## Repo map & owners (PRD §6, §15.1)

| Path | Owner |
|---|---|
| `companion/` | Role A (Swift) |
| `engine/app/` (main, models, extraction, triage, aggregate, heat, ledger, session, plugins, llm, voice, learning/store.py) | Role B |
| `engine/app/detectors/pushback.py` | Role B |
| `engine/app/detectors/*` (everything else) + `engine/tests/detectors/` | Role C |
| `engine/app/learning/memory.py` | Role C |
| `engine/app/detectors/source_faithfulness.py`, `engine/app/course_correct/`, `engine/app/learning/calibration.py`, `eval/`, `pitch/` | Role D |
| `shared/schemas/`, `shared/fixtures/` | Role B (steward — generated, don't hand-edit) |
| `docs/requests.md` | everyone (append only) |

## First-time setup (everyone)

```bash
git clone <repo> && cd reigns
git config reigns.role C            # your role letter: A, B, C or D
sh scripts/install_hooks.sh          # blocks commits to folders you don't own
cp .env.example .env                 # fill in your keys; .env is git-ignored
```

Engine (Roles B, C, D):

```bash
cd engine
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest                                        # should be all green
uvicorn app.main:app --port 8765 --reload     # engine at ws://127.0.0.1:8765/ws
```

## Testing without the Mac app

```bash
curl localhost:8765/health
curl localhost:8765/debug/status                          # which detectors are plugged in
curl localhost:8765/debug/scenarios
curl -X POST localhost:8765/debug/scenario/scenario_fake_citation
curl -X POST "localhost:8765/debug/message?session_id=me" -H 'content-type: application/json' \
  -d '{"message_id":"m1","role":"assistant","position":1,"text":"The Eiffel Tower was completed in 1899."}'
```

## How to plug your code in (no registration, no editing Role B's files)

**Role C / D detectors** — create `engine/app/detectors/<name>.py` with a module-level `detector`:

```python
from app.models import Claim, DetectorResult, Evidence, SessionContext
from app.llm import complete_json, LLMError   # shared helper: REIGNS_MODEL, JSON parsing, retry

class ReferenceAuditor:
    name = "reference_auditor"
    async def check(self, claim: Claim, session: SessionContext) -> DetectorResult: ...

detector = ReferenceAuditor()
```

Names: `reference_auditor`, `claim_verifier`, `consistency_probe`, `code_api_checker`,
`source_faithfulness`. The engine imports it automatically the next time it starts. If it's
missing or crashes, the engine skips it or records `status: "error"`, and nothing else breaks.
Use `session.cache` for per-session lookup caching (FR-C5).

**Role D Course Correct** — create `engine/app/course_correct/api.py` with
`diagnose(session) -> DriftProfile` and `build_bubble(level, profile, session) -> BubbleContent`
(§12.5), optionally `on_fix_outcome(session, correction_id, fixed)`. Until it exists the engine
sends a simple fallback bubble.

**Role D calibration** — `engine/app/learning/calibration.py` with `red_threshold(detector) -> float`.

**Tests** — use the pytest fixtures in `engine/tests/conftest.py` (`load_scenario`, `fixtures_dir`).
Every scenario has `expected.claims` (detector inputs) and `expected.detector_results`.

**Role A mock mode** — each `shared/fixtures/scenarios/*.json` has `companion_to_engine` (what you
send) and `expected.engine_to_companion` (what you'll receive, in order).

## Git rules that keep us conflict-free (PRD §15.2)

1. Only commit inside your folders (the hook enforces it). Need something elsewhere → append to
   `docs/requests.md`.
2. Branch per feature: `role-c/reference-auditor`. Small PRs. The owning role merges.
3. Before opening a PR: `git fetch && git rebase origin/main` (commit or stash first!).
4. Never hand-edit `shared/schemas` or `shared/fixtures`. §12 changes go to Role B, who runs
   `python scripts/export_schemas.py && python scripts/make_fixtures.py` in one commit.
5. Don't add dependencies. `engine/pyproject.toml` already has the full §14 stack; ask Role B.
6. Commit messages start with the requirement ID: `FR-C1: add Crossref lookup`.
7. Format Python with `black --line-length 100` before committing (avoids whitespace-only diffs).
