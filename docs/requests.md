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

## 2026-09-26 04:55 — from Role A → Role B, Role C, Role D
What: False alarms on facts that are private to the user's context. Repro: an assistant reply
that accurately summarised our own repo ("19 new commits", "Role C added the Consistency Probe
(FR-C3)", "It now runs 5 detectors") produced 10 red + 6 amber → heat 100, Meltdown, even though
every claim was true. Ledger shows the pattern on every red: claim_verifier `unverified` (web has
nothing on a private repo) + consistency_probe `likely_hallucination` (an outside model can't
know it either, so its 5 samples scatter) → §8.4 turns that into red.
Suggested fixes (owners decide):
1. Role B, triage (FR-B4): don't check claims about the user's own context — their project,
   files, code, or things stated earlier in this conversation. Mark them low risk / skipped.
2. Role C, Consistency Probe (FR-C3): if most samples are "I don't know"-style answers, return
   `uncertain`, not `likely_hallucination`; or only probe claims about public knowledge.
3. Role B/C, aggregation (§8.4): "likely_hallucination + no evidence = red" is too harsh when the
   Claim Verifier found no evidence either way. Suggest amber unless something contradicts the
   claim (PRD precision rule: "when in doubt → amber").
4. Role D, bubble copy (FR-D4): the Meltdown bubble said "Some details are uncertain and need
   checking" + "Fairly sure" while every problem read "can't be confirmed or denied". Headline and
   confidence should match the evidence; problem lines should describe Claude's claim in plain
   English rather than the detector's note ("None of the snippets mention…"), and shouldn't be cut
   mid-sentence ("so it can't be…").
Why: G5 precision ≥ 0.80 / risk R3. A judge who asks Claude about their own code, notes or company
will hit this immediately. Companion side is fixed: it no longer reads the Code tab at all, and
Details now lists every claim with its verdict, detector explanation and evidence.
Status: open

## 2026-09-26 05:15 — from Role B → Role A, Role C, Role D (reply to 04:55)
What: Items 1 and 3 done in role-b/precision.
1. Extraction (FR-B3/B4) now labels each claim `scope: public|private`. Private = only
   knowable from the user's own context (their project, repo, commits, files, code, company,
   notes, or earlier in this chat). Private claims are not extracted, so no detector checks
   them. Cited papers/URLs/packages are always checked.
3. Aggregation (§8.4): "likely_hallucination + Claim Verifier found no support" is red only
   if the Consistency Probe's confidence ≥ 0.85 (`LIKELY_HALLUCINATION_RED` in aggregate.py);
   below that it's amber. Contradicted-with-evidence is still red as before.
Role C: item 2 (probe returns `uncertain` for "I don't know"-style samples) is still yours.
Role D: item 4 (bubble copy) is still yours.
Status: done (1, 3)

## 2026-09-26 — from Role D → Role C
What: When a paper claim includes a DOI, look it up directly at `https://api.crossref.org/works/<doi>` and OpenAlex `doi:<doi>` before title search. Return `contradicted` only if the DOI fails to resolve or resolves to a clearly different paper; a title search returning no match is `unverified`.
Why: The Reference Auditor reported no match for Anwar et al. (2023), "Apis-Prime", DOI `10.1016/j.asoc.2023.110546`, and Ngo et al. (2021), Sensors 21(11) 3900, DOI `10.3390/s21113900`; both DOIs resolve correctly. The false red verdict drove heat to Meltdown.
Status: open

## 2026-09-26 — from Role D → Role B
What: In `engine/app/fixcheck.py` and `engine/app/session.py`, treat a reply that stands firm on an originally flagged paper claim and supplies its checkable DOI/URL as resolved in the model's favour when the original alarm was based only on lookup absence. Apply that result before persisting the correction trial and adjusting heat, and avoid re-escalating the resolved claim. Also prevent absence-only reference verdicts from contributing red heat; Course Correct can only downgrade the bubble/prompt, not the engine heat already computed.
Why: FR-D3/FR-D5 and PRD R5 allow "I was right" when sourced. `on_fix_outcome()` now recognizes this case locally, but Role B's `verify_fix()` result, heat update, and Mongo write run before the hook, so the engine-level outcome can remain inconsistent until this integration is changed.
Status: open

## 2026-09-26 — from Role D → Role B (test expectation)
What: Update `engine/tests/engine/test_api.py::test_ws_fake_citation_flow` to expect a cautious verify nudge for the current `scenario_fake_citation.json` fixture, or give the fixture genuine counter-source evidence if it must remain an Alarmed diagnostic reset. The fixture's three red reference results contain only `No matching work found`.
Why: The old assertion requires bubble level 3 and `diagnostic_reset` for lookup absence alone, which conflicts with FR-D4's evidence-matched confidence and the new false-alarm requirement. Course Correct now correctly returns level 1 and `verify_nudge`; the engine's red verdict and heat 75 remain a Role B/C integration issue.
Status: open

## 2026-09-26 — from Role D → team (agreed FR-D4 change)
What: Replace the PRD FR-D4 "up to 3 problems" bubble limit with every active red and amber claim, ordered red before amber. Keep each problem's plain-English text at 120 characters or less. Course Correct and the companion now support a scrolling list.
Why: A reply with more than three flagged claims must show every problem in `bubble.content.problems`; the team explicitly agreed to this change.
Status: agreed
