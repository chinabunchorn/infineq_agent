from dataclasses import replace

from infineq.simulator.engine import (
    NumericWindow,
    RatePhase,
    ReplicaChange,
    SimulationConfig,
    simulate,
)


def config(*, rate: float, duration_s: float = 20.0) -> SimulationConfig:
    return SimulationConfig(
        duration_s=duration_s,
        workload=(RatePhase(0.0, duration_s, rate),),
        seed=7,
        jitter_fraction=0.0,
        input_tokens=1,
        output_tokens=3,
        base_itl_s=0.5,
        prefill_base_s=0.1,
        prefill_per_input_s=0.0,
    )


def test_request_lifecycle_invariants_hold_for_replay_with_capacity_change() -> None:
    result = simulate(
        replace(
            config(rate=8.0, duration_s=18.0),
            replica_changes=(ReplicaChange(timestamp_s=6.0, replicas=2),),
        )
    )

    request_ids = [event.request_id for event in result.requests]
    assert request_ids == sorted(request_ids)
    assert len(request_ids) == len(set(request_ids))
    assert all(event.queue_wait_s >= 0 for event in result.requests)
    assert all(event.ttft_s >= event.prefill_s for event in result.requests)
    assert all(event.end_to_end_s >= event.ttft_s for event in result.requests)


def test_readiness_loss_disables_new_slots_until_readiness_returns() -> None:
    result = simulate(
        replace(
            config(rate=4.0, duration_s=20.0),
            replica_changes=(
                ReplicaChange(timestamp_s=5.0, replicas=1, ready_replicas=0),
                ReplicaChange(timestamp_s=10.0, replicas=1, ready_replicas=1),
            ),
        )
    )

    assert result.ready_replicas_at(7.0) == 0
    assert result.ready_replicas_at(12.0) == 1
    assert any(event.service_start_s >= 10.0 for event in result.requests)


def test_numeric_profiles_change_observed_timing_without_changing_queue_formula() -> None:
    result = simulate(
        replace(
            config(rate=1.0, duration_s=12.0),
            itl_profile=(NumericWindow(5.0, 12.0, 2.0),),
        )
    )

    before = [event for event in result.requests if event.submit_s < 5.0]
    after = [event for event in result.requests if event.submit_s >= 5.0]
    assert before and after
    assert max(event.itl_s for event in before) < min(event.itl_s for event in after)
    assert all(
        abs(event.ttft_s - event.queue_wait_s - event.prefill_s) < 1e-9 for event in result.requests
    )
