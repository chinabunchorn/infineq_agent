from pathlib import Path

from infineq.detection.detector import Detector
from infineq.detection.policy_v1 import MIN_REQUESTS, TTFT_THRESHOLD_MS
from infineq.evidence.store import EvidenceStore
from infineq.simulator.oracle_writer import build_hidden_oracle
from infineq.simulator.scenarios import scenario_catalog

PROJECT_ROOT = Path(__file__).parents[2]


def test_development_detector_behavior_matches_only_development_oracles() -> None:
    detector = Detector(store=EvidenceStore(project_root=PROJECT_ROOT))
    development = tuple(case for case in scenario_catalog() if case.variant.value == "A")

    for case in development:
        oracle = build_hidden_oracle(case)
        result = detector.run(case.episode_id)
        observed_behavior = (
            "abstain"
            if not result.data_quality.decision_ready
            else "warning"
            if result.packet is not None
            else "no_sustained_incident"
        )

        assert observed_behavior == oracle.expected_detector_behavior, case.episode_id
        if result.packet is not None:
            observed_signals = {signal.signal for signal in result.packet.signals}
            assert set(oracle.required_evidence).issubset(observed_signals), case.episode_id
            assert all(
                reference.evidence_id.startswith(f"ev:{case.episode_id}:")
                for reference in result.packet.evidence_refs
            )
            serialized = str(result.packet.model_dump(mode="json"))
            assert "root_cause" not in serialized
            assert "fault_label" not in serialized
            assert result.detector_crossing_s is not None
            if result.slo_crossing_s is None:
                assert result.slo_lead_time_s is None
            else:
                assert result.slo_lead_time_s == (
                    result.slo_crossing_s - result.detector_crossing_s
                )


def test_development_incidents_have_an_eligible_window_and_evidence_budget() -> None:
    detector = Detector(store=EvidenceStore(project_root=PROJECT_ROOT))

    for case in scenario_catalog():
        if case.variant.value != "A":
            continue
        oracle = build_hidden_oracle(case)
        if oracle.expected_detector_behavior != "warning":
            continue

        result = detector.run(case.episode_id)
        eligible = [
            evaluation
            for evaluation in result.evaluations
            if evaluation.request_count >= MIN_REQUESTS
        ]
        assert eligible, f"{case.episode_id} has no {MIN_REQUESTS}-request evaluation window"

        triggering = next(
            (evaluation for evaluation in eligible if evaluation.decision.triggered), None
        )
        assert triggering is not None, f"{case.episode_id} has no eligible evidence pattern"
        assert result.packet is not None
        assert result.detector_crossing_s == triggering.end_s
        observed_signals = {signal.signal for signal in result.packet.signals}
        assert set(oracle.required_evidence).issubset(observed_signals), case.episode_id


def test_backend_and_prompt_development_incidents_trigger_only_on_consecutive_ttft() -> None:
    detector = Detector(store=EvidenceStore(project_root=PROJECT_ROOT))
    target_families = {"backend_slowdown", "prompt_length_shift"}
    expected_rates = {"backend_slowdown": 1.70, "prompt_length_shift": 1.35}
    expected_prompt_multiplier = 4.50

    for case in scenario_catalog():
        if case.variant.value != "A" or case.family.value not in target_families:
            continue
        assert {phase.rate_per_s for phase in case.simulation_config.workload} == {
            expected_rates[case.family.value]
        }
        if case.family.value == "prompt_length_shift":
            assert case.simulation_config.input_multiplier_profile[0].value == (
                expected_prompt_multiplier
            )

        result = detector.run(case.episode_id)
        triggering = next(
            evaluation for evaluation in result.evaluations if evaluation.decision.triggered
        )
        previous = result.evaluations[result.evaluations.index(triggering) - 1]

        assert triggering.decision.primary_signal == "ttft"
        assert previous.ttft_p95_ms is not None
        assert previous.ttft_p95_ms >= TTFT_THRESHOLD_MS
        assert triggering.ttft_p95_ms is not None
        assert triggering.ttft_p95_ms >= TTFT_THRESHOLD_MS
