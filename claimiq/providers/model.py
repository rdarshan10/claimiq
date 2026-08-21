"""THE MODEL SEAM.

Every model call in ClaimIQ funnels through `invoke()` in this file. No other
module imports a provider SDK. To swap Groq for your own endpoint, implement a
Provider subclass and register it in PROVIDERS — nothing else changes.

Design notes (for the architecture review):
  * Task-based routing. Stages ask for a TaskProfile, not a model name, so
    cost/capability tuning happens here rather than being scattered through
    prompts.
  * Every call returns a ModelResponse carrying token counts, latency and cost.
    That is what makes the per-claim cost figure real rather than estimated.
  * Retry with exponential backoff + jitter, and a circuit breaker, live here
    so that no stage has to think about transport failure.
"""
from __future__ import annotations

import os
import random
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from dotenv import load_dotenv

load_dotenv()


# --------------------------------------------------------------------------
# Task profiles -> model routing
# --------------------------------------------------------------------------
class Task(str, Enum):
    """What a stage is trying to do. Stages request these, never model IDs."""

    CLASSIFY = "classify"
    EXTRACT = "extract"
    REASON = "reason"
    SUMMARIZE = "summarize"
    VERIFY = "verify"
    CHEAP = "cheap"


@dataclass(frozen=True)
class TaskProfile:
    model: str
    temperature: float
    max_tokens: int
    reasoning_effort: str | None = None
    json_mode: bool = False


# Routing table. 60%+ of calls land on the small model by design — this is the
# cost-engineering story, and it is one line to retune.
ROUTING: dict[Task, TaskProfile] = {
    Task.CLASSIFY: TaskProfile("openai/gpt-oss-20b", 0.0, 512, "low", True),
    # max_tokens on a reasoning model must cover BOTH the reasoning trace and
    # the answer. Under response_format=json_object a truncated answer is not
    # returned as partial text — the API rejects it with a 400
    # (json_validate_failed), so too small a budget fails 100% of the time.
    #
    # Budgets are sized to observed output, not to headroom. Under a tight TPM
    # window an oversized max_tokens is not free: it inflates the reservation,
    # so calls queue for a full window and throughput collapses. A per-document
    # extraction of a 1-2k char document emits ~700-1200 tokens.
    Task.EXTRACT: TaskProfile("openai/gpt-oss-120b", 0.0, 2000, "low", True),
    Task.REASON: TaskProfile("openai/gpt-oss-120b", 0.0, 4500, "medium", True),
    Task.SUMMARIZE: TaskProfile("openai/gpt-oss-120b", 0.3, 2048, "low", False),
    Task.VERIFY: TaskProfile("openai/gpt-oss-20b", 0.0, 1024, "low", True),
    Task.CHEAP: TaskProfile("openai/gpt-oss-20b", 0.0, 1024, "low", False),
}
# qwen/qwen3.6-27b was evaluated and dropped: it rejects response_format
# json_object (HTTP 400) and emits <think> blocks that consumed the full token
# budget before producing an answer. Two predictable models beat three.

# $ per 1M tokens. Approximate Groq public pricing — adjust to your contract.
# Used for the live cost panel; wrong numbers here make the ROI slide wrong.
PRICING: dict[str, tuple[float, float]] = {
    "openai/gpt-oss-120b": (0.15, 0.60),
    "openai/gpt-oss-20b": (0.05, 0.20),
    "qwen/qwen3.6-27b": (0.10, 0.40),
}


@dataclass
class ModelResponse:
    text: str
    model: str
    in_tokens: int
    out_tokens: int
    latency_s: float
    cost_usd: float
    attempts: int = 1
    raw: Any = None

    @property
    def tokens(self) -> int:
        return self.in_tokens + self.out_tokens


def _cost(model: str, in_tok: int, out_tok: int) -> float:
    cin, cout = PRICING.get(model, (0.0, 0.0))
    return (in_tok / 1_000_000) * cin + (out_tok / 1_000_000) * cout


# --------------------------------------------------------------------------
# Usage accounting — thread-safe, because extraction fans out across documents
# --------------------------------------------------------------------------
@dataclass
class UsageLedger:
    calls: int = 0
    in_tokens: int = 0
    out_tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0
    by_model: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, r: ModelResponse) -> None:
        with self._lock:
            self.calls += 1
            self.in_tokens += r.in_tokens
            self.out_tokens += r.out_tokens
            self.cost_usd += r.cost_usd
            self.latency_s += r.latency_s
            self.by_model[r.model] = self.by_model.get(r.model, 0) + 1

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "calls": self.calls,
                "in_tokens": self.in_tokens,
                "out_tokens": self.out_tokens,
                "total_tokens": self.in_tokens + self.out_tokens,
                "cost_usd": round(self.cost_usd, 6),
                "model_seconds": round(self.latency_s, 2),
                "by_model": dict(self.by_model),
            }

    def reset(self) -> None:
        with self._lock:
            self.calls = self.in_tokens = self.out_tokens = 0
            self.cost_usd = self.latency_s = 0.0
            self.by_model = {}


LEDGER = UsageLedger()


# --------------------------------------------------------------------------
# Circuit breaker — stops a dead endpoint from burning a 500-claim batch
# --------------------------------------------------------------------------
class CircuitOpen(RuntimeError):
    pass


class QuotaExhausted(RuntimeError):
    """A per-day quota is spent. Unlike a rate limit, waiting will not help."""


class CircuitBreaker:
    def __init__(self, threshold: int = 8, cooldown_s: float = 30.0) -> None:
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self._fails = 0
        self._opened_at = 0.0
        self._lock = threading.Lock()

    def check(self) -> None:
        with self._lock:
            if self._fails < self.threshold:
                return
            if time.time() - self._opened_at < self.cooldown_s:
                raise CircuitOpen(
                    f"Circuit open after {self._fails} consecutive failures; "
                    f"retrying in {self.cooldown_s - (time.time() - self._opened_at):.0f}s"
                )
            self._fails = 0  # half-open: let one through

    def ok(self) -> None:
        with self._lock:
            self._fails = 0

    def fail(self) -> None:
        with self._lock:
            self._fails += 1
            if self._fails >= self.threshold:
                self._opened_at = time.time()


BREAKER = CircuitBreaker()


# --------------------------------------------------------------------------
# Provider interface
# --------------------------------------------------------------------------
class Provider(Protocol):
    def complete(
        self, system: str | None, prompt: str, profile: TaskProfile
    ) -> ModelResponse: ...


class GroqProvider:
    """Default provider. OpenAI-compatible chat completions."""

    def __init__(self) -> None:
        from groq import Groq  # imported lazily so mock runs without the SDK

        key = os.getenv("GROQ_API_KEY")
        if not key:
            raise RuntimeError("GROQ_API_KEY not set — copy .env.example to .env")
        self._client = Groq(api_key=key)

    def complete(
        self, system: str | None, prompt: str, profile: TaskProfile
    ) -> ModelResponse:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        kwargs: dict[str, Any] = dict(
            model=profile.model,
            messages=messages,
            temperature=profile.temperature,
            max_completion_tokens=profile.max_tokens,
            stream=False,
        )
        if profile.reasoning_effort:
            kwargs["reasoning_effort"] = profile.reasoning_effort
        if profile.json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        t0 = time.time()
        r = self._client.chat.completions.create(**kwargs)
        elapsed = time.time() - t0

        in_tok = r.usage.prompt_tokens
        out_tok = r.usage.completion_tokens
        return ModelResponse(
            text=r.choices[0].message.content or "",
            model=profile.model,
            in_tokens=in_tok,
            out_tokens=out_tok,
            latency_s=elapsed,
            cost_usd=_cost(profile.model, in_tok, out_tok),
            raw=r,
        )


class MockProvider:
    """Deterministic stand-in so the pipeline runs with no credentials.

    Used by the test suite and for offline demos. Returns structurally valid
    but obviously fake payloads.
    """

    def complete(
        self, system: str | None, prompt: str, profile: TaskProfile
    ) -> ModelResponse:
        time.sleep(0.01)
        text = '{"_mock": true, "note": "MockProvider — no model was called"}'
        if not profile.json_mode:
            text = "[mock summary] No model was called."
        return ModelResponse(
            text=text,
            model=f"mock:{profile.model}",
            in_tokens=len(prompt) // 4,
            out_tokens=len(text) // 4,
            latency_s=0.01,
            cost_usd=0.0,
        )


PROVIDERS: dict[str, type] = {"groq": GroqProvider, "mock": MockProvider}

_provider: Provider | None = None
_provider_lock = threading.Lock()


def get_provider() -> Provider:
    global _provider
    with _provider_lock:
        if _provider is None:
            name = os.getenv("CLAIMIQ_PROVIDER", "groq").lower()
            if name not in PROVIDERS:
                raise RuntimeError(f"Unknown provider {name!r}")
            _provider = PROVIDERS[name]()
        return _provider


def set_provider(p: Provider) -> None:
    """Override the provider — used by tests and by the offline demo mode."""
    global _provider
    with _provider_lock:
        _provider = p


# --------------------------------------------------------------------------
# The single entry point
# --------------------------------------------------------------------------
RETRYABLE = ("rate", "timeout", "429", "500", "502", "503", "504", "overload")


class RateLimiter:
    """Token-per-minute governor, shared across threads.

    Groq's on-demand tier enforces TPM, not just RPM, and it counts input +
    output together. Measured on this account: 8,000 TPM for gpt-oss-120b.
    That is a hard architectural constraint, not a tuning knob — a single
    request larger than the budget can never succeed, and concurrent fan-out
    starves itself without a governor.

    We reserve tokens before each call and release the window as it slides.
    Set CLAIMIQ_TPM to match your tier (Dev tier is far higher).
    """

    def __init__(self, tpm: int, rpm: int, headroom: float = 0.88) -> None:
        self.tpm = max(1000, tpm)
        # Provider-side accounting is not identical to ours (tokenisation
        # differs, and the window edges do not align). Under-reserving costs a
        # 429 and a full 60s recovery, so hold back a margin deliberately.
        self.budget = int(self.tpm * headroom)
        self.min_interval = 60.0 / max(1, rpm)
        self._spent: list[tuple[float, int]] = []  # (timestamp, tokens)
        self._next_call = 0.0
        self._lock = threading.Lock()

    def _prune(self, now: float) -> int:
        self._spent = [(t, n) for t, n in self._spent if now - t < 60.0]
        return sum(n for _, n in self._spent)

    def acquire(self, estimated_tokens: int) -> None:
        """Block until `estimated_tokens` fit inside the sliding window."""
        deadline = time.monotonic() + 180.0
        while True:
            with self._lock:
                now = time.monotonic()
                used = self._prune(now)
                gap = self._next_call - now

                if used + estimated_tokens <= self.budget and gap <= 0:
                    self._spent.append((now, estimated_tokens))
                    self._next_call = now + self.min_interval
                    return

                if used + estimated_tokens > self.budget and self._spent:
                    oldest = self._spent[0][0]
                    wait = max(gap, 60.0 - (now - oldest) + 0.25)
                else:
                    wait = max(gap, 0.05)

            if time.monotonic() > deadline:
                return  # fail open; the retry path handles a real 429
            time.sleep(min(wait, 5.0))

    def record_actual(self, estimated: int, actual: int) -> None:
        """Reconcile the reservation against real usage, in both directions.

        Reservations are deliberately pessimistic (the output budget is a
        ceiling, not a prediction — a call budgeted 3500 output tokens often
        uses 400). Releasing the unused remainder matters: without it the
        window stays fictionally full and throughput collapses to a fraction
        of the tier's real capacity.
        """
        delta = actual - estimated
        with self._lock:
            if delta > 0:
                self._spent.append((time.monotonic(), delta))
            elif delta < 0:
                # Refund against the newest reservations first.
                refund = -delta
                for i in range(len(self._spent) - 1, -1, -1):
                    if refund <= 0:
                        break
                    ts, n = self._spent[i]
                    give = min(n, refund)
                    self._spent[i] = (ts, n - give)
                    refund -= give
                self._spent = [(t, n) for t, n in self._spent if n > 0]


LIMITER = RateLimiter(
    tpm=int(os.getenv("CLAIMIQ_TPM", "8000")),
    rpm=int(os.getenv("CLAIMIQ_RPM", "28")),
)


# Reserving the full output ceiling wastes most of the window, since
# max_tokens is a cap rather than a forecast. These are measured ratios of
# actual output tokens to the configured ceiling, per task; record_actual()
# corrects any under-estimate immediately after the call.
_OUTPUT_RATIO = {
    Task.CLASSIFY: 0.6,
    Task.EXTRACT: 0.85,  # structured output routinely fills most of its budget
    Task.REASON: 0.9,    # reasoning traces genuinely use nearly all of it
    Task.SUMMARIZE: 0.8,
    Task.VERIFY: 0.5,
    Task.CHEAP: 0.5,
}


def estimate_tokens(
    prompt: str, system: str | None, profile: TaskProfile, task: Task | None = None
) -> int:
    """Pre-flight reservation: exact-ish input plus a realistic output share."""
    chars = len(prompt) + len(system or "")
    in_tok = int(chars / 3.6)
    ratio = _OUTPUT_RATIO.get(task, 0.6) if task else 0.6
    return in_tok + int(profile.max_tokens * ratio)


# When a model's daily quota is spent, fall back to one that still has budget.
# Quality drops and we say so — but a partially-degraded claim beats a dead
# batch, and each model carries its own separate quota.
FALLBACK_MODEL: dict[str, str] = {
    "openai/gpt-oss-120b": "openai/gpt-oss-20b",
}

_exhausted: set[str] = set()
_exhausted_lock = threading.Lock()


def invoke(
    prompt: str,
    task: Task = Task.CHEAP,
    system: str | None = None,
    max_retries: int = 4,
    profile_override: TaskProfile | None = None,
    allow_fallback: bool = True,
) -> ModelResponse:
    """Call a model. This is the only function that talks to a provider.

    Retries transient failures with exponential backoff + jitter, trips a
    circuit breaker on sustained failure, and records usage to LEDGER.
    """
    profile = profile_override or ROUTING[task]
    provider = get_provider()
    last_err: Exception | None = None

    # Skip a model already known to be out of daily budget.
    with _exhausted_lock:
        spent = profile.model in _exhausted
    if spent and allow_fallback and profile.model in FALLBACK_MODEL:
        profile = TaskProfile(
            FALLBACK_MODEL[profile.model], profile.temperature,
            min(profile.max_tokens, 2048), profile.reasoning_effort,
            profile.json_mode,
        )

    # A request larger than the whole TPM window can never succeed. Trim the
    # output budget to what actually fits rather than 413-ing on every attempt.
    in_est = int((len(prompt) + len(system or "")) / 3.5)
    ceiling = LIMITER.tpm - in_est - 200
    if ceiling < profile.max_tokens:
        if ceiling < 512:
            raise RuntimeError(
                f"Prompt of ~{in_est} tokens leaves no room under a "
                f"{LIMITER.tpm} TPM limit. Reduce evidence size or raise "
                f"CLAIMIQ_TPM to match your tier."
            )
        profile = TaskProfile(
            profile.model, profile.temperature, ceiling,
            profile.reasoning_effort, profile.json_mode,
        )

    # The rate governor models a remote provider's quota. A local provider
    # (mock, or a self-hosted endpoint) has no such quota, and throttling it
    # turns an instant offline run into minutes of pointless sleeping.
    paced = not isinstance(provider, MockProvider)

    for attempt in range(1, max_retries + 1):
        BREAKER.check()
        est = estimate_tokens(prompt, system, profile, task)
        if paced:
            LIMITER.acquire(est)
        try:
            r = provider.complete(system, prompt, profile)
            if paced:
                LIMITER.record_actual(est, r.in_tokens + r.out_tokens)
            r.attempts = attempt
            BREAKER.ok()
            LEDGER.record(r)
            return r
        except CircuitOpen:
            raise
        except Exception as e:  # noqa: BLE001 - provider SDKs raise broadly
            last_err = e
            msg = str(e).lower()

            # Truncated-JSON 400s are a budget problem, not a transport problem:
            # retrying the same request cannot succeed. Retry once with room to
            # finish, then surface it — silently returning no findings would be
            # the worst outcome for a validation system.
            if "json_validate_failed" in msg or "failed to generate json" in msg:
                if attempt < max_retries and profile.max_tokens < 16000:
                    profile = TaskProfile(
                        profile.model, profile.temperature,
                        min(16000, profile.max_tokens * 2),
                        profile.reasoning_effort, profile.json_mode,
                    )
                    continue
                BREAKER.ok()  # endpoint is healthy; this request was malformed
                raise RuntimeError(
                    f"Model could not produce valid JSON within "
                    f"{profile.max_tokens} tokens (reasoning_effort="
                    f"{profile.reasoning_effort}). Raise max_tokens for this task."
                ) from e

            # Rate limiting is backpressure from a healthy endpoint, not a
            # fault. Counting 429/413 toward the breaker made a constrained
            # tier look like an outage: the breaker tripped mid-batch and every
            # remaining extraction failed while the pipeline still reported
            # success. Back off on these, but never open the circuit.
            throttled = any(t in msg for t in ("429", "rate", "413", "too large"))

            # A per-DAY quota is not backpressure — waiting will not clear it,
            # and retrying burns the remainder. Fail immediately with the real
            # cause, and let the caller fall back to a model that still has
            # budget. (Observed: gpt-oss-120b TPD 200000 exhausted mid-batch,
            # which retry logic alone would surface as an unexplained stall.)
            if "tokens per day" in msg or "tpd" in msg:
                BREAKER.ok()
                with _exhausted_lock:
                    _exhausted.add(profile.model)

                fallback = FALLBACK_MODEL.get(profile.model)
                if allow_fallback and fallback and fallback not in _exhausted:
                    profile = TaskProfile(
                        fallback, profile.temperature,
                        min(profile.max_tokens, 2048),
                        profile.reasoning_effort, profile.json_mode,
                    )
                    continue

                raise QuotaExhausted(
                    f"Daily token quota exhausted for {profile.model}"
                    + (f" and fallback {fallback}" if fallback else "")
                    + ". Use CLAIMIQ_PROVIDER=mock, or wait for the quota to "
                    f"reset. Original: {str(e)[:200]}"
                ) from e

            if not any(t in msg for t in RETRYABLE) or attempt == max_retries:
                if not throttled:
                    BREAKER.fail()
                raise
            if not throttled:
                BREAKER.fail()

            delay = min(2 ** attempt * 0.5, 8.0) + random.uniform(0, 0.4)
            if throttled:
                delay = max(delay, 8.0)  # a TPM window needs real time to clear
            time.sleep(delay)

    raise RuntimeError(f"invoke failed after {max_retries} attempts: {last_err}")
