"""A hard spend cap for the paid eval and data scripts.

Every script that can spend credits records its model calls in one ledger,
``DATA_DIR/eval-spend.db`` (the ModelCall table, separate from the app's database so eval
spend never counts against the live bot's daily cap). Before a run, the script finds out which
calls are not cached yet (a free cache-only dry run, see ``probe``), prices them, prints the
estimate, logs it to ``DATA_DIR/eval-spend.log`` and refuses to start when the ledger total plus
the estimate would pass the cap. During a run, ``over_cap`` lets the script stop starting new
paid work once the logged spend reaches the cap.

Costs are list-price estimates from the clients (token usage or a flat price per request).
"""

import argparse
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, func
from sqlmodel import Session, SQLModel, create_engine, select

from app.db import make_recorder
from app.meta.base import CallRecord, ModelClient
from app.models import MANILA, ModelCall

LEDGER_NAME = "eval-spend.db"
LOG_NAME = "eval-spend.log"
DEFAULT_CAP_USD = 5.00


def add_cap_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--spend-cap",
        type=float,
        default=DEFAULT_CAP_USD,
        metavar="USD",
        help=f"refuse to run past this total logged spend (default {DEFAULT_CAP_USD:.2f})",
    )


def ledger_engine(data_dir: Path) -> Engine:
    data_dir.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{data_dir / LEDGER_NAME}")
    SQLModel.metadata.create_all(engine, tables=[ModelCall.__table__])  # type: ignore[attr-defined]
    return engine


class SpendLedger:
    """The logged spend in a data dir, the cap, and a recorder that keeps both current."""

    def __init__(self, data_dir: Path, cap_usd: float, *, script: str) -> None:
        self.data_dir, self.cap, self.script = data_dir, cap_usd, script
        self.engine = ledger_engine(data_dir)
        self._write = make_recorder(self.engine)
        self.run_spend = 0.0
        self.run_calls = 0
        self.run_uncached = 0

    def record(self, call: CallRecord) -> None:
        self._write(call)
        self.run_calls += 1
        if not call.cached:
            self.run_uncached += 1
            self.run_spend += call.cost_estimate

    def spent(self) -> float:
        with Session(self.engine) as session:
            return float(
                session.exec(select(func.coalesce(func.sum(ModelCall.cost_estimate), 0))).one()
            )

    def over_cap(self) -> bool:
        """True once this run has paid and the logged spend has passed the cap.

        The estimate already kept a run inside the cap; this catches an underestimate (longer
        replies, length retries) without stopping a run that is still answered from the cache.
        """
        return self.run_uncached > 0 and self.spent() > self.cap

    def approve(self, estimate: float, detail: str) -> bool:
        """Print and log the estimate; False if spend so far plus the estimate passes the cap."""
        spent = self.spent()
        ok = estimate <= 0 or spent + estimate <= self.cap + 1e-9  # a free run is always fine
        print(
            f"Estimated new spend: ${estimate:.2f} ({detail}). Logged so far: ${spent:.2f} "
            f"of the ${self.cap:.2f} cap."
        )
        if not ok:
            print(
                f"Refusing: ${spent:.2f} + ${estimate:.2f} would pass the ${self.cap:.2f} cap "
                "(raise --spend-cap only on purpose)."
            )
        self._log(
            {
                "estimate_usd": round(estimate, 4),
                "detail": detail,
                "spent_usd": round(spent, 4),
                "approved": ok,
            }
        )
        return ok

    def close(self) -> None:
        self._log(
            {
                "run_spend_usd": round(self.run_spend, 4),
                "calls": self.run_calls,
                "uncached_calls": self.run_uncached,
                "spent_usd": round(self.spent(), 4),
            }
        )
        self.engine.dispose()

    def _log(self, entry: dict[str, Any]) -> None:
        line = {"at": datetime.now(MANILA).isoformat(timespec="seconds"), "script": self.script}
        with (self.data_dir / LOG_NAME).open("a", encoding="utf-8") as f:
            f.write(json.dumps({**line, "cap_usd": self.cap, **entry}) + "\n")


def attach(ledger: SpendLedger, *clients: object) -> None:
    """Send real clients' call records to the ledger (test fakes have no recorder)."""
    for client in clients:
        if isinstance(client, ModelClient):
            client.recorder = ledger.record


@contextmanager
def probe(*clients: object) -> Iterator[None]:
    """Make real clients cache-only for a free dry run; their `uncached` lists the misses.

    Fakes are left alone: callers treat them as fully uncached (``probe_ready``).
    """
    real = [c for c in clients if isinstance(c, ModelClient)]
    for client in real:
        client.cache_only, client.uncached = True, []
    try:
        yield
    finally:
        for client in real:
            client.cache_only = False


def probe_ready(*clients: object) -> bool:
    """Only real clients can tell a cached reply from an uncached one."""
    return all(isinstance(c, ModelClient) for c in clients)


def misses(client: object) -> int:
    return len(getattr(client, "uncached", []))
