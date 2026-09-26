# Cross-role change requests (PRD §14 rule 2)

Need a change in a file you don't own? Append an entry at the bottom. Never edit
someone else's entry. (This file uses git's `union` merge, so appends from two people
merge without a conflict.)

Format:

```
## YYYY-MM-DD HH:MM — from Role X → Role Y
What: ...
Why: ...
Status: open
```

---

## 2026-09-26 02:54 — from Role D → Role B
What: Please coordinate a team-approved async Course Correct diagnosis hook (or another non-blocking way for `diagnose()` to use the shared LLM judge), and await it in the engine integration.
Why: FR-D1 requires LLM-judged claim dependencies for the blast radius, while the current §12.5 `diagnose(session)` contract is synchronous and the engine processes replies inside an asyncio event loop.
Status: open

## 2026-09-26 02:54 — from Role D → Role B
What: Please preserve the selected correction `variant_id` in `CorrectionRecord` and pass the original failed claim IDs into fix verification; re-run their relevant detectors on the next assistant reply and persist the resulting trial to `prompt_trials` / `prompt_variants`.
Why: FR-D5 requires rechecking the claims the correction targeted, and FR-L2 needs the verified outcome to reward the selected bandit variant; the current engine-level fix check looks only for any red verdict and does not preserve the selected variant.
Status: open

## 2026-09-26 04:00 — from Role B → Role D (reply to both 02:54 requests)
What: Done in role-b/fix-verification.
1. Async hook: `diagnose()` and `build_bubble()` in course_correct/api.py may now be sync OR
   `async def` — the engine awaits them either way (app/plugins.py). They get 4 s total
   (`COURSE_CORRECT_TIMEOUT_S`); on timeout/exception the fallback bubble is used. Use
   `from app.llm import complete_json` inside an async diagnose().
2. Variant + targeted verification:
   - Tag the variant you chose: `session.cache[f"course_correct:variant:{correction_id}"] = "fabricated_sources:diagnostic_reset:v2"` inside build_bubble. The engine stores it as `CorrectionRecord.variant_id`.
   - Each correction records `target_claim_ids` (claims shown in the bubble + all active red/amber).
   - After `correction.inserted`, the next reply is judged by app/fixcheck.py: not fixed if it
     re-asserts a target (still red/amber) or has any red claim. Result → `on_fix_outcome()`.
   - Fixed → targets go into `session.resolved_claim_ids` (hidden from `active_verdicts()`,
     so they leave your bubble) and their heat is removed, then −20.
   - Mongo: `prompt_trials` row + `prompt_variants` `$inc` alpha (fixed) / beta (not fixed),
     counts start at 0 — add your prior (e.g. Beta(1,1)) when sampling.
Status: done
