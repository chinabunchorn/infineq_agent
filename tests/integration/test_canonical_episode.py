from statistics import median

from infineq.simulator.engine import (
    nominal_capacity_per_second,
    run_canonical_episode,
)


def test_canonical_nominal_capacity_and_queue_signal_separation() -> None:
    result = run_canonical_episode()
    baseline = [event for event in result.requests if 45.0 <= event.submit_s < 60.0]
    incident = [event for event in result.requests if 105.0 <= event.submit_s < 120.0]

    assert 2.30 < nominal_capacity_per_second() < 2.34
    assert 4.60 < nominal_capacity_per_second(replicas=2) < 4.68
    assert median(event.queue_wait_s for event in incident) > median(
        event.queue_wait_s for event in baseline
    )
    assert median(event.ttft_s for event in incident) > median(event.ttft_s for event in baseline)
    assert {event.itl_s for event in baseline} == {event.itl_s for event in incident}


def test_approved_canonical_scale_out_drains_queue_and_meets_recovery_rule() -> None:
    result = run_canonical_episode(approved_at_s=120.0)

    assert result.replica_snapshots[-1].ready_replicas == 2
    assessment = result.recovery_assessment()
    assert assessment.criteria_met
    assert assessment.p95_ttft_ms <= 2_000.0
    assert all(depth <= 2 for depth in assessment.queue_depth_checks)


def test_rejected_canonical_path_does_not_claim_recovery() -> None:
    result = run_canonical_episode(approved_at_s=None)

    assert max(snapshot.ready_replicas for snapshot in result.replica_snapshots) == 1
    assert not result.recovery_assessment().criteria_met
