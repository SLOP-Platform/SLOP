"""backend.agent.router.scoring — Deterministic prompt complexity heuristic.

``complexity_score(prompt)`` maps a prompt to a :class:`Tier` using a small set
of explicit, auditable signals.  No randomness, no I/O, no network — the same
input always yields the same Tier (ADR-0010: explicit logic only).

Scoring model
-------------
We accumulate an integer ``score`` from independent signals, then map the final
score onto a Tier band.  The signals and their weights are:

Length (cheap proxy for token count; ~4 chars/token):
    * ``len(prompt) >= 2000`` chars  → +2   (~500+ tokens — a large task)
    * ``len(prompt) >= 600``  chars  → +1   (~150+ tokens — non-trivial)
    * otherwise                      → +0

Structure (code / errors usually need a stronger model):
    * a fenced code block (``` ... ```)            → +1
    * a stack trace / traceback signal             → +2
      (``Traceback``, a ``  File "..."`` frame line, ``Exception``/``Error:``
       with a traceback-like shape, ``at <pkg>.<Class>`` Java-style frames)

Reasoning intent (open-ended analysis → escalate to the top tier):
    * any reasoning keyword present (whole-word, case-insensitive) → +2
      keywords: why, design, architect/architecture, compare, comparison,
      trade-off / tradeoff, root cause, explain, rationale, reasoning,
      pros and cons, analyze / analyse, evaluate, justify, prove.

Score → Tier band:
    score <= 0   → SIMPLE
    score == 1   → STANDARD
    score in 2,3 → COMPLEX
    score >= 4   → REASONING

The reasoning-keyword weight (+2) plus any one other signal (>=+2 more) lifts a
prompt into REASONING, which matches the design intent: explicit "why/design/
compare" requests deserve the strongest available model.  A lone reasoning
keyword on a tiny prompt lands at COMPLEX (score 2), which is the deliberate
floor for analysis-style asks.
"""

from __future__ import annotations

import re

from backend.agent.router.types import Tier

# --- Length thresholds (characters; ~4 chars/token) ------------------------
_LEN_LARGE = 2000
_LEN_MEDIUM = 600

# --- Structure signals -----------------------------------------------------
# A fenced code block: opening ``` (optionally with a language) — we only need
# to know one exists, not that it is balanced.
_CODE_FENCE_RE = re.compile(r"```")

# Stack-trace / traceback signals.  Any one match counts as a stacktrace.
_STACKTRACE_RES = (
    re.compile(r"\bTraceback \(most recent call last\)"),
    re.compile(r"\bTraceback\b"),
    re.compile(r'^\s*File "[^"]+", line \d+', re.MULTILINE),  # Python frame
    re.compile(r"^\s+at [\w$.]+\([^)]*\)", re.MULTILINE),  # Java/JS frame
    re.compile(r"\b[A-Za-z_][\w.]*(?:Error|Exception):", re.MULTILINE),
)

# --- Reasoning keywords (whole-word, case-insensitive) ---------------------
# Exact terms match with \b on both ends (whole-word / whole-phrase).
_REASONING_TERMS = (
    "why",
    "design",
    "compare",
    "comparison",
    "trade-off",
    "tradeoff",
    "root cause",
    "explain",
    "rationale",
    "reasoning",
    "pros and cons",
    "analyze",
    "analyse",
    "evaluate",
    "justify",
    "prove",
)
# Stem terms match a leading word boundary but allow trailing word chars, so
# "architect" also catches "architecture" / "architectural".
_REASONING_STEMS = ("architect",)
# Build one alternation regex.  Exact terms are \b-bounded on both ends; stems
# are \b-bounded on the left only.
_REASONING_RE = re.compile(
    r"\b(?:"
    + "|".join(re.escape(t) for t in _REASONING_TERMS)
    + ")\\b"
    + "|\\b(?:"
    + "|".join(re.escape(t) for t in _REASONING_STEMS)
    + r")\w*",
    re.IGNORECASE,
)


def _length_score(prompt: str) -> int:
    n = len(prompt)
    if n >= _LEN_LARGE:
        return 2
    if n >= _LEN_MEDIUM:
        return 1
    return 0


def _has_code_block(prompt: str) -> bool:
    return _CODE_FENCE_RE.search(prompt) is not None


def _has_stacktrace(prompt: str) -> bool:
    return any(rx.search(prompt) for rx in _STACKTRACE_RES)


def _has_reasoning_keyword(prompt: str) -> bool:
    return _REASONING_RE.search(prompt) is not None


def complexity_score(prompt: str) -> Tier:
    """Map *prompt* to a :class:`Tier` deterministically.

    See the module docstring for the exact signal weights and score→Tier bands.
    A ``None`` or empty prompt scores SIMPLE.
    """
    if not prompt:
        return Tier.SIMPLE

    score = 0
    score += _length_score(prompt)
    if _has_code_block(prompt):
        score += 1
    if _has_stacktrace(prompt):
        score += 2
    if _has_reasoning_keyword(prompt):
        score += 2

    if score <= 0:
        return Tier.SIMPLE
    if score == 1:
        return Tier.STANDARD
    if score <= 3:
        return Tier.COMPLEX
    return Tier.REASONING
