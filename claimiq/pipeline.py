"""Pipeline assembly and the single entry point used by CLI, batch and UI.

One code path for all three surfaces — a demo that runs different code from
production is a demo that proves nothing.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from claimiq.core.orchestrator import CheckpointStore, Context, Pipeline
from claimiq.core.schemas import ClaimResult
from claimiq.stages.classify import ClassifyStage
from claimiq.stages.extract import ExtractStage
from claimiq.stages.ingest import IngestStage
from claimiq.stages.reason import ReasonStage
from claimiq.stages.rules import RulesStage
from claimiq.stages.score import ScoreStage
from claimiq.stages.summarize import SummarizeStage
from claimiq.stages.verify import VerifyStage

CHECKPOINT_ROOT = Path("data/checkpoints")
RESULT_ROOT = Path("data/runs")


def build_pipeline(folder: Path, store: CheckpointStore) -> Pipeline:
    """Stage order matters:

    rules before reason  - deterministic findings are cheap and inform nothing
                           downstream, so they run first and are never blocked
                           by a model failure
    verify after reason  - it must check the reasoners' citations too
    score last           - it consumes everything above
    """
    return Pipeline(
        [
            IngestStage(folder),
            ClassifyStage(),
            ExtractStage(),
            RulesStage(),
            ReasonStage(),
            VerifyStage(),
            SummarizeStage(),
            ScoreStage(),
        ],
        store,
    )


def process_claim(
    folder: Path | str,
    claim_id: str | None = None,
    *,
    resume: bool = True,
    progress: Callable[[str, str], None] | None = None,
    checkpoint_root: Path = CHECKPOINT_ROOT,
    write_audit: bool = True,
) -> ClaimResult:
    """Run one claim folder through the full pipeline."""
    folder = Path(folder)
    claim_id = claim_id or folder.name

    store = CheckpointStore(checkpoint_root)
    ctx = Context(
        claim_id=claim_id,
        result=ClaimResult(claim_id=claim_id),
        workdir=RESULT_ROOT,
    )

    t0 = time.time()
    pipeline = build_pipeline(folder, store)
    result = pipeline.run(ctx, resume=resume, progress=progress)
    result.stage_timings["_wall"] = round(time.time() - t0, 2)

    if write_audit:
        pipeline.write_audit(ctx)

    RESULT_ROOT.mkdir(parents=True, exist_ok=True)
    (RESULT_ROOT / f"{claim_id}.json").write_text(
        result.model_dump_json(indent=2), encoding="utf-8"
    )
    return result
