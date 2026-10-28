"""Score the incident pair judge on synthetic or private labelled facts (no images/audio).

    uv run python -m scripts.eval_dedupe [pairs.json] --judge muse-spark-1.3 [--yes]

Only the bundled synthetic fixture may live inside this public repository. Keep additional
labelled data outside it. Every context is rebuilt with the production redaction/allowlist.
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.evaluation import binary_scores, outside_repo
from app.meta.factory import LLM_BACKENDS, make_pair_judge
from app.meta.llm import AgentLLM
from app.pair_judge import judge_pair, report_context

DEFAULT_PAIRS = Path(__file__).resolve().parents[1] / "tests/fixtures/dedupe_pairs.json"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("pairs", type=Path, nargs="?", default=DEFAULT_PAIRS)
    parser.add_argument("--llm", "--judge", dest="judge", choices=list(LLM_BACKENDS))
    parser.add_argument("--yes", action="store_true", help="skip the paid-call confirmation")
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
    labels, predictions = [], []
    unsure = errors = 0
    for pair in pairs:
        labels.append(pair["label"] == "same")
        try:
            reply = await judge_pair(
                judge,
                [context_for_judge(c) for c in pair["incident_context"]],
                context_for_judge(pair["new_report"]),
            )
            review = reply["decision"] == "unsure" or reply["confidence"] < threshold
            prediction = reply["decision"] == "same" and not review
            unsure += review
            if review or prediction != labels[-1]:
                print(
                    f"miss {pair['id']}: expected {pair['label']}, got {reply['decision']} "
                    f"({reply['confidence']:.2f}); {reply['reason']}"
                    + (" [officer review]" if review else "")
                )
        except Exception as exc:
            errors += 1
            unsure += 1
            prediction = False
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
    return 1 if errors else 0


async def run(args: argparse.Namespace) -> int:
    try:
        pairs = load_pairs(args.pairs)
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
        if not args.yes and input("Spend credits on uncached pairs? [y/N] ").strip().lower() != "y":
            return 1
        judge = make_pair_judge(settings)
        try:
            return await evaluate(pairs, judge, threshold=settings.pair_merge_threshold)
        finally:
            await judge.aclose()
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
