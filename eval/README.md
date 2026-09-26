# Role D evaluation (FR-D6)

`run_eval.py` replays stored conversations through the running engine's
`/debug/message` endpoint. It scores red flags against claim-level labels and
writes precision, recall, verified fix rate, and median verdict latency to
`results.md`. It does not ask a model to generate answers.

The runner matches each extracted claim using both its `verdicts.update` quote
and its normalized wording from the engine's local SQLite ledger. Labels with
numbers must share the same number with the engine claim. It then uses
stopword-free word overlap (Jaccard >= 0.4) and assigns labels one to one.
Unmatched engine claims appear as `unlabelled` in the per-case report and are
excluded from precision and recall; unmatched red labels count as misses.

## Current sample

`trap_prompts.jsonl` starts with seven scripted scenarios copied from
`shared/fixtures/scenarios/` by `seed_traps.py`. They make the harness usable
while the team collects the PRD's ~38 **captured model answers**. The pilot
numbers must not be presented as final model performance.
`seed_traps.py` refuses to overwrite an existing trap file unless `--force` is
passed; using `--force` would remove subsequently captured cases.

| Trap type | PRD target | Current seed | Captured replies still needed |
| --- | ---: | ---: | ---: |
| Fake citations | 8 | 1 | 7 |
| Checkable facts | 6 | 1 | 5 |
| Niche or unanswerable facts | 5 | 1 | 4 |
| Pushback | 5 | 1 | 4 |
| Long-document summaries | 5 | 1 | 4 |
| Python code/API | 5 | 1 | 4 |
| Clean controls | 4 | 1 | 3 |
| **Total** | **38** | **7** | **31** |

## Add a captured case

Append one JSON object per line to `trap_prompts.jsonl`. Each row needs:

- `id`: unique case name.
- `category`: one of the trap types in the table (the seed rows show the exact
  category names).
- `answer_origin`: `recorded_model` for a response captured from the model.
- `label_origin`: `independently_checked` after a person verifies each label
  against source material. The initial seed rows use fixture expectations.
- `events`: the actual conversation in order, using `message.new` envelopes
  with `message_id`, `role`, `text`, and `position` in each payload. Include a
  `session.start` event first. The runner assigns a fresh session ID.
- `claims`: independently checked labels for each claim in every recorded
  assistant answer. Each has the answer's `message_id`, an exact `quote`, and
  `label` of `red`, `not_red`, or `unknown`. Use `unknown` when a niche fact
  cannot be established. Label all factual claims, including correct controls.

For fix rate, capture the model's next assistant reply after accepting a
Course Correct prompt. Put a `correction.inserted` event between the two
assistant messages. The runner replaces its placeholder correction ID with the
one the engine actually offered. If no correction is offered, it reports that
the trial could not be measured. The first reply after insertion is the only
one counted for fix rate.

Do not commit real private conversations or pasted private documents. Use
synthetic prompts or public material with independently checked labels.

## Run

Start the engine as described in the root README. For a live judge run, load
the root `.env` explicitly when starting Uvicorn from `engine/`:

```bash
uvicorn app.main:app --port 8765 --reload --env-file ../.env
```

Filling `.env` alone does not export its keys into an already-running server.
Restart it after changing the file. Then, from the repository root run:

```bash
python eval/run_eval.py
```

The runner reads `engine/reigns.db` to get normalized claims. If the engine
uses a different `REIGNS_DB` path, pass it with `--ledger PATH`. Use
`--base-url`, `--traps`, `--output`, `--limit`, or `--timeout` if needed.
The report marks a run diagnostic-only when a required detector is missing,
checks error out, or scripted fixture answers are used. Review the engine log
and repeat with real model answers and working services before quoting metrics.

The PRD also asks for two correction baselines: the same model asked “are you
sure?” and a plain “that's wrong, fix it” prompt. Those require separately
captured follow-up answers. Add optional `baselines` entries named
`are_you_sure` and `plain_fix`, each with the captured `reply` and an
independently checked Boolean `fixed`. Use separate copies of the original
conversation for each baseline. The initial seed set does not measure them.
