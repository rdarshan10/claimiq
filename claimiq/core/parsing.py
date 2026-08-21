"""Defensive parsing of model output.

Groq's json_object mode is reliable on the gpt-oss models (verified by probe),
but "reliable" is not "guaranteed", and a single malformed response must not
kill a 500-claim batch. Everything here is best-effort recovery that fails to
a typed error rather than an exception.
"""
from __future__ import annotations

import json
import re
from typing import Any

# Models occasionally wrap JSON in fences or emit <think> preambles.
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _balanced_span(text: str) -> str | None:
    """Return the first balanced {...} or [...] block, respecting strings."""
    start = None
    opener = closer = ""
    for i, ch in enumerate(text):
        if ch in "{[":
            start = i
            opener, closer = ch, "}" if ch == "{" else "]"
            break
    if start is None:
        return None

    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _repair(s: str) -> str:
    """Fix the malformations that actually show up in practice."""
    s = re.sub(r",\s*([}\]])", r"\1", s)          # trailing commas
    s = re.sub(r"\bNaN\b|\bInfinity\b", "null", s)
    s = s.replace("“", '"').replace("”", '"')  # smart quotes
    return s


def parse_json(text: str) -> tuple[dict | list | None, str | None]:
    """Extract JSON from model output. Returns (parsed, error)."""
    if not text or not text.strip():
        return None, "empty response"

    cleaned = _THINK.sub("", text).strip()

    candidates: list[str] = []
    if m := _FENCE.search(cleaned):
        candidates.append(m.group(1).strip())
    candidates.append(cleaned)
    if span := _balanced_span(cleaned):
        candidates.append(span)

    for cand in candidates:
        for attempt in (cand, _repair(cand)):
            try:
                return json.loads(attempt), None
            except json.JSONDecodeError:
                continue

    return None, f"unparseable JSON (first 160 chars): {cleaned[:160]!r}"


# --------------------------------------------------------------------------
# Coercion helpers — model output is stringly-typed more often than you'd like
# --------------------------------------------------------------------------
_NUM = re.compile(r"-?[\d,]*\.?\d+")


def as_float(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        # "$1,650.00" / "USD 1650" / "1 650,00"
        m = _NUM.search(v.replace(" ", ""))
        if m:
            try:
                return float(m.group().replace(",", ""))
            except ValueError:
                return None
    return None


def as_str(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v.strip() or None
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v if x is not None) or None
    return str(v)


def as_list(v: Any) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        return [p.strip() for p in re.split(r"[;,\n]", v) if p.strip()]
    return [v]


def as_confidence(v: Any) -> float:
    """Clamp to [0,1], tolerating percentages and words."""
    if isinstance(v, str):
        word = {"high": 0.9, "medium": 0.6, "low": 0.3, "unknown": 0.0}.get(v.lower())
        if word is not None:
            return word
    f = as_float(v)
    if f is None:
        return 0.0
    # Rescale only values that are plausibly on a 0-100 scale. A stray 1.7 is a
    # malformed 0-1 confidence, not 1.7%, so clamp it rather than dividing.
    if 2.0 <= f <= 100.0:
        f = f / 100.0
    return max(0.0, min(1.0, f))
