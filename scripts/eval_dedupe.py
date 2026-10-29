"""Score the incident pair judge on synthetic or private labelled facts (no images/audio).

    uv run python -m scripts.eval_dedupe [pairs.json] --judge muse-spark-1.3 [--yes]
        [--record data/eval/results.json [--dataset ID]] [--spend-cap USD]

Only the bundled synthetic fixture may live inside this public repository. Keep additional
labelled data outside it. Every context is rebuilt with the production redaction/allowlist.
After confirmation, a free cache-only dry run counts the uncached pairs; their estimated cost is
checked against the spend cap (scripts/spend.py) before any paid call. --record writes the
metrics into the results file (app/eval_results.py); the bundled fixture's dataset id is the
default.
"""

import argparse
import asyncio
import contextlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from app import eval_results
from app.config import get_settings
from app.evaluation import binary_scores, outside_repo
from app.meta.factory import LLM_BACKENDS, make_pair_judge
from app.meta.llm import AgentLLM
from app.pair_judge import judge_pair, report_context
from scripts import spend

DEFAULT_PAIRS = Path(__file__).resolve().parents[1] / "tests/fixtures/dedupe_pairs.json"
DEFAULT_DATASET = "dedupe-pairs-64"  # the bundled fixture's id in the results file
# Estimate per uncached pair: about 1.5k prompt + 1.5k output (reasoning) tokens at $1.25/$4.25
# per 1M; a length retry can add a second call.
COST_PER_PAIR = 0.01


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("pairs", type=Path, nargs="?", default=DEFAULT_PAIRS)
    parser.add_argument("--llm", "--judge", dest="judge", choices=list(LLM_BACKENDS))
    parser.add_argument("--yes", action="store_true", help="skip the paid-call confirmation")
    parser.add_argument("--record", type=Path, metavar="RESULTS", help="upsert into this file")
    parser.add_argument("--dataset", help=f"dataset id for --record (default {DEFAULT_DATASET})")
    spend.add_cap_argument(parser)
    return parser.parse_args(argv)


def load_pairs(path: Path) -> list[dict[str, Any]]:
    path = path.expanduser().resolve()
    if path != DEFAULT_PAIRS:
        outside_repo(path.parent)
    pairs = json.loads(path.read_text("utf-8"))
    if not isinstance(pairs, list) or not pairs:
        raise ValueError("pairs must be a non-empty JSON list")
    ids = set()
    for pair in pairs:
        if not isinstance(pair, dict) or pair.get("label") not in ("same", "different"):
            raise ValueError("each pair needs a same/different label")
        if not isinstance(pair.get("id"), str) or pair["id"] in ids:
            raise ValueError("each pair needs a unique string id")
        ids.add(pair["id"])
        if not isinstance(pair.get("incident_context"), list) or not pair["incident_context"]:
            raise ValueError(f"{pair['id']}: incident_context must be a non-empty list")
        for context in [*pair["incident_context"], pair.get("new_report")]:
            if not isinstance(context, dict) or not isinstance(context.get("facts"), dict):
                raise ValueError(f"{pair['id']}: every report context needs facts")
    return pairs


def context_for_judge(raw: dict[str, Any]) -> dict[str, Any]:
    context = report_context(
        barangay=raw.get("barangay"),
        facts=raw["facts"],
        judgments=raw.get("photo_judgments", []),
    )
    if raw.get("observed_at"):
        # Parse and reformat so arbitrary text cannot be smuggled into a timestamp.
        context["observed_at"] = datetime.fromisoformat(raw["observed_at"]).isoformat()
    return context


async def evaluate(pairs: list[dict[str, Any]], judge: AgentLLM, *, threshold: float) -> int:
    code, _metrics = await evaluate_metrics(pairs, judge, threshold=threshold)
    return code


async def evaluate_metrics(
    pairs: list[dict[str, Any]], judge: AgentLLM, *, threshold: float
) -> tuple[int, dict[str, Any]]:
    """Judge every pair, print misses and the summary; return the exit code and the metrics."""
    labels, predictions, miss_ids = [], [], []
    unsure = errors = 0
    for pair in pairs:
        labels.append(pair["label"] == "same")
        try:
            reply = await judge_pair(
                judge,
                [context_for_judge(c) for c in pair["incident_context"]],
                context_for_judge(pair["new_report"]),
            )
            review = reply["decision"] == "unsure" or (
                reply["decision"] == "same" and reply["confidence"] < threshold
            )
            prediction = reply["decision"] == "same" and not review
            unsure += review
            if review or prediction != labels[-1]:
                miss_ids.append(pair["id"])
                print(
                    f"miss {pair['id']}: expected {pair['label']}, got {reply['decision']} "
                    f"({reply['confidence']:.2f}); {reply['reason']}"
                    + (" [officer review]" if review else "")
                )
        except Exception as exc:
            errors += 1
            unsure += 1
            prediction = False
            miss_ids.append(pair["id"])
            print(f"miss {pair['id']}: expected {pair['label']}, error: {exc}")
        predictions.append(prediction)
    scores = binary_scores(labels, predictions)
    print(f"\n{judge.model}: {len(pairs)} pairs, same threshold {threshold:.2f}")
    print(f"Precision: {scores.precision:.3f}  Recall: {scores.recall:.3f}  F1: {scores.f1:.3f}")
    print(
        f"Unsure rate (includes low confidence/errors): {unsure / len(pairs):.1%} "
        f"({unsure}/{len(pairs)}); errors: {errors}"
    )
    print(f"TP={scores.tp} FP={scores.fp} FN={scores.fn} TN={scores.tn}")
    metrics = {
        **eval_results.scores_dict(scores),
        "threshold": threshold,
        "review_rate": round(unsure / len(pairs), 4),
        "reviews": unsure,
        "errors": errors,
        "miss_ids": miss_ids,
    }
    return (1 if errors else 0), metrics


async def estimate_cost(pairs: list[dict[str, Any]], judge: AgentLLM) -> tuple[float, str]:
    """USD for the pairs not cached yet, from a free cache-only dry run (worst case for fakes)."""
    if not spend.probe_ready(judge):
        uncached = len(pairs)
    else:
        with spend.probe(judge):
            for pair in pairs:
                with contextlib.suppress(Exception):  # the client counts an uncached pair
                    await judge_pair(
                        judge,
                        [context_for_judge(c) for c in pair["incident_context"]],
                        context_for_judge(pair["new_report"]),
                    )
        uncached = spend.misses(judge)
    return uncached * COST_PER_PAIR, f"{uncached} uncached pair(s)"


def dataset_for(args: argparse.Namespace) -> str | None:
    """The dataset id to record under, after checking the results file describes it."""
    if not args.record:
        return None
    default = args.pairs.expanduser().resolve() == DEFAULT_PAIRS
    dataset = args.dataset or (DEFAULT_DATASET if default else None)
    if dataset is None:
        raise ValueError("--record with your own pairs needs --dataset")
    if dataset not in eval_results.dataset_ids(args.record):
        raise ValueError(f"dataset {dataset!r} is not described in {args.record}")
    return dataset


async def run(args: argparse.Namespace) -> int:
    try:
        pairs = load_pairs(args.pairs)
        dataset = dataset_for(args)
        settings = get_settings()
        if args.judge:
            settings = settings.model_copy(update={"pair_judge_llm": args.judge})
        name = settings.pair_judge_llm or settings.agent_llm
        print(
            f"{len(pairs)} pairs, judge {name}. Uncached calls spend provider credits; "
            "a length retry can add a second call per pair. Cached reruns are free. "
            "Dollar cost is not estimated (provider pricing must be checked before use)."
        )
        if name == "muse-glimmer":
            print("Glimmer serverless availability is unverified; errors never switch models.")
        print(f"At most ${len(pairs) * COST_PER_PAIR:.2f} if no pair is cached.")
        if not args.yes and input("Spend credits on uncached pairs? [y/N] ").strip().lower() != "y":
            return 1
        judge = make_pair_judge(settings)
        ledger = spend.SpendLedger(settings.data_dir, args.spend_cap, script="eval_dedupe")
        spend.attach(ledger, judge)
        try:
            estimate, detail = await estimate_cost(pairs, judge)
            if not ledger.approve(estimate, detail):
                return 1
            code, metrics = await evaluate_metrics(
                pairs, judge, threshold=settings.pair_merge_threshold
            )
        finally:
            await judge.aclose()
            ledger.close()
        print(f"logged spend for this run: ${ledger.run_spend:.4f}")
        if dataset is not None:
            if metrics["errors"]:
                print("not recorded: some pairs failed; re-run when the provider answers")
                return code
            entry = eval_results.upsert(
                args.record,
                {
                    "task": "pair_judge",
                    "dataset": dataset,
                    "model": name,
                    "model_id": judge.model,
                    "backend": eval_results.backend_of(judge),
                    "sam": None,
                    "date": eval_results.manila_today(),
                    "commit": eval_results.git_commit(),
                    "n": len(pairs),
                    "errors": 0,
                    "spend_usd": ledger.run_spend,
                    "metrics": metrics,
                },
            )
            print(f"recorded pair_judge/{dataset}/{entry['model']} in {args.record}")
        return code
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
