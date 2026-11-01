"""Score the not-a-report screen (app.screening) on labelled messages, with real cached calls.

    uv run python -m scripts.eval_screen [messages.json] [--llm muse-spark-1.3] [--yes]
        [--record data/eval/results.json [--dataset ID]] [--spend-cap USD]

Each message goes through the pipeline's own screen in its order (`screening.screen_text`):
the free rule, the facts call, then the screening call only when the facts state nothing. The
"real report" class is the positive one: precision is the share of kept messages that are
reports, recall the share of real reports kept, and "wrongly filtered" counts real reports the
screen dropped (the target is 0). Only the bundled synthetic set may live in this repository.
After confirmation, a free cache-only dry run counts the uncached messages; their estimated cost
is checked against the spend cap (scripts/spend.py) before any paid call.
"""

import argparse
import asyncio
import contextlib
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app import eval_results
from app.config import get_settings
from app.evaluation import binary_scores, outside_repo
from app.meta.factory import LLM_BACKENDS, make_agent_llm
from app.meta.llm import AgentLLM
from app.screening import screen_text
from scripts import spend

DEFAULT_MESSAGES = Path(__file__).resolve().parents[1] / "data/eval/screen_messages.json"
DEFAULT_DATASET = "screen-messages-60"  # the bundled set's id in the results file
LABELS = ("report", "not_report")
# Estimate per uncached call, measured on Spark 1.3 on 2026-11-02 (reasoning tokens dominate):
# a facts call logged $0.0057 and a screening call $0.0046. The dry run sees only the facts
# calls (a screening call depends on their reply), so the run also stops at the spend cap.
COST_PER_CALL = 0.006


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("messages", type=Path, nargs="?", default=DEFAULT_MESSAGES)
    parser.add_argument("--llm", choices=list(LLM_BACKENDS))
    parser.add_argument("--yes", action="store_true", help="skip the paid-call confirmation")
    parser.add_argument("--record", type=Path, metavar="RESULTS", help="upsert into this file")
    parser.add_argument("--dataset", help=f"dataset id for --record (default {DEFAULT_DATASET})")
    spend.add_cap_argument(parser)
    return parser.parse_args(argv)


def load_messages(path: Path) -> list[dict[str, Any]]:
    path = path.expanduser().resolve()
    if path != DEFAULT_MESSAGES:
        outside_repo(path.parent)
    messages = json.loads(path.read_text("utf-8")).get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a non-empty JSON list")
    ids = set()
    for message in messages:
        if not isinstance(message, dict) or message.get("label") not in LABELS:
            raise ValueError(f"each message needs a label in {LABELS}")
        if not isinstance(message.get("id"), str) or message["id"] in ids:
            raise ValueError("each message needs a unique string id")
        if not isinstance(message.get("text"), str) or not message["text"].strip():
            raise ValueError(f"{message['id']}: text must be a non-empty string")
        ids.add(message["id"])
    return messages


async def evaluate_metrics(
    messages: list[dict[str, Any]], llm: AgentLLM, stop: Callable[[], bool] = lambda: False
) -> tuple[int, dict[str, Any]]:
    """Screen every message, print each miss and the summary; return the exit code and metrics.
    `stop` (the spend cap) ends the run early; the skipped messages count as failed."""
    labels, kept, miss_ids = [], [], []
    rule_filtered = model_calls = unsure = errors = 0
    for message in messages:
        if stop():
            errors += 1
            print(f"skipped {message['id']}: the spend cap was reached")
            continue
        report = message["label"] == "report"
        try:
            screening = await screen_text(llm, message["text"])
        except Exception as exc:  # the pipeline keeps a report whose facts call failed
            errors += 1
            print(f"error {message['id']}: {exc}")
            continue
        labels.append(report)
        kept.append(not screening.filtered)
        rule_filtered += screening.source == "rule" and screening.filtered
        model_calls += screening.source == "model"
        unsure += screening.verdict == "unsure"
        if kept[-1] != report:
            miss_ids.append(message["id"])
            what = "real report screened out" if report else "non-report kept"
            print(
                f"miss {message['id']} ({what}): {screening.source} {screening.verdict}, "
                f"{screening.reason}"
            )
    scores = binary_scores(labels, kept)
    print(f"\n{llm.model}: {len(labels)} messages scored, {errors} failed")
    print(f"Real reports: precision {scores.precision:.3f}, recall {scores.recall:.3f}")
    print(
        f"Real reports wrongly filtered: {scores.fn}. Non-reports filtered: {scores.tn} of "
        f"{scores.tn + scores.fp} ({rule_filtered} by the free rule). Screening calls: "
        f"{model_calls}, unsure: {unsure}"
    )
    metrics = {
        **eval_results.scores_dict(scores),
        "wrongly_filtered": scores.fn,
        "non_reports_filtered": scores.tn,
        "rule_filtered": rule_filtered,
        "model_calls": model_calls,
        "unsure": unsure,
        "miss_ids": miss_ids,
    }
    return (1 if errors else 0), metrics


async def estimate_cost(messages: list[dict[str, Any]], llm: AgentLLM) -> tuple[float, str]:
    """USD for the messages whose calls are not cached yet, from a free cache-only dry run."""
    if not spend.probe_ready(llm):
        uncached = len(messages)
    else:
        with spend.probe(llm):
            for message in messages:
                with contextlib.suppress(Exception):  # the client counts an uncached call
                    await screen_text(llm, message["text"])
        uncached = spend.misses(llm)
    return uncached * COST_PER_CALL, f"{uncached} uncached facts call(s)"


def dataset_for(args: argparse.Namespace) -> str | None:
    if not args.record:
        return None
    default = args.messages.expanduser().resolve() == DEFAULT_MESSAGES
    dataset = args.dataset or (DEFAULT_DATASET if default else None)
    if dataset is None:
        raise ValueError("--record with your own messages needs --dataset")
    if dataset not in eval_results.dataset_ids(args.record):
        raise ValueError(f"dataset {dataset!r} is not described in {args.record}")
    return dataset


async def run(args: argparse.Namespace) -> int:
    try:
        messages = load_messages(args.messages)
        dataset = dataset_for(args)
        settings = get_settings()
        if args.llm:
            settings = settings.model_copy(update={"agent_llm": args.llm})
        print(
            f"{len(messages)} messages, agent LLM {settings.agent_llm}. About "
            f"${2 * len(messages) * COST_PER_CALL:.2f} if nothing is cached; cached reruns are "
            "free."
        )
        if not args.yes and input("Spend credits on uncached calls? [y/N] ").strip().lower() != "y":
            return 1
        llm = make_agent_llm(settings)
        ledger = spend.SpendLedger(settings.data_dir, args.spend_cap, script="eval_screen")
        spend.attach(ledger, llm)
        try:
            estimate, detail = await estimate_cost(messages, llm)
            if not ledger.approve(estimate, detail):
                return 1
            code, metrics = await evaluate_metrics(messages, llm, ledger.over_cap)
        finally:
            await llm.aclose()
            ledger.close()
        print(f"logged spend for this run: ${ledger.run_spend:.4f}")
        if dataset is not None:
            if code:
                print("not recorded: some messages failed; re-run when the provider answers")
                return code
            entry = eval_results.upsert(
                args.record,
                {
                    "task": "screen",
                    "dataset": dataset,
                    "model": settings.agent_llm,
                    "model_id": llm.model,
                    "backend": eval_results.backend_of(llm),
                    "sam": None,
                    "date": eval_results.manila_today(),
                    "commit": eval_results.git_commit(),
                    "n": len(messages),
                    "errors": 0,
                    "spend_usd": ledger.run_spend,
                    "metrics": metrics,
                },
            )
            print(f"recorded screen/{dataset}/{entry['model']} in {args.record}")
        return code
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
