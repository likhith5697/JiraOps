"""Backoff calculation shared by in-call HTTP retries and job-level retries."""

from __future__ import annotations

import random


def exponential_backoff(
    attempt: int,
    *,
    base: float,
    cap: float,
    jitter: float = 0.2,
    rng: random.Random | None = None,
) -> float:
    """Delay before retry number ``attempt`` (0-based): min(cap, base * 2**attempt) +/- jitter."""
    if base <= 0:
        return 0.0
    delay = min(cap, base * (2 ** max(0, attempt)))
    if jitter:
        r = (rng or random).uniform(-jitter, jitter)
        delay = max(0.0, delay * (1 + r))
    return delay
