"""Deterministic rolling-window selection and aggregations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from typing import cast


@dataclass(frozen=True, slots=True)
class TimedSample[T]:
    """One value observed at replay time in seconds."""

    timestamp_s: float
    value: T
    key: str = ""


class WindowFailureCode(StrEnum):
    """Stable failure codes for non-decision-ready rolling calculations."""

    EMPTY_WINDOW = "empty_window"
    SPARSE_WINDOW = "sparse_window"
    INVALID_WINDOW = "invalid_window"
    INVALID_QUANTILE = "invalid_quantile"
    INVALID_VALUE = "invalid_value"
    ZERO_BASELINE = "zero_baseline"
    NO_CROSSING = "no_crossing"


@dataclass(frozen=True, slots=True)
class WindowFailure:
    """Typed explanation for an unavailable rolling-window result."""

    code: WindowFailureCode
    message: str
    sample_count: int = 0
    required_samples: int = 0


@dataclass(frozen=True, slots=True)
class TokenDistribution:
    """Frozen input/output token observations and their percentile summaries."""

    input_values: tuple[float, ...]
    output_values: tuple[float, ...]
    input_p50: float
    input_p95: float
    output_p50: float
    output_p95: float

    @property
    def input_median(self) -> float:
        """Return the median input-token count."""

        return self.input_p50

    @property
    def output_median(self) -> float:
        """Return the median output-token count."""

        return self.output_p50


@dataclass(frozen=True, slots=True)
class WindowResult[T]:
    """Immutable value-or-failure result for a rolling calculation."""

    value: T | None
    failure: WindowFailure | None = None
    persistence_duration_s: float | None = None
    timestamp_s: float | None = None

    @property
    def available(self) -> bool:
        """Whether the calculation produced a value."""

        return self.failure is None


def _normalise_samples[T](
    samples: WindowSelection[T] | Sequence[TimedSample[T]], *, min_samples: int
) -> tuple[tuple[TimedSample[T], ...], WindowFailure | None]:
    if min_samples < 1:
        raise ValueError("minimum sample count must be positive")
    if isinstance(samples, WindowSelection):
        selected = samples.samples
        if samples.failure is not None:
            return selected, samples.failure
    else:
        selected = tuple(samples)

    if not selected:
        return selected, WindowFailure(
            code=WindowFailureCode.EMPTY_WINDOW,
            message="rolling window contains no observations",
            sample_count=0,
            required_samples=min_samples,
        )
    if len(selected) < min_samples:
        return selected, WindowFailure(
            code=WindowFailureCode.SPARSE_WINDOW,
            message="rolling window contains fewer than the required observations",
            sample_count=len(selected),
            required_samples=min_samples,
        )
    return selected, None


def percentile(
    samples: WindowSelection[float] | Sequence[TimedSample[float]],
    quantile: float,
    *,
    min_samples: int = 1,
) -> WindowResult[float]:
    """Compute a percentile using the frozen ``(n - 1) * q`` interpolation."""

    if not 0.0 <= quantile <= 1.0:
        raise ValueError("quantile must be between zero and one")
    selected, failure = _normalise_samples(samples, min_samples=min_samples)
    if failure is not None:
        return WindowResult(value=None, failure=failure)

    values: list[float] = []
    for sample in selected:
        value = sample.value
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
            return WindowResult(
                value=None,
                failure=WindowFailure(
                    code=WindowFailureCode.INVALID_VALUE,
                    message="percentile requires finite numeric observations",
                    sample_count=len(selected),
                    required_samples=min_samples,
                ),
            )
        values.append(float(value))

    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    value = ordered[lower] + (ordered[upper] - ordered[lower]) * fraction
    return WindowResult(value=value)


def p50(
    samples: WindowSelection[float] | Sequence[TimedSample[float]], *, min_samples: int = 1
) -> WindowResult[float]:
    """Compute the frozen p50 percentile."""

    return percentile(samples, 0.50, min_samples=min_samples)


def p95(
    samples: WindowSelection[float] | Sequence[TimedSample[float]], *, min_samples: int = 1
) -> WindowResult[float]:
    """Compute the frozen p95 percentile."""

    return percentile(samples, 0.95, min_samples=min_samples)


@dataclass(frozen=True, slots=True)
class WindowSelection[T]:
    """A half-open trailing selection with an explicit failure slot."""

    start_s: float
    end_s: float
    samples: tuple[TimedSample[T], ...]
    failure: WindowFailure | None = None


def select_trailing_window[T](
    samples: tuple[TimedSample[T], ...],
    *,
    end_s: float,
    width_s: float,
    min_samples: int = 1,
) -> WindowSelection[T]:
    """Select ``[max(0, end-width), end)`` in deterministic order."""

    if width_s <= 0:
        raise ValueError("window width must be positive")
    if min_samples < 1:
        raise ValueError("minimum sample count must be positive")

    start_s = max(0.0, end_s - width_s)
    selected = tuple(
        sorted(
            (sample for sample in samples if start_s <= sample.timestamp_s < end_s),
            key=lambda sample: (sample.timestamp_s, sample.key),
        )
    )
    failure = None
    if not selected:
        failure = WindowFailure(
            code=WindowFailureCode.EMPTY_WINDOW,
            message="rolling window contains no observations",
            sample_count=0,
            required_samples=min_samples,
        )
    elif len(selected) < min_samples:
        failure = WindowFailure(
            code=WindowFailureCode.SPARSE_WINDOW,
            message="rolling window contains fewer than the required observations",
            sample_count=len(selected),
            required_samples=min_samples,
        )
    return WindowSelection(start_s=start_s, end_s=end_s, samples=selected, failure=failure)


def _rate_samples(
    samples: WindowSelection[object] | Sequence[TimedSample[object]],
    *,
    start_s: float | None,
    end_s: float | None,
    duration_s: float | None,
    min_samples: int,
) -> tuple[tuple[TimedSample[object], ...], float, float, WindowFailure | None]:
    selected, failure = _normalise_samples(samples, min_samples=min_samples)
    if isinstance(samples, WindowSelection):
        window_start = samples.start_s
        window_end = samples.end_s
    else:
        if duration_s is not None:
            if duration_s <= 0:
                raise ValueError("window duration must be positive")
            window_start = 0.0 if start_s is None else start_s
            window_end = window_start + duration_s if end_s is None else end_s
        elif start_s is not None and end_s is not None:
            window_start = start_s
            window_end = end_s
        else:
            raise ValueError("raw samples require start_s and end_s or duration_s")
    if window_end <= window_start:
        raise ValueError("window end must follow window start")
    return selected, window_start, window_end, failure


def request_rate(
    samples: WindowSelection[object] | Sequence[TimedSample[object]],
    *,
    start_s: float | None = None,
    end_s: float | None = None,
    duration_s: float | None = None,
    min_samples: int = 1,
) -> WindowResult[float]:
    """Return requests per second over the selected half-open interval."""

    selected, window_start, window_end, failure = _rate_samples(
        samples,
        start_s=start_s,
        end_s=end_s,
        duration_s=duration_s,
        min_samples=min_samples,
    )
    if failure is not None:
        return WindowResult(value=None, failure=failure)
    return WindowResult(value=len(selected) / (window_end - window_start))


def _is_error(value: object) -> bool:
    if isinstance(value, Mapping):
        value = value.get("status", value.get("outcome", value))
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() not in {"ok", "success", "succeeded", "complete", "completed"}
    return False


def error_rate(
    samples: WindowSelection[object] | Sequence[TimedSample[object]],
    *,
    start_s: float | None = None,
    end_s: float | None = None,
    duration_s: float | None = None,
    min_samples: int = 1,
) -> WindowResult[float]:
    """Return the fraction of selected requests with an error outcome."""

    selected, _window_start, _window_end, failure = _rate_samples(
        samples,
        start_s=start_s,
        end_s=end_s,
        duration_s=duration_s,
        min_samples=min_samples,
    )
    if failure is not None:
        return WindowResult(value=None, failure=failure)
    return WindowResult(value=sum(_is_error(sample.value) for sample in selected) / len(selected))


def _numeric_metric_value(value: object, *, field: str) -> float | None:
    if isinstance(value, Mapping):
        value = value.get(field, value.get("value"))
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if isfinite(numeric) else None


def queue_depth_persistence(
    samples: WindowSelection[object] | Sequence[TimedSample[object]],
    *,
    threshold: float,
    duration_s: float | None = None,
    required_duration_s: float | None = None,
    inclusive: bool = True,
    min_samples: int = 1,
) -> WindowResult[bool]:
    """Report whether queue depth stays at threshold for the requested duration."""

    if duration_s is None:
        duration_s = required_duration_s
    elif required_duration_s is not None and duration_s != required_duration_s:
        raise ValueError("duration_s and required_duration_s must agree")
    if duration_s is None or duration_s < 0:
        raise ValueError("a non-negative persistence duration is required")

    selected, failure = _normalise_samples(samples, min_samples=min_samples)
    if failure is not None:
        return WindowResult(value=None, failure=failure)

    ordered: list[tuple[float, float]] = []
    for sample in selected:
        depth = _numeric_metric_value(sample.value, field="queue_depth")
        if depth is None:
            return WindowResult(
                value=None,
                failure=WindowFailure(
                    code=WindowFailureCode.INVALID_VALUE,
                    message="queue depth requires finite numeric observations",
                    sample_count=len(selected),
                    required_samples=min_samples,
                ),
            )
        ordered.append((sample.timestamp_s, depth))

    ordered.sort(key=lambda item: item[0])
    longest_duration = 0.0
    active_start: float | None = None
    for timestamp_s, depth in ordered:
        active = depth >= threshold if inclusive else depth > threshold
        if active:
            if active_start is None:
                active_start = timestamp_s
            longest_duration = max(longest_duration, timestamp_s - active_start)
        else:
            active_start = None
    return WindowResult(
        value=longest_duration >= duration_s,
        persistence_duration_s=longest_duration,
    )


def _scalar_value(
    value: float | int | WindowResult[float],
) -> tuple[float | None, WindowFailure | None]:
    if isinstance(value, WindowResult):
        if value.failure is not None:
            return None, value.failure
        if value.value is None:
            return None, WindowFailure(
                code=WindowFailureCode.INVALID_VALUE,
                message="comparison requires a scalar observation",
            )
        value = value.value
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        return None, WindowFailure(
            code=WindowFailureCode.INVALID_VALUE,
            message="comparison requires finite numeric observations",
        )
    return float(value), None


def _comparison_values(
    observed: float | int | WindowResult[float],
    baseline: float | int | WindowResult[float],
) -> tuple[float | None, float | None, WindowFailure | None]:
    observed_value, observed_failure = _scalar_value(observed)
    if observed_failure is not None:
        return None, None, observed_failure
    baseline_value, baseline_failure = _scalar_value(baseline)
    if baseline_failure is not None:
        return None, None, baseline_failure
    return observed_value, baseline_value, None


def baseline_ratio(
    observed: float | int | WindowResult[float],
    baseline: float | int | WindowResult[float],
) -> WindowResult[float]:
    """Return observed divided by baseline without silently handling zero."""

    observed_value, baseline_value, failure = _comparison_values(observed, baseline)
    if failure is not None:
        return WindowResult(value=None, failure=failure)
    if observed_value is None or baseline_value is None:
        return WindowResult(
            value=None,
            failure=WindowFailure(
                code=WindowFailureCode.INVALID_VALUE,
                message="comparison requires finite numeric observations",
            ),
        )
    if baseline_value == 0.0:
        return WindowResult(
            value=None,
            failure=WindowFailure(
                code=WindowFailureCode.ZERO_BASELINE,
                message="baseline ratio is undefined for a zero baseline",
            ),
        )
    return WindowResult(value=observed_value / baseline_value)


def baseline_change(
    observed: float | int | WindowResult[float],
    baseline: float | int | WindowResult[float],
) -> WindowResult[float]:
    """Return the absolute observed-minus-baseline change."""

    observed_value, baseline_value, failure = _comparison_values(observed, baseline)
    if failure is not None:
        return WindowResult(value=None, failure=failure)
    if observed_value is None or baseline_value is None:
        return WindowResult(
            value=None,
            failure=WindowFailure(
                code=WindowFailureCode.INVALID_VALUE,
                message="comparison requires finite numeric observations",
            ),
        )
    return WindowResult(value=observed_value - baseline_value)


def _itl_value(value: object) -> float | None:
    if isinstance(value, Mapping):
        if "itl_ms" in value:
            return _numeric_metric_value(value["itl_ms"], field="itl_ms")
        if "itl_s" in value:
            seconds = _numeric_metric_value(value["itl_s"], field="itl_s")
            return None if seconds is None else seconds * 1_000.0
        value = value.get("itl", value.get("value"))
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if isfinite(numeric) else None


def median_itl(
    samples: WindowSelection[object] | Sequence[TimedSample[object]], *, min_samples: int = 1
) -> WindowResult[float]:
    """Return median ITL in milliseconds, preserving explicit window failures."""

    selected, failure = _normalise_samples(samples, min_samples=min_samples)
    if failure is not None:
        return WindowResult(value=None, failure=failure)
    values: list[TimedSample[float]] = []
    for sample in selected:
        itl = _itl_value(sample.value)
        if itl is None:
            return WindowResult(
                value=None,
                failure=WindowFailure(
                    code=WindowFailureCode.INVALID_VALUE,
                    message="ITL requires finite numeric observations",
                    sample_count=len(selected),
                    required_samples=min_samples,
                ),
            )
        values.append(TimedSample(timestamp_s=sample.timestamp_s, value=itl, key=sample.key))
    return p50(values, min_samples=min_samples)


def _token_value(value: object, field: str) -> float | None:
    if isinstance(value, Mapping):
        aliases = (field, field.removesuffix("_tokens"), f"{field}_count")
        raw = next((value[alias] for alias in aliases if alias in value), None)
    elif isinstance(value, (tuple, list)) and len(value) == 2:
        raw = value[0 if field == "input_tokens" else 1]
    else:
        raw = value
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    numeric = float(raw)
    return numeric if isfinite(numeric) else None


def token_distribution(
    samples: WindowSelection[object] | Sequence[TimedSample[object]],
    output_samples: WindowSelection[object] | Sequence[TimedSample[object]] | None = None,
    *,
    min_samples: int = 1,
) -> WindowResult[TokenDistribution]:
    """Return immutable input/output token values plus frozen p50/p95 summaries."""

    input_selected, input_failure = _normalise_samples(samples, min_samples=min_samples)
    if input_failure is not None:
        return WindowResult(value=None, failure=input_failure)
    if output_samples is None:
        output_selected = input_selected
    else:
        output_selected, output_failure = _normalise_samples(
            output_samples, min_samples=min_samples
        )
        if output_failure is not None:
            return WindowResult(value=None, failure=output_failure)
    if len(input_selected) != len(output_selected):
        return WindowResult(
            value=None,
            failure=WindowFailure(
                code=WindowFailureCode.INVALID_VALUE,
                message="input and output token observations must have equal length",
                sample_count=min(len(input_selected), len(output_selected)),
                required_samples=max(len(input_selected), len(output_selected)),
            ),
        )

    input_values: list[float] = []
    output_values: list[float] = []
    for input_sample, output_sample in zip(input_selected, output_selected, strict=True):
        input_value = _token_value(input_sample.value, "input_tokens")
        output_value = _token_value(output_sample.value, "output_tokens")
        if input_value is None or output_value is None:
            return WindowResult(
                value=None,
                failure=WindowFailure(
                    code=WindowFailureCode.INVALID_VALUE,
                    message="token distribution requires finite numeric token counts",
                    sample_count=len(input_selected),
                    required_samples=min_samples,
                ),
            )
        input_values.append(input_value)
        output_values.append(output_value)

    input_timed = tuple(
        TimedSample(timestamp_s=float(index), value=value)
        for index, value in enumerate(input_values)
    )
    output_timed = tuple(
        TimedSample(timestamp_s=float(index), value=value)
        for index, value in enumerate(output_values)
    )
    input_p50 = p50(input_timed).value
    input_p95 = p95(input_timed).value
    output_p50 = p50(output_timed).value
    output_p95 = p95(output_timed).value
    if any(value is None for value in (input_p50, input_p95, output_p50, output_p95)):
        raise AssertionError("validated token observations must produce percentiles")
    return WindowResult(
        value=TokenDistribution(
            input_values=tuple(input_values),
            output_values=tuple(output_values),
            input_p50=cast(float, input_p50),
            input_p95=cast(float, input_p95),
            output_p50=cast(float, output_p50),
            output_p95=cast(float, output_p95),
        )
    )


def crossing_timestamp(
    samples: WindowSelection[object] | Sequence[TimedSample[object]],
    threshold: float,
    *,
    direction: str = "above",
    inclusive: bool = True,
    min_samples: int = 1,
) -> WindowResult[float]:
    """Return the first ordered timestamp that crosses a threshold."""

    if direction not in {"above", "below"}:
        raise ValueError("direction must be 'above' or 'below'")
    selected, failure = _normalise_samples(samples, min_samples=min_samples)
    if failure is not None:
        return WindowResult(value=None, failure=failure)

    ordered = sorted(selected, key=lambda sample: (sample.timestamp_s, sample.key))
    for sample in ordered:
        value = _numeric_metric_value(sample.value, field="value")
        if value is None:
            return WindowResult(
                value=None,
                failure=WindowFailure(
                    code=WindowFailureCode.INVALID_VALUE,
                    message="threshold crossings require finite numeric observations",
                    sample_count=len(selected),
                    required_samples=min_samples,
                ),
            )
        crossed = (
            (value >= threshold if inclusive else value > threshold)
            if direction == "above"
            else (value <= threshold if inclusive else value < threshold)
        )
        if crossed:
            return WindowResult(value=sample.timestamp_s, timestamp_s=sample.timestamp_s)
    return WindowResult(
        value=None,
        failure=WindowFailure(
            code=WindowFailureCode.NO_CROSSING,
            message="no observation crosses the requested threshold",
            sample_count=len(selected),
            required_samples=min_samples,
        ),
    )


def detector_crossing_timestamp(
    samples: WindowSelection[object] | Sequence[TimedSample[object]],
    threshold: float,
    *,
    direction: str = "above",
    inclusive: bool = True,
    min_samples: int = 1,
) -> WindowResult[float]:
    """Return the first timestamp at which a detector threshold is crossed."""

    return crossing_timestamp(
        samples,
        threshold,
        direction=direction,
        inclusive=inclusive,
        min_samples=min_samples,
    )


def slo_crossing_timestamp(
    samples: WindowSelection[object] | Sequence[TimedSample[object]],
    threshold: float,
    *,
    direction: str = "above",
    inclusive: bool = True,
    min_samples: int = 1,
) -> WindowResult[float]:
    """Return the first timestamp at which an SLO threshold is crossed."""

    return crossing_timestamp(
        samples,
        threshold,
        direction=direction,
        inclusive=inclusive,
        min_samples=min_samples,
    )


__all__ = [
    "TimedSample",
    "TokenDistribution",
    "WindowFailure",
    "WindowFailureCode",
    "WindowResult",
    "WindowSelection",
    "baseline_change",
    "baseline_ratio",
    "crossing_timestamp",
    "detector_crossing_timestamp",
    "error_rate",
    "median_itl",
    "p50",
    "p95",
    "percentile",
    "queue_depth_persistence",
    "request_rate",
    "select_trailing_window",
    "slo_crossing_timestamp",
    "token_distribution",
]
