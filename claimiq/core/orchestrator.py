"""Stage runner with checkpointing, resume and audit trail.

Deliberately hand-written rather than LangChain/LCEL. The reasoning, for the
architecture review:

  * Checkpoint/resume requires control of every stage boundary; LCEL's value
    proposition is hiding those boundaries.
  * The audit trail must record the exact prompt and raw response sent to the
    model. Through a framework you are reconstructing what it probably sent.
  * Groq is OpenAI-compatible — the provider layer is ~40 lines. A dependency
    to abstract a call we write once is negative value.

What we do use from LangChain: langchain-text-splitters, for chunking. Boring,
well-tested, no orchestration entanglement.
"""
from __future__ import annotations

import json
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from claimiq.core.schemas import ClaimResult


class Stage(Protocol):
    name: str

    def run(self, ctx: "Context") -> None: ...


@dataclass
class AuditEntry:
    stage: str
    kind: str            # "model_call" | "rule" | "event"
    detail: dict[str, Any]
    ts: float = field(default_factory=time.time)


@dataclass
class Context:
    """Carried through every stage. The single mutable object in the pipeline."""

    claim_id: str
    result: ClaimResult
    workdir: Path
    config: dict[str, Any] = field(default_factory=dict)
    audit: list[AuditEntry] = field(default_factory=list)
    _progress: Callable[[str, str], None] | None = None

    def log(self, stage: str, kind: str, **detail: Any) -> None:
        self.audit.append(AuditEntry(stage=stage, kind=kind, detail=detail))

    def progress(self, stage: str, message: str) -> None:
        if self._progress:
            self._progress(stage, message)

    def set_progress_hook(self, fn: Callable[[str, str], None] | None) -> None:
        self._progress = fn


class CheckpointStore:
    """Filesystem checkpoints, one JSON file per (claim, stage).

    Filesystem rather than a DB because a checkpoint is a whole-object snapshot
    keyed by two strings — a directory is the right shape for that, and it is
    trivially inspectable when debugging a bad run.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, claim_id: str, stage: str) -> Path:
        d = self.root / claim_id
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{stage}.json"

    def save(self, claim_id: str, stage: str, result: ClaimResult) -> None:
        tmp = self._path(claim_id, stage).with_suffix(".tmp")
        tmp.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(self._path(claim_id, stage))  # atomic

    def load(self, claim_id: str, stage: str) -> ClaimResult | None:
        p = self._path(claim_id, stage)
        if not p.exists():
            return None
        try:
            return ClaimResult.model_validate_json(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - corrupt checkpoint => recompute
            return None

    def completed_stages(self, claim_id: str) -> set[str]:
        d = self.root / claim_id
        if not d.exists():
            return set()
        return {p.stem for p in d.glob("*.json")}

    def clear(self, claim_id: str) -> None:
        d = self.root / claim_id
        if d.exists():
            for p in d.glob("*.json"):
                p.unlink()


class Pipeline:
    """Ordered stages with checkpoint-based resume."""

    def __init__(
        self,
        stages: list[Stage],
        store: CheckpointStore,
        *,
        fail_fast: bool = False,
    ) -> None:
        self.stages = stages
        self.store = store
        self.fail_fast = fail_fast

    def run(
        self,
        ctx: Context,
        *,
        resume: bool = True,
        progress: Callable[[str, str], None] | None = None,
    ) -> ClaimResult:
        if progress:
            ctx.set_progress_hook(progress)

        done = self.store.completed_stages(ctx.claim_id) if resume else set()
        if not resume:
            self.store.clear(ctx.claim_id)

        for stage in self.stages:
            if stage.name in done:
                cached = self.store.load(ctx.claim_id, stage.name)
                if cached is not None:
                    ctx.result = cached
                    ctx.progress(stage.name, "restored from checkpoint")
                    ctx.log(stage.name, "event", resumed=True)
                    continue

            ctx.progress(stage.name, "running")
            t0 = time.time()
            try:
                stage.run(ctx)
                ctx.result.stage_timings[stage.name] = round(time.time() - t0, 2)
                self.store.save(ctx.claim_id, stage.name, ctx.result)
                ctx.progress(stage.name, "done")
            except Exception as e:  # noqa: BLE001 - one bad stage != dead claim
                ctx.result.stage_timings[stage.name] = round(time.time() - t0, 2)
                msg = f"{stage.name}: {type(e).__name__}: {e}"
                ctx.result.errors.append(msg)
                ctx.log(
                    stage.name, "event", error=msg, trace=traceback.format_exc()[:2000]
                )
                ctx.progress(stage.name, f"failed: {e}")
                if self.fail_fast:
                    raise

        return ctx.result

    def write_audit(self, ctx: Context) -> Path:
        """Persist the audit trail. This is the regulator-facing artefact."""
        p = self.store.root / ctx.claim_id / "audit.jsonl"
        with p.open("w", encoding="utf-8") as fh:
            for e in ctx.audit:
                fh.write(
                    json.dumps(
                        {
                            "ts": e.ts,
                            "stage": e.stage,
                            "kind": e.kind,
                            **e.detail,
                        },
                        default=str,
                    )
                    + "\n"
                )
        return p
