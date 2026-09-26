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
