from __future__ import annotations

from dataclasses import replace

from infineq.simulator.engine import (
    RatePhase,
    ReplicaChange,
    SimulationConfig,
    nominal_capacity_per_second,
    simulate,
)
from infineq.simulator.serialization import stable_json_bytes


def config(*, rate: float, duration_s: float = 20.0, seed: int = 7) -> SimulationConfig:
    return SimulationConfig(
        duration_s=duration_s,
        workload=(RatePhase(0.0, duration_s, rate),),
        seed=seed,
        jitter_fraction=0.0,
        input_tokens=1,
        output_tokens=3,
        base_itl_s=0.5,
        prefill_base_s=0.1,
        prefill_per_input_s=0.0,
    )


def test_empty_workload_produces_no_events() -> None:
    result = simulate(config(rate=0.0))

    assert result.requests == ()


def test_below_capacity_arrivals_have_no_sustained_queue_wait() -> None:
    result = simulate(config(rate=1.0))

    assert result.requests
    assert max(event.queue_wait_s for event in result.requests) == 0.0


def test_above_capacity_arrivals_grow_a_queue() -> None:
    result = simulate(config(rate=10.0, duration_s=20.0))

    assert max(event.queue_wait_s for event in result.requests) > 0.0
    assert result.queue_depth_at(19.0) > 0


def test_replica_change_increases_slots_only_at_approved_timestamp() -> None:
    result = simulate(
        replace(
            config(rate=10.0, duration_s=12.0),
            replica_changes=(ReplicaChange(timestamp_s=5.0, replicas=2),),
        )
    )

    assert all(event.replica_index == 0 for event in result.requests if event.service_start_s < 5.0)
    assert any(
        event.replica_index == 1 for event in result.requests if event.service_start_s >= 5.0
    )
    assert result.replica_snapshots[-1].ready_replicas == 2


def test_fcfs_order_is_stable_for_requests_waiting_in_the_same_queue() -> None:
    result = simulate(config(rate=4.0, duration_s=8.0))
    started = sorted(result.requests, key=lambda event: (event.service_start_s, event.request_id))

    assert [event.request_id for event in started] == [
        event.request_id for event in result.requests
    ]


def test_ttft_is_queue_wait_plus_prefill_and_itl_ignores_queue_wait() -> None:
    queued = simulate(config(rate=3.0, duration_s=12.0))
    uncongested = simulate(config(rate=1.0, duration_s=12.0))

    assert all(
        abs(event.ttft_s - (event.queue_wait_s + event.prefill_s)) < 1e-9
        for event in queued.requests
    )
    assert {event.itl_s for event in queued.requests} == {
        event.itl_s for event in uncongested.requests
    }


def test_nominal_capacity_matches_the_frozen_approximately_232_and_464_values() -> None:
    one = nominal_capacity_per_second()
    two = nominal_capacity_per_second(replicas=2)

    assert 2.30 < one < 2.34
    assert 4.60 < two < 4.68


def test_identical_seed_and_config_produce_identical_normalized_bytes() -> None:
    left = simulate(config(rate=2.0, seed=31))
    right = simulate(config(rate=2.0, seed=31))

    assert stable_json_bytes(left.to_records()) == stable_json_bytes(right.to_records())


def test_different_seed_changes_jitter_without_changing_replica_policy() -> None:
    left = simulate(replace(config(rate=2.0, seed=31), jitter_fraction=0.05, input_tokens=256))
    right = simulate(replace(config(rate=2.0, seed=32), jitter_fraction=0.05, input_tokens=256))

    assert [event.input_tokens for event in left.requests] != [
        event.input_tokens for event in right.requests
    ]
    assert left.replica_snapshots == right.replica_snapshots


def test_requests_never_start_before_submission_or_finish_before_first_token() -> None:
    result = simulate(config(rate=3.0, duration_s=12.0))

    assert all(event.service_start_s >= event.submit_s for event in result.requests)
    assert all(event.completion_s >= event.first_token_s for event in result.requests)
