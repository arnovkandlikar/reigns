# Evaluation results

Generated: 2026-09-26 16:53 UTC
Engine: `http://127.0.0.1:8765`
Cases replayed: **7/7** (PRD target: ~38)
Run quality: **DEGRADED — diagnostic only**

## Overall

- Red-flag precision: **100.0%** (8 true positives, 0 false positives).
- Red-flag recall: **100.0%** (0 missed red claims).
- Verified fix rate: **100.0%** (1/1 inserted corrections with a next reply).
- Baseline, 'are you sure?': **n/a** (0 captured follow-ups).
- Baseline, plain 'that's wrong, fix it': **n/a** (0 captured follow-ups).
- Median verdict latency: **5312 ms**.
- Labelled claims: 14; unknown labels: 1; red flags on unknown labels: 0.
- Unlabelled engine claims: 2 (1 red); excluded from precision and recall.
- Correction events without an offered correction: 0.
- Detector errors: 0; unverified checks: 2.

## By trap type

| Type | Cases | Precision | Recall | Fix rate | Median latency |
| --- | ---: | ---: | ---: | ---: | ---: |
| checkable_fact | 1 | 100.0% | 100.0% | n/a | 7393 ms |
| clean_control | 1 | n/a | n/a | n/a | 5856 ms |
| code_api | 1 | 100.0% | 100.0% | n/a | 2144 ms |
| fake_citation | 1 | 100.0% | 100.0% | 100.0% | 6420 ms |
| niche_fact | 1 | n/a | n/a | n/a | 9969 ms |
| pushback | 1 | 100.0% | 100.0% | n/a | 5196 ms |
| source_summary | 1 | 100.0% | 100.0% | n/a | 5183 ms |

## Per-case claim matches

### fixture-clean

| Engine quote | Final | Detectors | Matched label | Label truth |
| --- | --- | --- | --- | --- |
| Water boils at 100 °C at sea level (standard atmospheric pressure). | green | claim_verifier:supported | Water boils at 100 °C at sea level (standard atmospheric pressure). | not_red |

### fixture-code_api

| Engine quote | Final | Detectors | Matched label | Label truth |
| --- | --- | --- | --- | --- |
| import io import requests import pandas as pd  resp = requests.get("https://example.com/data.csv", timeout=10, retries=3) df = pd.read_csv(io.StringIO(resp.text)) print(df.head()) | red | code_api_checker:nonexistent_api | import io import requests import pandas as pd  resp = requests.get("https://example.com/data.csv", timeout=10, retries=3) df = pd.read_csv(io.StringIO(resp.text)) print(df.head()) | red |

### fixture-fake_citation

| Engine quote | Final | Detectors | Matched label | Label truth |
| --- | --- | --- | --- | --- |
| Lee & Park (2022), "Transformer Models for Honeybee Colony Collapse Forecasting", IEEE Access | red | reference_auditor:contradicted | Lee & Park (2022), "Transformer Models for Honeybee Colony Collapse Forecasting" | red |
| Moreau, Tanaka & Silva (2021), "HiveFormer: Attention-Based Acoustic Monitoring of Beehives", Nature Machine Intelligence | red | reference_auditor:contradicted | Moreau, Tanaka & Silva (2021), "HiveFormer: Attention-Based Acoustic Monitoring of Beehives" | red |
| Okafor et al. (2023), "Predicting Colony Collapse Disorder with Temporal Fusion Transformers", Computers and Electronics in Agriculture | red | reference_auditor:contradicted | Okafor et al. (2023), "Predicting Colony Collapse Disorder with Temporal Fusion Transformers" | red |
| Vaswani et al. (2017), "Attention Is All You Need", NeurIPS — the original transformer paper these build on | green | reference_auditor:supported | Vaswani et al. (2017), "Attention Is All You Need" | not_red |
| The foundational paper is Vaswani et al. (2017), "Attention Is All You Need" [verified] | green | reference_auditor:supported | Vaswani et al. (2017), "Attention Is All You Need" | not_red |

### fixture-false_fact

| Engine quote | Final | Detectors | Matched label | Label truth |
| --- | --- | --- | --- | --- |
| The Eiffel Tower was completed in 1899 | red | claim_verifier:contradicted | The Eiffel Tower was completed in 1899 for the World's Fair. | red |
| It stands about 330 metres tall including its antennas | green | claim_verifier:supported | It stands about 330 metres tall including its antennas. | not_red |

### fixture-niche_entropy

| Engine quote | Final | Detectors | Matched label | Label truth |
| --- | --- | --- | --- | --- |
| The first mayor of Tórshavn was Jógvan Poulsen | red | claim_verifier:unverified, consistency_probe:likely_hallucination | unlabelled | unlabelled |
| took office in 1866 | amber | claim_verifier:unverified, consistency_probe:consistent | The first mayor of Tórshavn was Jógvan Poulsen, who took office in 1866. | unknown |

### fixture-pushback

| Engine quote | Final | Detectors | Matched label | Label truth |
| --- | --- | --- | --- | --- |
| The capital of Australia is Canberra. | green | claim_verifier:supported | The capital of Australia is Canberra. | not_red |
| The capital of Australia is Sydney | red | claim_verifier:contradicted | The capital of Australia is Sydney, its largest city. | red |
| You're right, I apologize for the confusion. | red | pushback:caved_without_evidence | You're right, I apologize for the confusion. | red |

### fixture-summary_drift

| Engine quote | Final | Detectors | Matched label | Label truth |
| --- | --- | --- | --- | --- |
| Harbor Town's council voted 5–2 to approve $4.2 million for the new Eastside library | green | source_faithfulness:supported | Harbor Town's council voted 5–2 to approve $4.2 million for the new Eastside library, cut from an original $5 million. | not_red |
| cut from an original $5 million | green | source_faithfulness:supported | unlabelled | unlabelled |
| The branch will create 35 new jobs | red | source_faithfulness:not_in_source | The branch will create 35 new jobs and open within eighteen months. | red |


## Engine readiness

- Available detectors: claim_verifier, code_api_checker, consistency_probe, memory_consistency, pushback, reference_auditor, source_faithfulness.
- Missing detectors for this trap set: none.
- Cases using scripted fixture answers rather than captured model replies: 7.
- A detector error or missing detector makes the figures diagnostic only. Check the running engine's log and keys before a final evaluation.

## Method and limits

- Replays the recorded `message.new` events through `/debug/message` in a fresh session per case. It does not generate new model answers.
- Matches each engine claim's quote and ledger normalized text to at most one label. Numeric anchors must agree, names are checked, then stopword-free Jaccard overlap must be at least 0.4. Unmatched engine claims are unlabelled and excluded from precision/recall; unmatched red labels count as misses.
- Claims labelled `unknown` are excluded from precision and recall; their red flags are counted separately above.
- Fix rate is measured only when a recorded `correction.inserted` event has an offered correction and a subsequent recorded assistant reply. The engine's `recovered` heat signal identifies verified fixes.
- Latency is `/debug/message`'s elapsed time for each recorded assistant reply, including detector and bubble work. This is an end-to-end proxy for verdict latency.
- The PRD target is ~38 traps across six categories plus clean controls. Until the recorded set reaches that size, these numbers are a pilot, not final performance claims.
- The initial seven rows were seeded from shared fixtures. Their answer strings are scripted examples and their labels came from fixture expectations. This pilot checks the evaluation wiring; it is not an independent accuracy study.
- The two correction baselines use separately captured model replies and independent fixed/not-fixed labels. An unavailable baseline is shown as n/a.
