"""Pre-flight for a live demo.

Run this the day before, not the morning of. It checks quota, processes every
claim so results load instantly from checkpoints, runs the evaluation, and
tells you plainly whether the demo is ready.

Usage:
    python prepare_demo.py            # check, process what is missing, verify
    python prepare_demo.py --check    # quota and readiness check only
    python prepare_demo.py --force    # reprocess everything from scratch
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

from claimiq.core.schemas import ClaimResult
from claimiq.evals.gold import EXPECTED_RECOMMENDATION
from claimiq.pipeline import RESULT_ROOT, process_claim

CLAIMS_ROOT = Path("claimiq/data/claims")
MODELS = ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]

GREEN, YELLOW, RED, RESET = "\033[92m", "\033[93m", "\033[91m", "\033[0m"


def ok(msg: str) -> None:
    print(f"  {GREEN}OK{RESET}    {msg}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}WARN{RESET}  {msg}")


def bad(msg: str) -> None:
    print(f"  {RED}FAIL{RESET}  {msg}")


def check_quota() -> dict[str, str]:
    """Probe each model's remaining daily budget with a trivial request."""
    print("\nProvider quota")
    status: dict[str, str] = {}

    if os.getenv("CLAIMIQ_PROVIDER", "groq").lower() == "mock":
        warn("CLAIMIQ_PROVIDER=mock — no real model will be called")
        return {m: "mock" for m in MODELS}

    try:
        from dotenv import load_dotenv
        from groq import Groq

        load_dotenv()
        key = os.getenv("GROQ_API_KEY")
        if not key:
            bad("GROQ_API_KEY not set (copy .env.example to .env)")
            return {m: "no-key" for m in MODELS}
        client = Groq(api_key=key)
    except Exception as e:  # noqa: BLE001
        bad(f"cannot reach provider: {e}")
        return {m: "error" for m in MODELS}

    for model in MODELS:
        try:
            client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "ok"}],
                max_completion_tokens=2000,
                stream=False,
            )
            ok(f"{model} — available")
            status[model] = "available"
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            m = re.search(r"Limit (\d+), Used (\d+)", msg)
            if "tokens per day" in msg.lower() and m:
                limit, used = int(m.group(1)), int(m.group(2))
                warn(f"{model} — daily quota spent ({used:,}/{limit:,})")
                status[model] = "exhausted"
            elif "rate" in msg.lower() or "429" in msg:
                warn(f"{model} — rate limited right now (recoverable)")
                status[model] = "throttled"
            else:
                bad(f"{model} — {msg[:90]}")
                status[model] = "error"
    return status


def check_results(force: bool) -> tuple[list[str], list[str]]:
    print("\nClaim results")
    if not CLAIMS_ROOT.exists():
        bad(f"{CLAIMS_ROOT} not found — run: python -m claimiq.data.generate")
        return [], []

    claims = sorted(p.name for p in CLAIMS_ROOT.iterdir() if p.is_dir())
    ready: list[str] = []
    missing: list[str] = []

    for cid in claims:
        path = RESULT_ROOT / f"{cid}.json"
        if force or not path.exists():
            missing.append(cid)
            warn(f"{cid} — not processed")
            continue
        try:
            r = ClaimResult.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            missing.append(cid)
            warn(f"{cid} — result unreadable, will reprocess")
            continue

        expected = EXPECTED_RECOMMENDATION.get(cid)
        degraded = bool(r.errors) or any(e.error for e in r.extractions)
        if degraded:
            missing.append(cid)
            warn(f"{cid} — processed but degraded (errors present), will redo")
        elif expected and r.recommendation.value != expected:
            ready.append(cid)
            warn(
                f"{cid} — routed {r.recommendation.value}, expected {expected}"
            )
        else:
            ready.append(cid)
            ok(
                f"{cid} — {r.recommendation.value}, {len(r.findings)} findings, "
                f"{r.usage.get('grounding_rate', 0):.0%} grounded"
            )
    return ready, missing


def main() -> int:
    force = "--force" in sys.argv
    check_only = "--check" in sys.argv

    print("=" * 62)
    print("ClaimIQ — demo preparation")
    print("=" * 62)

    quota = check_quota()
    ready, missing = check_results(force)

    if check_only:
        print("\n" + "=" * 62)
        print(f"{len(ready)} claim(s) ready, {len(missing)} need processing.")
        return 0 if not missing else 1

    if missing:
        usable = [m for m, s in quota.items() if s in ("available", "mock")]
        if not usable:
            print()
            bad("No model has budget — cannot process the missing claims.")
            print("\n  Options:")
            print("    - wait for the daily quota to reset")
            print("    - CLAIMIQ_PROVIDER=mock python prepare_demo.py  (offline demo)")
            print("    - upgrade the provider tier")
            return 1

        print(f"\nProcessing {len(missing)} claim(s)")
        for cid in missing:
            t0 = time.time()
            print(f"  {cid} … ", end="", flush=True)
            try:
                r = process_claim(CLAIMS_ROOT / cid, cid, resume=False)
                print(
                    f"{r.recommendation.value}, {len(r.findings)} findings, "
                    f"{time.time() - t0:.0f}s"
                )
            except Exception as e:  # noqa: BLE001
                print(f"{RED}failed{RESET}: {str(e)[:120]}")

        ready, missing = check_results(False)

    print("\n" + "=" * 62)
    if missing:
        print(f"{YELLOW}Demo NOT ready{RESET} — {len(missing)} claim(s) unprocessed.")
        return 1

    print(f"{GREEN}Demo ready{RESET} — {len(ready)} claim(s) will load instantly.")
    print("\n  streamlit run claimiq/ui/app.py")
    print("\n  Demo path: Review a claim → CLM-2024-1188 → Findings tab")
    return 0


if __name__ == "__main__":
    sys.exit(main())
