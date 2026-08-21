"""Batch runner with a durable queue and resume.

The operational requirement: a 500-claim overnight run must survive a dead
endpoint, a rate-limit storm and a process restart without losing completed
work or reprocessing it. State lives in SQLite so a restart picks up exactly
where it stopped, and per-claim stage checkpoints mean even a partially
processed claim resumes mid-pipeline rather than from the beginning.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from claimiq.core.schemas import ClaimResult, Recommendation, Severity
from claimiq.pipeline import CHECKPOINT_ROOT, process_claim

DB_PATH = Path("data/batch.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    claim_id     TEXT PRIMARY KEY,
    folder       TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    attempts     INTEGER NOT NULL DEFAULT 0,
    queued_at    TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT,
    wall_s       REAL,
    recommendation TEXT,
    risk         REAL,
    findings     INTEGER,
    critical     INTEGER,
    billed       REAL,
    exposure     REAL,
    cost_usd     REAL,
    grounding    REAL,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_status ON jobs(status);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobQueue:
    """SQLite-backed queue. Chosen over Redis/Celery deliberately for a POC:

    zero infrastructure, single file, trivially inspectable with any SQL client,
    and it survives a restart. The interface is narrow enough that swapping in
    Postgres for production is a connection-string change.
    """

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with closing(self._connect()) as con:
            con.executescript(_SCHEMA)
            con.commit()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=30.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")  # concurrent readers during a run
        return con

    def enqueue(self, folders: Iterable[Path]) -> int:
        added = 0
        with self._lock, closing(self._connect()) as con:
            for folder in folders:
                folder = Path(folder)
                try:
                    con.execute(
                        "INSERT INTO jobs (claim_id, folder, queued_at) VALUES (?,?,?)",
                        (folder.name, str(folder.resolve()), _now()),
                    )
                    added += 1
                except sqlite3.IntegrityError:
                    pass  # already queued; resume semantics
            con.commit()
        return added

    def claim_next(self) -> tuple[str, str] | None:
        """Atomically take the next pending job."""
        with self._lock, closing(self._connect()) as con:
            row = con.execute(
                "SELECT claim_id, folder FROM jobs WHERE status='pending' "
                "ORDER BY queued_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            con.execute(
                "UPDATE jobs SET status='running', started_at=?, "
                "attempts=attempts+1 WHERE claim_id=?",
                (_now(), row["claim_id"]),
            )
            con.commit()
            return row["claim_id"], row["folder"]

    def complete(self, claim_id: str, result: ClaimResult, wall_s: float) -> None:
        crit = sum(1 for f in result.findings if f.severity == Severity.CRITICAL)
        with self._lock, closing(self._connect()) as con:
            con.execute(
                "UPDATE jobs SET status='done', finished_at=?, wall_s=?, "
                "recommendation=?, risk=?, findings=?, critical=?, billed=?, "
                "exposure=?, cost_usd=?, grounding=?, error=NULL WHERE claim_id=?",
                (
                    _now(), round(wall_s, 2), result.recommendation.value,
                    result.risk_score, len(result.findings), crit,
                    result.total_billed, result.exposure,
                    result.usage.get("cost_usd"), result.usage.get("grounding_rate"),
                    claim_id,
                ),
            )
            con.commit()

    def fail(self, claim_id: str, error: str, max_attempts: int = 3) -> None:
        """Return to the queue for another attempt, or park it as failed."""
        with self._lock, closing(self._connect()) as con:
            row = con.execute(
                "SELECT attempts FROM jobs WHERE claim_id=?", (claim_id,)
            ).fetchone()
            attempts = row["attempts"] if row else max_attempts
            status = "pending" if attempts < max_attempts else "failed"
            con.execute(
                "UPDATE jobs SET status=?, error=?, finished_at=? WHERE claim_id=?",
                (status, error[:500], _now(), claim_id),
            )
            con.commit()

    def stats(self) -> dict:
        with closing(self._connect()) as con:
            counts = {
                r["status"]: r["n"]
                for r in con.execute(
                    "SELECT status, COUNT(*) n FROM jobs GROUP BY status"
                )
            }
            agg = con.execute(
                "SELECT COUNT(*) n, AVG(wall_s) avg_wall, SUM(cost_usd) cost, "
                "SUM(billed) billed, SUM(exposure) exposure, AVG(risk) risk, "
                "AVG(grounding) grounding FROM jobs WHERE status='done'"
            ).fetchone()
            recs = {
                r["recommendation"]: r["n"]
                for r in con.execute(
                    "SELECT recommendation, COUNT(*) n FROM jobs "
                    "WHERE status='done' GROUP BY recommendation"
                )
            }
        done = agg["n"] or 0
        auto = recs.get(Recommendation.AUTO_APPROVE.value, 0)
        return {
            "counts": counts,
            "processed": done,
            "avg_wall_s": round(agg["avg_wall"] or 0, 1),
            "total_cost_usd": round(agg["cost"] or 0, 4),
            "cost_per_claim": round((agg["cost"] or 0) / done, 5) if done else 0,
            "total_billed": round(agg["billed"] or 0, 2),
            "total_exposure": round(agg["exposure"] or 0, 2),
            "avg_risk": round(agg["risk"] or 0, 3),
            "avg_grounding": round(agg["grounding"] or 0, 4),
            "recommendations": recs,
            "stp_rate": round(auto / done, 4) if done else 0.0,
        }

    def rows(self) -> list[dict]:
        with closing(self._connect()) as con:
            return [dict(r) for r in con.execute(
                "SELECT * FROM jobs ORDER BY "
                "CASE status WHEN 'running' THEN 0 WHEN 'pending' THEN 1 "
                "WHEN 'failed' THEN 2 ELSE 3 END, finished_at DESC"
            )]

    def reset_running(self) -> int:
        """Recover jobs orphaned by a crash. Called on startup."""
        with self._lock, closing(self._connect()) as con:
            cur = con.execute(
                "UPDATE jobs SET status='pending' WHERE status='running'"
            )
            con.commit()
            return cur.rowcount


@dataclass
class BatchReport:
    processed: int
    failed: int
    wall_s: float
    stats: dict


def run_batch(
    root: Path | str,
    *,
    workers: int = 1,
    db_path: Path = DB_PATH,
    on_event: Callable[[str, str], None] | None = None,
    resume: bool = True,
) -> BatchReport:
    """Process every claim folder under `root`.

    Concurrency defaults to 1: on a TPM-constrained tier, parallel claims only
    contend for the same token window. Raise it when CLAIMIQ_TPM allows.
    """
    root = Path(root)
    folders = sorted(p for p in root.iterdir() if p.is_dir())

    q = JobQueue(db_path)
    orphaned = q.reset_running()
    if orphaned and on_event:
        on_event("recover", f"requeued {orphaned} interrupted job(s)")

    added = q.enqueue(folders)
    if on_event:
        on_event("queue", f"{added} new, {len(folders)} total under {root}")

    processed = failed = 0
    t0 = time.time()

    def worker() -> tuple[int, int]:
        ok = bad = 0
        while True:
            job = q.claim_next()
            if job is None:
                return ok, bad
            claim_id, folder = job
            if on_event:
                on_event("start", claim_id)
            c0 = time.time()
            try:
                result = process_claim(
                    Path(folder), claim_id, resume=resume,
                    checkpoint_root=CHECKPOINT_ROOT,
                )
                q.complete(claim_id, result, time.time() - c0)
                ok += 1
                if on_event:
                    on_event(
                        "done",
                        f"{claim_id}: {result.recommendation.value} "
                        f"({len(result.findings)} findings, {time.time()-c0:.0f}s)",
                    )
            except Exception as e:  # noqa: BLE001 - a bad claim must not stop the batch
                q.fail(claim_id, f"{type(e).__name__}: {e}")
                bad += 1
                if on_event:
                    on_event("fail", f"{claim_id}: {e}")
        return ok, bad

    if workers <= 1:
        processed, failed = worker()
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(worker) for _ in range(workers)]
            for f in as_completed(futures):
                ok, bad = f.result()
                processed += ok
                failed += bad

    return BatchReport(
        processed=processed,
        failed=failed,
        wall_s=round(time.time() - t0, 1),
        stats=q.stats(),
    )


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="ClaimIQ batch runner")
    ap.add_argument("root", nargs="?", default="claimiq/data/claims")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--fresh", action="store_true", help="ignore checkpoints")
    args = ap.parse_args()

    report = run_batch(
        args.root,
        workers=args.workers,
        resume=not args.fresh,
        on_event=lambda kind, msg: print(f"[{kind:7}] {msg}", flush=True),
    )
    print("\n" + json.dumps(report.stats, indent=2))
    print(f"\nprocessed={report.processed} failed={report.failed} wall={report.wall_s}s")
