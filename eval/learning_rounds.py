"""FR-L7: evaluate Course Correct's prompt bandit on recorded, verified outcomes.

Each input case must have independently checked results for all three prompt variants,
captured in separate copies of the same conversation. The holdout split is never used to
choose or update variants. This is a replay simulation over recorded counterfactual trials;
it does not call the engine or generate model replies.
"""

from __future__ import annotations

import argparse
import json
import os
import random
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT / "learning_outcomes.jsonl"
DEFAULT_REPORT = ROOT / "learning_rounds.md"
DEFAULT_CHART = ROOT / "learning_rounds.png"
VARIANTS = ("v1", "v2", "v3")
ROUNDS = 5
MIN_HOLDOUT = 10
FAILURE_TYPES = {
    "knowledge_gap",
    "anchored_wrong_assumption",
    "caving",
    "fabricated_sources",
    "outdated_knowledge",
    "source_drift",
    "api_confusion",
}
PROMPT_TYPES = {
    "verify_nudge",
    "targeted_correction",
    "diagnostic_reset",
    "fresh_start",
}


class LearningDataError(ValueError):
    """Learning input is missing, ambiguous, or not independently verified."""


@dataclass(frozen=True)
class Case:
    case_id: str
    failure_type: str
    prompt_type: str
    split: str
    outcomes: dict[str, bool]


def load_cases(path: Path) -> list[Case]:
    if not path.is_file():
        raise LearningDataError(
            f"missing {path}; capture and independently verify v1/v2/v3 outcomes "
            "for training and held-out cases first"
        )
    cases: list[Case] = []
    seen: set[str] = set()
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        try:
            raw: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LearningDataError(
                f"{path}:{line_number}: invalid JSON: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise LearningDataError(
                f"{path}:{line_number}: each row must be a JSON object"
            )
        case_id = raw.get("id")
        failure_type = raw.get("failure_type")
        prompt_type = raw.get("prompt_type")
        split = raw.get("split")
        if not isinstance(case_id, str) or not case_id or case_id in seen:
            raise LearningDataError(f"{path}:{line_number}: missing or duplicate id")
        if not isinstance(failure_type, str) or failure_type not in FAILURE_TYPES:
            raise LearningDataError(
                f"{case_id}: unsupported failure_type {failure_type!r}"
            )
        if not isinstance(prompt_type, str) or prompt_type not in PROMPT_TYPES:
            raise LearningDataError(
                f"{case_id}: unsupported prompt_type {prompt_type!r}"
            )
        if not isinstance(split, str) or split not in {"train", "holdout"}:
            raise LearningDataError(f"{case_id}: split must be 'train' or 'holdout'")
        if raw.get("answer_origin") != "recorded_model":
            raise LearningDataError(f"{case_id}: answer_origin must be recorded_model")
        if raw.get("label_origin") != "independently_checked":
            raise LearningDataError(
                f"{case_id}: outcomes must be independently_checked"
            )
        outcomes = raw.get("outcomes")
        if not isinstance(outcomes, dict) or set(outcomes) != set(VARIANTS):
            raise LearningDataError(
                f"{case_id}: outcomes must contain exactly {', '.join(VARIANTS)}"
            )
        if not all(isinstance(outcomes[name], bool) for name in VARIANTS):
            raise LearningDataError(
                f"{case_id}: each variant outcome must be a JSON boolean"
            )
        seen.add(case_id)
        cases.append(Case(case_id, failure_type, prompt_type, split, outcomes))

    if not cases:
        raise LearningDataError(f"no cases in {path}")
    training = [case for case in cases if case.split == "train"]
    holdout = [case for case in cases if case.split == "holdout"]
    if not training:
        raise LearningDataError("at least one training case is required")
    if len(holdout) < MIN_HOLDOUT:
        raise LearningDataError(
            f"need at least {MIN_HOLDOUT} holdout cases; found {len(holdout)}"
        )
    train_types = {(case.failure_type, case.prompt_type) for case in training}
    holdout_types = {(case.failure_type, case.prompt_type) for case in holdout}
    missing_holdout = train_types - holdout_types
    if missing_holdout:
        raise LearningDataError(
            "every trained failure-type/prompt-type pair needs held-out cases; missing "
            + ", ".join(
                f"{failure}/{prompt}" for failure, prompt in sorted(missing_holdout)
            )
        )
    untrained_holdout = holdout_types - train_types
    if untrained_holdout:
        raise LearningDataError(
            "held-out cases need a trained failure-type/prompt-type pair; missing "
            + ", ".join(
                f"{failure}/{prompt}" for failure, prompt in sorted(untrained_holdout)
            )
        )
    return cases


def _sample_variant(
    rng: random.Random, counts: dict[str, dict[str, int]], bandit_key: str
) -> str:
    """Thompson sample from the Beta(1,1) prior plus completed-round observations."""
    draws = {
        variant: rng.betavariate(
            1 + counts[bandit_key][variant]["successes"],
            1 + counts[bandit_key][variant]["failures"],
        )
        for variant in VARIANTS
    }
    return max(VARIANTS, key=lambda variant: draws[variant])


def _empty_counts() -> defaultdict[str, dict[str, dict[str, int]]]:
    return defaultdict(
        lambda: {variant: {"successes": 0, "failures": 0} for variant in VARIANTS}
    )


def _rate(values: list[bool]) -> float | None:
    return sum(values) / len(values) if values else None


def run_learning(cases: list[Case], seed: int = 7) -> dict[str, Any]:
    """Run five online rounds; update the bandit only after each round completes."""
    training = [case for case in cases if case.split == "train"]
    holdout = [case for case in cases if case.split == "holdout"]
    counts = _empty_counts()
    rng = random.Random(seed)
    rounds = []

    for round_number in range(1, ROUNDS + 1):
        order = training.copy()
        random.Random(f"{seed}:round:{round_number}").shuffle(order)
        bandit_results: list[bool] = []
        baseline_results: list[bool] = []
        round_choices: list[tuple[str, str, bool]] = []
        # Freeze the posterior for this round; verified rewards update it after all cases run.
        for case in order:
            bandit_key = f"{case.failure_type}:{case.prompt_type}"
            choice = _sample_variant(rng, counts, bandit_key)
            reward = case.outcomes[choice]
            bandit_results.append(reward)
            baseline_results.append(case.outcomes["v1"])
            round_choices.append((bandit_key, choice, reward))
        for bandit_key, variant, reward in round_choices:
            counts[bandit_key][variant]["successes" if reward else "failures"] += 1
        rounds.append(
            {
                "round": round_number,
                "bandit_fixed": sum(bandit_results),
                "bandit_trials": len(bandit_results),
                "bandit_rate": _rate(bandit_results),
                "v1_fixed": sum(baseline_results),
                "v1_trials": len(baseline_results),
                "v1_rate": _rate(baseline_results),
            }
        )

    winners: dict[str, dict[str, Any]] = {}
    trained_pairs = sorted({(case.failure_type, case.prompt_type) for case in training})
    for failure_type, prompt_type in trained_pairs:
        bandit_key = f"{failure_type}:{prompt_type}"
        means = {
            variant: (1 + counts[bandit_key][variant]["successes"])
            / (
                2
                + counts[bandit_key][variant]["successes"]
                + counts[bandit_key][variant]["failures"]
            )
            for variant in VARIANTS
        }
        winner = max(VARIANTS, key=lambda variant: means[variant])
        winners[bandit_key] = {
            "failure_type": failure_type,
            "prompt_type": prompt_type,
            "variant": winner,
            "posterior_mean": means[winner],
            "counts": counts[bandit_key],
        }

    held_bandit = [
        case.outcomes[winners[f"{case.failure_type}:{case.prompt_type}"]["variant"]]
        for case in holdout
    ]
    held_v1 = [case.outcomes["v1"] for case in holdout]
    return {
        "seed": seed,
        "training_cases": len(training),
        "holdout_cases": len(holdout),
        "rounds": rounds,
        "winners": winners,
        "holdout": {
            "bandit_fixed": sum(held_bandit),
            "bandit_trials": len(held_bandit),
            "bandit_rate": _rate(held_bandit),
            "v1_fixed": sum(held_v1),
            "v1_trials": len(held_v1),
            "v1_rate": _rate(held_v1),
        },
    }


def write_chart(path: Path, result: dict[str, Any]) -> None:
    rounds = result["rounds"]
    x = [row["round"] for row in rounds]
    bandit = [row["bandit_rate"] * 100 for row in rounds]
    v1 = [row["v1_rate"] * 100 for row in rounds]
    heldout = result["holdout"]
    fig, axes = plt.subplots(1, 2, figsize=(10, 4.5), constrained_layout=True)
    axes[0].plot(x, bandit, marker="o", linewidth=2, label="Thompson sampling")
    axes[0].plot(x, v1, marker="o", linewidth=2, label="Always v1")
    axes[0].set(
        title="Fix rate by seeded evaluation round",
        xlabel="Round",
        ylabel="Fix rate (%)",
    )
    axes[0].set_xticks(x)
    axes[0].set_ylim(0, 100)
    axes[0].grid(axis="y", alpha=0.25)
    axes[0].legend()
    axes[1].bar(
        ["Learned\nvariant", "Always v1"],
        [heldout["bandit_rate"] * 100, heldout["v1_rate"] * 100],
        color=["#386cb0", "#9e9e9e"],
    )
    axes[1].set(title="Held-out fix rate", ylabel="Fix rate (%)", ylim=(0, 100))
    axes[1].grid(axis="y", alpha=0.25)
    fig.suptitle("Seeded evaluation rounds")
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=160)
    plt.close(fig)


def write_report(path: Path, chart_path: Path, result: dict[str, Any]) -> None:
    chart_link = Path(os.path.relpath(chart_path, start=path.parent)).as_posix()
    lines = [
        "# Course Correct learning rounds",
        "",
        "**Seeded evaluation rounds** — offline replay of recorded model replies and independently checked outcomes.",
        "",
        f"- Random seed: {result['seed']}",
        f"- Training cases: {result['training_cases']}",
        f"- Held-out cases, excluded from learning: {result['holdout_cases']}",
        "- Rounds: 5",
        "",
        f"![Fix rate across five rounds and held-out comparison]({chart_link})",
        "",
        "## Fix rate by round",
        "",
        "| Round | Thompson sampling | Always v1 |",
        "| ---: | ---: | ---: |",
    ]
    for row in result["rounds"]:
        lines.append(
            f"| {row['round']} | {row['bandit_fixed']}/{row['bandit_trials']} "
            f"({row['bandit_rate']:.1%}) | {row['v1_fixed']}/{row['v1_trials']} "
            f"({row['v1_rate']:.1%}) |"
        )
    lines += [
        "",
        "## Selected variant by failure type and prompt type",
        "",
        "| Failure type / prompt type | Variant | Posterior mean | Trials by variant |",
        "| --- | --- | ---: | --- |",
    ]
    for _bandit_key, data in sorted(result["winners"].items()):
        counts = "; ".join(
            f"{variant}: {data['counts'][variant]['successes']} fixed / "
            f"{data['counts'][variant]['failures']} not fixed"
            for variant in VARIANTS
        )
        lines.append(
            f"| {data['failure_type']} / {data['prompt_type']} | {data['variant']} "
            f"| {data['posterior_mean']:.1%} | {counts} |"
        )
    held = result["holdout"]
    lines += [
        "",
        "## Held-out comparison",
        "",
        (
            f"- Learned variant by failure type: {held['bandit_fixed']}/{held['bandit_trials']} "
            f"({held['bandit_rate']:.1%})."
        ),
        f"- Always v1: {held['v1_fixed']}/{held['v1_trials']} ({held['v1_rate']:.1%}).",
        "",
        "## Method and limits",
        "",
        (
            "Each case must have separate recorded follow-up replies for v1, v2, and v3, with "
            "each fixed/not-fixed result independently checked. Thompson sampling chooses one "
            "arm per training case; outcomes update the posterior after each round. The v1 "
            "baseline uses the same training cases. Held-out cases never update the bandit. "
            "These results measure the captured set and should not be described as general "
            "model performance."
        ),
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--chart", type=Path, default=DEFAULT_CHART)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    try:
        cases = load_cases(args.input)
    except LearningDataError as exc:
        parser.error(str(exc))
    result = run_learning(cases, args.seed)
    write_chart(args.chart, result)
    write_report(args.report, args.chart, result)
    print(f"Wrote {args.report} and {args.chart}")


if __name__ == "__main__":
    main()
