"""Frozen numerical policy for detector-v1."""

from __future__ import annotations

from dataclasses import dataclass
from math import isclose

EVALUATION_INTERVAL_S = 5.0
TRAILING_WINDOW_S = 15.0
MIN_REQUESTS = 20
TTFT_THRESHOLD_MS = 1_000.0
ERROR_RATE_THRESHOLD = 0.05


@dataclass(frozen=True, slots=True)
class PolicyObservation:
    """One quality-ready evaluation of the frozen observation window."""

    end_s: float
    request_count: int
    ttft_p95_ms: float | None
    error_rate: float | None
    corroborating_signals: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """A numerical policy result with no causal or fault label."""

    triggered: bool
    primary_signal: str | None = None
    corroborating_signals: tuple[str, ...] = ()


def _has_second_signal(current: PolicyObservation, primary_signal: str) -> bool:
    if primary_signal == "error_rate":
        return any(signal != "error_rate" for signal in current.corroborating_signals)
    if primary_signal == "ttft":
        return any(signal != "ttft" for signal in current.corroborating_signals)
    return "ttft" in current.corroborating_signals and primary_signal != "ttft"


def evaluate_policy(
    current: PolicyObservation,
    *,
    previous: PolicyObservation | None = None,
) -> PolicyDecision:
    """Evaluate the frozen latency/error rules for one five-second check."""

    corroborating = current.corroborating_signals
    if current.request_count < MIN_REQUESTS or not corroborating:
        return PolicyDecision(False, corroborating_signals=corroborating)
    if (
        current.error_rate is not None
        and current.error_rate >= ERROR_RATE_THRESHOLD
        and _has_second_signal(current, "error_rate")
    ):
        return PolicyDecision(True, "error_rate", corroborating)
    consecutive_ttft = (
        current.ttft_p95_ms is not None
        and current.ttft_p95_ms >= TTFT_THRESHOLD_MS
        and previous is not None
        and previous.request_count >= MIN_REQUESTS
        and isclose(
            previous.end_s + EVALUATION_INTERVAL_S,
            current.end_s,
            rel_tol=0.0,
            abs_tol=1e-6,
        )
        and previous.ttft_p95_ms is not None
        and previous.ttft_p95_ms >= TTFT_THRESHOLD_MS
        and _has_second_signal(current, "ttft")
    )
    if consecutive_ttft:
        return PolicyDecision(True, "ttft", corroborating)
    return PolicyDecision(False, corroborating_signals=corroborating)


__all__ = [
    "ERROR_RATE_THRESHOLD",
    "EVALUATION_INTERVAL_S",
    "MIN_REQUESTS",
    "TRAILING_WINDOW_S",
    "TTFT_THRESHOLD_MS",
    "PolicyDecision",
    "PolicyObservation",
    "evaluate_policy",
]
