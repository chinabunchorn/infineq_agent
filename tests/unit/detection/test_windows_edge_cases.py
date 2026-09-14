import math
from typing import Any

import pytest

from infineq.detection.windows import (
    TimedSample,
    WindowFailure,
    WindowFailureCode,
    WindowResult,
    WindowSelection,
    baseline_change,
    baseline_ratio,
    crossing_timestamp,
    error_rate,
    median_itl,
    p50,
    percentile,
    queue_depth_persistence,
    request_rate,
    select_trailing_window,
    token_distribution,
)


def sample(value: object, timestamp_s: float = 1.0) -> TimedSample[Any]:
    return TimedSample(timestamp_s=timestamp_s, value=value)


def test_window_results_and_percentiles_propagate_failures_and_reject_invalid_inputs() -> None:
    failure = WindowFailure(WindowFailureCode.EMPTY_WINDOW, "empty")
    failed_selection = WindowSelection(0.0, 1.0, (), failure)

    assert not WindowResult(value=None, failure=failure).available
    assert WindowResult(value=1.0).available
    assert p50(failed_selection).failure is failure
    assert percentile((sample(True),), 0.5).failure is not None
    assert percentile((sample(math.nan),), 0.5).failure is not None

    with pytest.raises(ValueError):
        percentile((sample(1.0),), -0.1)
    with pytest.raises(ValueError):
        percentile((sample(1.0),), 1.1)
    with pytest.raises(ValueError):
        p50((sample(1.0),), min_samples=0)


def test_window_selection_and_raw_rate_arguments_validate_boundaries() -> None:
    with pytest.raises(ValueError):
        select_trailing_window((), end_s=1.0, width_s=0.0)
    with pytest.raises(ValueError):
        select_trailing_window((), end_s=1.0, width_s=1.0, min_samples=0)
    with pytest.raises(ValueError):
        request_rate((sample("ok"),), duration_s=0.0)
    with pytest.raises(ValueError):
        request_rate((sample("ok"),), start_s=1.0, end_s=1.0)
    with pytest.raises(ValueError):
        request_rate((sample("ok"),))

    assert request_rate((sample("ok"),), duration_s=2.0).value == 0.5
    assert request_rate((sample("ok"),), start_s=0.0, end_s=2.0).value == 0.5
    assert request_rate((sample("ok"),), start_s=1.0, duration_s=2.0).value == 0.5


def test_error_rate_classifies_mapping_boolean_numeric_and_string_outcomes() -> None:
    values = (
        sample({"status": "ok"}, 0.0),
        sample({"outcome": "failed"}, 1.0),
        sample(True, 2.0),
        sample(0, 3.0),
        sample(2, 4.0),
        sample("completed", 5.0),
        sample("cancelled", 6.0),
        sample(object(), 7.0),
    )

    result = error_rate(values, start_s=0.0, end_s=8.0)

    assert result.failure is None
    assert result.value == 4 / 8


def test_queue_persistence_supports_aliases_exclusive_threshold_and_invalid_values() -> None:
    samples = (
        sample({"queue_depth": 2}, 0.0),
        sample({"value": 4}, 2.0),
        sample({"queue_depth": 4}, 4.0),
        sample({"queue_depth": 1}, 6.0),
    )

    assert (
        queue_depth_persistence(
            samples, threshold=3, required_duration_s=2.0, inclusive=False
        ).value
        is True
    )
    assert queue_depth_persistence(samples, threshold=3, duration_s=0.0).value is True
    assert (
        queue_depth_persistence(samples, threshold=3, duration_s=2.0).persistence_duration_s == 2.0
    )

    with pytest.raises(ValueError):
        queue_depth_persistence(samples, threshold=3, duration_s=1.0, required_duration_s=2.0)
    with pytest.raises(ValueError):
        queue_depth_persistence(samples, threshold=3, duration_s=-1.0)
    assert (
        queue_depth_persistence((sample("not-a-number"),), threshold=1, duration_s=1.0).failure
        is not None
    )


def test_baseline_comparisons_propagate_nested_and_invalid_values() -> None:
    failed = WindowResult[float](
        value=None,
        failure=WindowFailure(WindowFailureCode.EMPTY_WINDOW, "empty"),
    )
    no_value = WindowResult[float](value=None)

    assert baseline_ratio(failed, 1.0).failure is failed.failure
    assert baseline_ratio(no_value, 1.0).failure is not None
    assert baseline_change(1.0, failed).failure is failed.failure
    assert baseline_change(no_value, 1.0).failure is not None
    assert baseline_ratio(True, 1.0).failure is not None
    assert baseline_change(1.0, math.inf).failure is not None


def test_itl_and_tokens_accept_supported_shapes_and_report_bad_shapes() -> None:
    itl_samples = (
        sample({"itl_ms": 10.0}, 0.0),
        sample({"itl_s": 0.02}, 1.0),
        sample({"itl": 30.0}, 2.0),
        sample(40.0, 3.0),
    )
    itl = median_itl(itl_samples)
    assert itl.failure is None
    assert itl.value == 25.0
    assert median_itl((sample({"itl_ms": "bad"}),)).failure is not None

    inputs = (
        sample({"input_tokens_count": 10, "output_tokens_count": 2}, 0.0),
        sample((20, 4), 1.0),
    )
    outputs = (
        sample({"input_tokens": 30, "output_tokens": 6}, 0.0),
        sample((40, 8), 1.0),
    )
    distribution = token_distribution(inputs, outputs)
    assert distribution.failure is None
    assert distribution.value is not None
    assert distribution.value.input_values == (10.0, 20.0)
    assert distribution.value.output_values == (6.0, 8.0)
    assert token_distribution(inputs, inputs).value is not None
    assert token_distribution(inputs, (sample((1, 2)),), min_samples=1).failure is not None
    assert token_distribution((sample("bad"),)).failure is not None


def test_crossing_reports_invalid_direction_invalid_values_and_no_crossing() -> None:
    with pytest.raises(ValueError):
        crossing_timestamp((sample(1.0),), threshold=1.0, direction="sideways")

    invalid = crossing_timestamp((sample({"value": "bad"}),), threshold=1.0)
    assert invalid.failure is not None
    assert invalid.failure.code is WindowFailureCode.INVALID_VALUE

    below = crossing_timestamp(
        (sample(2.0, 0.0), sample(1.0, 1.0)), threshold=1.0, direction="below", inclusive=False
    )
    assert below.value is None
    assert below.failure is not None
    assert below.failure.code is WindowFailureCode.NO_CROSSING
