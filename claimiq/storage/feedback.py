"""Adjuster feedback capture.

Two purposes, and the second is the one that matters commercially:

  1. Measure real accuracy over time — a number, not a vibe. When an adjuster
     corrects a field or dismisses a finding, that is ground truth arriving for
     free during normal work.
  2. Feed few-shot examples back into extraction, so the system improves on the
     document layouts this insurer actually receives rather than on generic ones.

Stored in SQLite alongside the job queue. Deliberately append-only: a
correction is an event, and overwriting history would destroy the audit trail
that makes the accuracy measurement defensible.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path("data/feedback.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS field_corrections (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id     TEXT NOT NULL,
    doc_id       TEXT NOT NULL,
    field_name   TEXT NOT NULL,
    extracted    TEXT,
    corrected    TEXT,
    confidence   REAL,
    was_grounded INTEGER,
    adjuster     TEXT,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS finding_verdicts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id     TEXT NOT NULL,
    finding_title TEXT NOT NULL,
    kind         TEXT,
    severity     TEXT,
    source       TEXT,
    verdict      TEXT NOT NULL,   -- 'confirmed' | 'rejected' | 'unclear'
    note         TEXT,
    adjuster     TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fc_claim ON field_corrections(claim_id);
CREATE INDEX IF NOT EXISTS idx_fv_claim ON finding_verdicts(claim_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class FeedbackStore:
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
        return con

    # -- writes ------------------------------------------------------------
    def record_field_correction(
        self,
        claim_id: str,
        doc_id: str,
        field_name: str,
        extracted: object,
        corrected: object,
        confidence: float = 0.0,
        was_grounded: bool | None = None,
        adjuster: str = "demo",
    ) -> None:
        with self._lock, closing(self._connect()) as con:
            con.execute(
                "INSERT INTO field_corrections (claim_id, doc_id, field_name, "
                "extracted, corrected, confidence, was_grounded, adjuster, "
                "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    claim_id, doc_id, field_name,
                    json.dumps(extracted, default=str),
                    json.dumps(corrected, default=str),
                    confidence,
                    None if was_grounded is None else int(was_grounded),
                    adjuster, _now(),
                ),
            )
            con.commit()

    def record_finding_verdict(
        self,
        claim_id: str,
        finding_title: str,
        verdict: str,
        kind: str = "",
        severity: str = "",
        source: str = "",
        note: str = "",
        adjuster: str = "demo",
    ) -> None:
        if verdict not in ("confirmed", "rejected", "unclear"):
            raise ValueError(f"bad verdict {verdict!r}")
        with self._lock, closing(self._connect()) as con:
            con.execute(
                "INSERT INTO finding_verdicts (claim_id, finding_title, kind, "
                "severity, source, verdict, note, adjuster, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (claim_id, finding_title, kind, severity, source, verdict,
                 note, adjuster, _now()),
            )
            con.commit()

    # -- reads -------------------------------------------------------------
    def accuracy(self) -> dict:
        """Measured precision, from adjuster verdicts rather than self-report."""
        with closing(self._connect()) as con:
            verdicts = {
                r["verdict"]: r["n"]
                for r in con.execute(
                    "SELECT verdict, COUNT(*) n FROM finding_verdicts GROUP BY verdict"
                )
            }
            corrections = con.execute(
                "SELECT COUNT(*) n FROM field_corrections"
            ).fetchone()["n"]
            by_source = [
                dict(r) for r in con.execute(
                    "SELECT source, verdict, COUNT(*) n FROM finding_verdicts "
                    "GROUP BY source, verdict"
                )
            ]
            worst = [
                dict(r) for r in con.execute(
                    "SELECT field_name, COUNT(*) n FROM field_corrections "
                    "GROUP BY field_name ORDER BY n DESC LIMIT 5"
                )
            ]

        confirmed = verdicts.get("confirmed", 0)
        rejected = verdicts.get("rejected", 0)
        judged = confirmed + rejected
        return {
            "findings_judged": judged,
            "confirmed": confirmed,
            "rejected": rejected,
            "precision": round(confirmed / judged, 3) if judged else None,
            "field_corrections": corrections,
            "by_source": by_source,
            "most_corrected_fields": worst,
        }

    def few_shot_examples(self, field_name: str, limit: int = 3) -> list[dict]:
        """Past corrections for one field, for use as few-shot examples.

        The improvement loop: fields this insurer's document layouts get wrong
        become the examples that teach the next extraction.
        """
        with closing(self._connect()) as con:
            return [
                {
                    "field": r["field_name"],
                    "wrong": json.loads(r["extracted"]) if r["extracted"] else None,
                    "right": json.loads(r["corrected"]) if r["corrected"] else None,
                }
                for r in con.execute(
                    "SELECT field_name, extracted, corrected FROM field_corrections "
                    "WHERE field_name=? ORDER BY created_at DESC LIMIT ?",
                    (field_name, limit),
                )
            ]

    def corrections_for(self, claim_id: str) -> list[dict]:
        with closing(self._connect()) as con:
            return [
                dict(r) for r in con.execute(
                    "SELECT * FROM field_corrections WHERE claim_id=? "
                    "ORDER BY created_at DESC", (claim_id,)
                )
            ]
