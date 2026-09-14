from infineq.detection.windows import (
    TimedSample,
    WindowFailureCode,
    baseline_change,
    baseline_ratio,
    detector_crossing_timestamp,
    error_rate,
    median_itl,
    percentile,
    queue_depth_persistence,
    request_rate,
    select_trailing_window,
    slo_crossing_timestamp,
    token_distribution,
)
from infineq.detection.windows import (
    p50 as p50_metric,
)
from infineq.detection.windows import (
    p95 as p95_metric,
)


def test_trailing_window_is_start_inclusive_end_exclusive_and_tie_deterministic() -> None:
    samples = (
        TimedSample(timestamp_s=10.0, value="at-end", key="z"),
        TimedSample(timestamp_s=5.0, value="second", key="b"),
        TimedSample(timestamp_s=5.0, value="first", key="a"),
        TimedSample(timestamp_s=4.999, value="before-start", key="x"),
    )

    selected = select_trailing_window(samples, end_s=10.0, width_s=5.0)

    assert selected.start_s == 5.0
    assert selected.end_s == 10.0
    assert [sample.value for sample in selected.samples] == ["first", "second"]
    assert selected.failure is None


def test_empty_trailing_window_is_an_explicit_typed_failure() -> None:
    selected = select_trailing_window(
        (TimedSample(timestamp_s=20.0, value=1.0),), end_s=10.0, width_s=5.0
    )

    assert selected.samples == ()
    assert selected.failure is not None
    assert selected.failure.code is WindowFailureCode.EMPTY_WINDOW


def test_nonempty_window_below_required_count_is_a_typed_sparse_failure() -> None:
    selected = select_trailing_window(
        (TimedSample(timestamp_s=8.0, value=1.0),),
        end_s=10.0,
        width_s=5.0,
        min_samples=2,
    )

    assert len(selected.samples) == 1
    assert selected.failure is not None
    assert selected.failure.code is WindowFailureCode.SPARSE_WINDOW
    assert selected.failure.sample_count == 1
    assert selected.failure.required_samples == 2


def test_percentile_uses_frozen_linear_interpolation_on_sorted_values() -> None:
    samples = tuple(
        TimedSample(timestamp_s=float(index), value=value)
        for index, value in enumerate((10.0, 20.0, 30.0))
    )

    p50 = percentile(samples, 0.50)
    p95 = percentile(samples, 0.95)
    p50_alias = p50_metric(samples)
    p95_alias = p95_metric(samples)

    assert p50.value == 20.0
    assert p95.value == 29.0
    assert p50_alias == p50
    assert p95_alias == p95
    assert p50.failure is None
    assert p95.failure is None


def test_request_and_error_rates_use_the_half_open_window_duration() -> None:
    samples = (
        TimedSample(timestamp_s=0.0, value="success", key="a"),
        TimedSample(timestamp_s=1.0, value="error", key="b"),
        TimedSample(timestamp_s=2.0, value="timeout", key="c"),
        TimedSample(timestamp_s=4.0, value="error", key="d"),
    )
    selection = select_trailing_window(samples, end_s=4.0, width_s=4.0)

    requests = request_rate(selection)
    errors = error_rate(selection)

    assert requests.value == 3 / 4
    assert errors.value == 2 / 3
    assert requests.failure is None
    assert errors.failure is None


def test_queue_depth_persistence_requires_a_continuous_threshold_duration() -> None:
    samples = (
        TimedSample(timestamp_s=0.0, value=3, key="a"),
        TimedSample(timestamp_s=5.0, value=4, key="b"),
        TimedSample(timestamp_s=10.0, value=3, key="c"),
        TimedSample(timestamp_s=15.0, value=1, key="d"),
    )
    selection = select_trailing_window(samples, end_s=16.0, width_s=16.0)

    result = queue_depth_persistence(selection, threshold=3, duration_s=10.0)

    assert result.value is True
    assert result.persistence_duration_s == 10.0
    assert result.failure is None


def test_baseline_ratio_and_change_are_deterministic_and_zero_baseline_is_typed() -> None:
    ratio = baseline_ratio(150.0, 100.0)
    change = baseline_change(150.0, 100.0)
    zero = baseline_ratio(1.0, 0.0)

    assert ratio.value == 1.5
    assert change.value == 50.0
    assert ratio.failure is None
    assert change.failure is None
    assert zero.value is None
    assert zero.failure is not None
    assert zero.failure.code is WindowFailureCode.ZERO_BASELINE


def test_median_itl_and_token_distribution_use_the_same_frozen_percentile() -> None:
    itl_samples = tuple(
        TimedSample(timestamp_s=float(index), value=value)
        for index, value in enumerate((50.0, 70.0, 60.0))
    )
    token_samples = tuple(
        TimedSample(
            timestamp_s=float(index),
            value={"input_tokens": input_tokens, "output_tokens": output_tokens},
        )
        for index, (input_tokens, output_tokens) in enumerate(((100, 10), (300, 30), (200, 20)))
    )

    itl = median_itl(itl_samples)
    distribution = token_distribution(token_samples)

    assert itl.value == 60.0
    assert distribution.failure is None
    assert distribution.value is not None
    assert distribution.value.input_p50 == 200.0
    assert distribution.value.output_p50 == 20.0
    assert distribution.value.input_values == (100.0, 300.0, 200.0)
    assert distribution.value.output_values == (10.0, 30.0, 20.0)


def test_detector_and_slo_crossings_are_first_sorted_threshold_timestamps() -> None:
    samples = (
        TimedSample(timestamp_s=10.0, value=1_000.0, key="later"),
        TimedSample(timestamp_s=5.0, value=999.0, key="before"),
        TimedSample(timestamp_s=8.0, value=1_000.0, key="first"),
    )

    detector = detector_crossing_timestamp(samples, threshold=1_000.0)
    slo = slo_crossing_timestamp(samples, threshold=1_000.0)
    strict = slo_crossing_timestamp(samples, threshold=1_000.0, inclusive=False)

    assert detector.value == 8.0
    assert slo.value == 8.0
    assert detector.timestamp_s == 8.0
    assert slo.timestamp_s == 8.0
    assert strict.value is None
    assert strict.failure is not None
    assert strict.failure.code is WindowFailureCode.NO_CROSSING
