from infineq.detection.policy_v1 import (
    ERROR_RATE_THRESHOLD,
    MIN_REQUESTS,
    PolicyObservation,
    evaluate_policy,
)


def test_policy_requires_two_consecutive_high_ttft_evaluations() -> None:
    previous = PolicyObservation(
        end_s=70.0,
        request_count=84,
        ttft_p95_ms=1_100.0,
        error_rate=0.0,
        corroborating_signals=("queue_depth",),
    )
    current = PolicyObservation(
        end_s=75.0,
        request_count=102,
        ttft_p95_ms=1_200.0,
        error_rate=0.0,
        corroborating_signals=("queue_depth",),
    )

    decision = evaluate_policy(current, previous=previous)

    assert decision.triggered is True
    assert decision.primary_signal == "ttft"
    assert decision.corroborating_signals == ("queue_depth",)


def test_policy_rejects_a_primary_signal_without_second_signal_corroboration() -> None:
    previous = PolicyObservation(
        end_s=70.0,
        request_count=84,
        ttft_p95_ms=1_100.0,
        error_rate=0.0,
        corroborating_signals=(),
    )
    current = PolicyObservation(
        end_s=75.0,
        request_count=102,
        ttft_p95_ms=1_200.0,
        error_rate=0.0,
        corroborating_signals=(),
    )

    decision = evaluate_policy(current, previous=previous)

    assert decision.triggered is False


def test_policy_does_not_count_the_primary_signal_as_corroboration() -> None:
    previous = PolicyObservation(
        end_s=70.0,
        request_count=84,
        ttft_p95_ms=1_100.0,
        error_rate=0.0,
        corroborating_signals=("ttft",),
    )
    current = PolicyObservation(
        end_s=75.0,
        request_count=102,
        ttft_p95_ms=1_200.0,
        error_rate=0.0,
        corroborating_signals=("ttft",),
    )

    decision = evaluate_policy(current, previous=previous)

    assert decision.triggered is False


def test_error_rate_threshold_triggers_on_one_quality_ready_evaluation() -> None:
    current = PolicyObservation(
        end_s=80.0,
        request_count=80,
        ttft_p95_ms=300.0,
        error_rate=ERROR_RATE_THRESHOLD,
        corroborating_signals=("request_outcome",),
    )

    decision = evaluate_policy(current)

    assert decision.triggered is True
    assert decision.primary_signal == "error_rate"


def test_itl_corroboration_cannot_trigger_without_frozen_primary() -> None:
    previous = PolicyObservation(
        end_s=70.0,
        request_count=84,
        ttft_p95_ms=320.0,
        error_rate=0.0,
        corroborating_signals=("itl",),
    )
    current = PolicyObservation(
        end_s=75.0,
        request_count=102,
        ttft_p95_ms=320.0,
        error_rate=0.0,
        corroborating_signals=("itl",),
    )

    decision = evaluate_policy(current, previous=previous)

    assert decision.triggered is False


def test_detector_emits_a_schema_valid_queue_packet_with_measured_slo_timing() -> None:
    from pathlib import Path

    from infineq.detection.detector import Detector
    from infineq.evidence.store import EvidenceStore
    from infineq.schemas.incident import IncidentPacketV1

    project_root = Path(__file__).parents[3]
    result = Detector(store=EvidenceStore(project_root=project_root)).run("ep-61d8aa")

    assert isinstance(result.packet, IncidentPacketV1)
    assert result.packet.incident_id == "ep-61d8aa"
    assert result.packet.detector_version == "policy-v1"
    assert result.detector_crossing_s is not None
    assert result.slo_crossing_s is not None
    assert result.slo_lead_time_s == result.slo_crossing_s - result.detector_crossing_s
    serialized = str(result.packet.model_dump(mode="json"))
    assert "root_cause" not in serialized
    assert "fault_label" not in serialized


def test_policy_does_not_trigger_before_the_minimum_request_gate() -> None:
    current = PolicyObservation(
        end_s=20.0,
        request_count=MIN_REQUESTS - 1,
        ttft_p95_ms=1_500.0,
        error_rate=ERROR_RATE_THRESHOLD,
        corroborating_signals=("queue_depth",),
    )

    decision = evaluate_policy(current)

    assert decision.triggered is False


def test_detector_evaluates_every_five_seconds_over_fifteen_second_windows() -> None:
    from pathlib import Path

    from infineq.detection.detector import Detector
    from infineq.evidence.store import EvidenceStore

    project_root = Path(__file__).parents[3]
    result = Detector(store=EvidenceStore(project_root=project_root)).run("ep-61d8aa")

    assert result.evaluations
    assert all(
        evaluation.observation_start_s == max(0.0, evaluation.end_s - 15.0)
        for evaluation in result.evaluations
    )
    assert all(
        right.end_s - left.end_s == 5.0
        for left, right in zip(result.evaluations, result.evaluations[1:], strict=False)
    )


def test_detector_abstains_for_missing_or_stale_required_telemetry() -> None:
    from pathlib import Path

    from infineq.detection.detector import Detector
    from infineq.detection.quality import QualityIssueCode
    from infineq.evidence.store import EvidenceStore

    project_root = Path(__file__).parents[3]
    result = Detector(store=EvidenceStore(project_root=project_root)).run("ep-e35192")

    assert result.packet is None
    assert result.decision == "abstain"
    assert any(
        failure.code in {QualityIssueCode.DATA_MISSING, QualityIssueCode.FRESHNESS}
        for failure in result.data_quality.failures
    )


def test_detector_counter_reset_cannot_become_an_incident() -> None:
    from dataclasses import replace
    from pathlib import Path

    from infineq.detection.detector import Detector
    from infineq.detection.quality import QualityIssueCode
    from infineq.evidence.store import EvidenceStore

    project_root = Path(__file__).parents[3]
    episode = EvidenceStore(project_root=project_root).load_episode("ep-61d8aa")
    records = list(episode.service_metrics)
    records[0] = {**records[0], "counter_reset": True}
    altered = replace(episode, service_metrics=tuple(records))

    class MemoryStore:
        def load_episode(self, _episode_id: str):
            return altered

    result = Detector(store=MemoryStore()).run("ep-61d8aa")

    assert result.packet is None
    assert any(
        failure.code is QualityIssueCode.COUNTER_RESET for failure in result.data_quality.failures
    )


def test_detector_counts_requests_only_inside_the_trailing_half_open_window() -> None:
    from pathlib import Path

    from infineq.detection.detector import Detector
    from infineq.evidence.store import EvidenceStore

    project_root = Path(__file__).parents[3]
    store = EvidenceStore(project_root=project_root)
    episode = store.load_episode("ep-a91e7c")
    result = Detector(store=store).run("ep-a91e7c")

    evaluation = next(item for item in result.evaluations if item.end_s == 20.0)
    expected_count = sum(
        5.0 <= record["submit_s"] < 20.0
        for record in episode.requests
        if isinstance(record.get("submit_s"), (int, float))
    )

    assert expected_count == 18
    assert evaluation.request_count == expected_count
