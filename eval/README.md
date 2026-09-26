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

## Public dataset seeding (FR-L6)

`seed_datasets.py` downloads the official releases for
[HaluEval](https://github.com/RUCAIBox/HaluEval),
[TruthfulQA](https://github.com/sylinrl/TruthfulQA), and
[FEVER](https://fever.ai/dataset/fever.html). It selects 25 correct and 25
known-wrong examples from each of HaluEval QA, HaluEval summarization,
TruthfulQA, and FEVER: 200 cases total. The local output is
`dataset_cases.jsonl`. Downloads are cached in `eval/.dataset_cache/`. Git
ignores both files; `dataset_manifest.json` records counts, source URLs, and
source file hashes without republishing dataset content.

```bash
python eval/seed_datasets.py
python eval/seed_datasets.py --offline  # rebuild from the cached source files
```

Every case has a stable ID, source URL, source row ID, source file SHA-256,
`origin: "dataset"`, and `confirmed_by: "dataset"`. FEVER's `NOT ENOUGH INFO`
rows are excluded. Its evidence IDs are not copied into `evidence_snippet` as
though they were evidence quotes. HaluEval's wrong answers and summaries are
dataset-generated examples; the labels refer to the complete response, not to
each sentence. TruthfulQA uses the dataset's Best Answer and Best Incorrect
Answer columns. No full source documents or conversation transcripts enter the
Mongo cases.

To upsert the cases into Atlas, after reviewing the JSONL, run:

```bash
python eval/seed_datasets.py --offline --mongo --embed --env-file .env
python eval/seed_datasets.py --verify-only --env-file .env
```

`--mongo` uses stable IDs, so repeating it does not duplicate cases. `--embed`
uses the project's Voyage key to make the new cases available to vector search;
without it, the cases are stored without vectors. A later `--embed` run fills
in vectors for existing cases. HaluEval's repository is
MIT licensed, TruthfulQA is Apache 2.0, and
[FEVER's license](https://fever.ai/download/fever/license.html) incorporates
Wikipedia attribution and share-alike terms. These public seed cases are
learning inputs. They are separate from `trap_prompts.jsonl` and do not count
as captured model answers or independent evaluation results.

## Prompt bandit learning rounds (FR-L7)

`learning_rounds.py` evaluates the Course Correct Thompson sampler over five
seeded rounds, compares it with always choosing v1, reports the selected variant
by failure type, and scores the final choices on a held-out set. It writes
`learning_rounds.md` and `learning_rounds.png`. This is an offline replay of
captured outcomes; it does not generate replies or write bandit state to Atlas.

Before running it, create `learning_outcomes.jsonl`. Each row represents one
original trap prompt and must include an independently checked outcome for all
three prompt variants, each captured in a separate copy of the same conversation:

```json
{"id":"citation-02","failure_type":"fabricated_sources","prompt_type":"diagnostic_reset","split":"train","answer_origin":"recorded_model","label_origin":"independently_checked","outcomes":{"v1":true,"v2":false,"v3":true}}
```

Use `split: "train"` for cases the sampler can learn from and `split:
"holdout"` for about ten cases excluded from all updates. Assign splits before
reviewing outcomes and keep them fixed. Each outcome is `true` only when a
person checked that the recorded next reply corrected the targeted claim.

To capture the variants, run the engine with `WITNESS_PROMPT_VARIANT=v1`,
`v2`, or `v3` and record a fresh reply after inserting that variant's prompt.
Unset the variable for normal Thompson sampling. For example, from PowerShell:

```powershell
$env:WITNESS_PROMPT_VARIANT = "v2"
uvicorn app.main:app --port 8765 --reload --env-file ../.env
```

Restart with the next variant for each separate capture. The override only
selects the prompt during controlled capture; it does not change trial counts.

After the input contains training and held-out cases, run:

```bash
python eval/learning_rounds.py --seed 7
```

Bandit arms are tracked by failure type and prompt type, matching the engine's
variant IDs. The current seven scripted trap fixtures do not include three independently
checked follow-ups per case, so they cannot produce an FR-L7 result. The runner
rejects missing or unverified outcome data instead of creating a synthetic
learning curve. Do not describe results from this seeded replay as general
model performance.
