from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from infineq.agents.tool_loop import ToolCallTrace
from infineq.detection.detector import Detector
from infineq.evaluation import live_baselines
from infineq.evaluation.baselines import DevelopmentOracle, ResourceBudget
from infineq.evidence.store import EvidenceStore
from infineq.evidence.tool_schemas import GetPolicyRequest, PolicyRef, PolicyResponse
from infineq.evidence.tools import VerifierTools
from infineq.schemas.incident import IncidentPacketV1
from infineq.schemas.investigation import (
    Disposition,
    EvidenceCoverage,
    Hypothesis,
    IncidentFamily,
    InvestigationResultV1,
)
from infineq.schemas.verification import VerificationResultV1, VerificationStatus
from infineq.workflow.corrections import CorrectionPacketV1
from infineq.workflow.transitions import WorkflowState

PROJECT_ROOT = Path(__file__).parents[3]
INCIDENT_ID = "ep-61d8aa"
POLICY_REF = "policy-infineq-v1"

LIVE_BUDGET = ResourceBudget(
    budget_id="budget-quality-gate-v1",
    max_visible_evidence=20,
    max_successful_tool_calls=8,
    max_redundant_tool_calls=3,
    max_runbook_chunks=3,
    max_input_tokens=50_000,
    max_output_tokens=10_000,
    max_total_tokens=60_000,
    max_latency_ms=120_000.0,
)


class GoodInvestigator:
    def __init__(self, result: InvestigationResultV1) -> None:
        self.result = result

    def investigate(
        self,
        packet: IncidentPacketV1,
        *,
        correction_packet: CorrectionPacketV1 | None = None,
    ) -> InvestigationResultV1:
        del packet, correction_packet
        return self.result


class InvalidResultVerifier:
    def verify(
        self,
        packet: IncidentPacketV1,
        investigation: InvestigationResultV1,
    ) -> Any:
        del packet, investigation
        return {"status": "verified", "untrusted": True}


class BlockedVerifier:
    def verify(
        self,
        packet: IncidentPacketV1,
        investigation: InvestigationResultV1,
    ) -> VerificationResultV1:
        return VerificationResultV1(
            verification_id=f"verification-{packet.incident_id}",
            incident_id=packet.incident_id,
            investigation_id=investigation.investigation_id,
            checked_at=packet.detected_at,
            status=VerificationStatus.BLOCKED,
            claim_checks=(),
            issues=("deterministic test block",),
            correction_requests=(),
            policy_ref=POLICY_REF,
        )


def canonical_packet() -> IncidentPacketV1:
    packet = Detector(store=EvidenceStore(project_root=PROJECT_ROOT)).detect(INCIDENT_ID)
    assert packet is not None
    return packet


def diagnosed_investigation(packet: IncidentPacketV1) -> InvestigationResultV1:
    evidence_ids = tuple(item.evidence_id for item in packet.evidence_refs[:2])
    return InvestigationResultV1(
        investigation_id=f"investigation-{packet.incident_id}",
        incident_id=packet.incident_id,
        completed_at=packet.detected_at,
        disposition=Disposition.DIAGNOSED,
        summary=(
            "The comparison covers capacity_queueing, backend_slowdown, "
            "workload_shape_change, and replica_or_deployment_regression."
        ),
        hypotheses=(
            Hypothesis(
                hypothesis_id="hyp-leading",
                family=IncidentFamily.CAPACITY_QUEUEING,
                rank=1,
                evidence_coverage=EvidenceCoverage.COMPLETE,
                statement="Capacity queueing is supported.",
                supporting_evidence_ids=(evidence_ids[0],),
                contradicting_evidence_ids=(),
                missing_evidence=(),
            ),
            Hypothesis(
                hypothesis_id="hyp-alternative",
                family=IncidentFamily.BACKEND_SLOWDOWN,
                rank=2,
                evidence_coverage=EvidenceCoverage.PARTIAL,
                statement="Backend slowdown remains possible.",
                supporting_evidence_ids=(),
                contradicting_evidence_ids=(evidence_ids[1],),
                missing_evidence=("stage timing",),
            ),
        ),
        leading_hypothesis_id="hyp-leading",
        proposed_action=None,
        cited_evidence_ids=evidence_ids,
        limitations=("stage timing was not directly observed.",),
    )


def policy_for() -> PolicyResponse:
    return VerifierTools(store=EvidenceStore(project_root=PROJECT_ROOT)).get_policy(
        GetPolicyRequest(policy_ref=PolicyRef.INFINEQ_V1)
    )


def unscored_execution(tmp_path: Path, suffix: str):
    packet = canonical_packet()
    return live_baselines.run_matched_live_modes(
        project_root=tmp_path,
        run_id=f"run-baseline-quality-{suffix.replace('_', '-')}",
        packet=packet,
        investigator=GoodInvestigator(diagnosed_investigation(packet)),
        verifier=BlockedVerifier(),
        policy=policy_for(),
        budget=LIVE_BUDGET,
        score_results=False,
    )


def test_invalid_verifier_result_is_preserved_as_a_typed_failure(tmp_path: Path) -> None:
    packet = canonical_packet()
    investigation = diagnosed_investigation(packet)

    execution = live_baselines.run_matched_live_modes(
        project_root=tmp_path,
        run_id="run-baseline-quality-invalid-verifier",
        packet=packet,
        investigator=GoodInvestigator(investigation),
        verifier=InvalidResultVerifier(),
        policy=policy_for(),
        budget=LIVE_BUDGET,
        score_results=False,
    )

    assert execution.workflow.final_state is WorkflowState.ANALYSIS_INCOMPLETE
    assert execution.workflow.failure is not None
    assert execution.workflow.failure.value == "invalid_schema"
    failure = json.loads((execution.output_directory / "failure.json").read_text())
    assert failure == {
        "action_executed": False,
        "agent_invoked": True,
        "component": "verifier",
        "failure": "invalid_schema",
        "schema_version": "live-failure-v1",
    }


def test_all_development_detector_failures_are_preserved_without_agent_calls(
    tmp_path: Path,
) -> None:
    class DetectorAlwaysFails:
        def run(self, episode_id: str) -> object:
            raise RuntimeError(f"detector failed for {episode_id}; token=secret")

    execution = live_baselines.run_all_development_live_modes(
        project_root=tmp_path,
        run_id="run-phase5-development-live-v1",
        detector=DetectorAlwaysFails(),  # type: ignore[arg-type]
        investigator=object(),  # type: ignore[arg-type]
        verifier=object(),  # type: ignore[arg-type]
        policy=policy_for(),
        oracle_loader=lambda episode_id: DevelopmentOracle(
            episode_id=episode_id,
            expected_diagnosis=None,
            required_evidence_kinds=(),
            action_card_expected=False,
        ),
    )

    assert len(execution.episode_ids) == 8
    assert len(execution.runs) == 24
    assert execution.evaluation.two_agent_justification_passed is False
    assert execution.aggregate_metrics["mode_record_count"] == 24
    assert all(
        getattr(workflow.failure, "value", None) == "invalid_schema"
        for workflow in execution.workflows
    )

    failure = json.loads(
        (
            execution.output_directory / "episodes" / execution.episode_ids[0] / "failure.json"
        ).read_text(encoding="utf-8")
    )
    assert failure == {
        "action_executed": False,
        "agent_invoked": False,
        "component": "detector",
        "failure": "detector_unavailable",
        "schema_version": "live-failure-v1",
    }
    serialized = "\n".join(
        path.read_text(encoding="utf-8")
        for path in execution.output_directory.rglob("*")
        if path.is_file()
    )
    assert "secret" not in serialized
    assert '"prompt":' not in serialized.casefold()
    assert '"endpoint":' not in serialized.casefold()


@pytest.mark.parametrize(
    "tamper",
    [
        "absolute_path",
        "escaped_path",
        "missing_artifact",
        "missing_modes",
        "bad_audit",
        "forbidden_body",
        "bad_baseline_id",
        "bad_baseline_modes",
        "bad_accounting",
        "bad_trace_reference",
        "bad_version_pin",
        "missing_evaluation",
        "wrong_nested_directory",
    ],
)
def test_live_artifact_validator_rejects_containment_and_accounting_tampering(
    tmp_path: Path,
    tamper: str,
) -> None:
    execution = unscored_execution(tmp_path, tamper)
    output = execution.output_directory
    manifest_path = output / "live_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    if tamper == "absolute_path":
        manifest["baseline_results_path"] = "/tmp/outside.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif tamper == "escaped_path":
        manifest["baseline_results_path"] = "../outside.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif tamper == "missing_artifact":
        (output / "baseline_results.json").unlink()
    elif tamper == "missing_modes":
        manifest["modes"].pop("investigator")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif tamper == "bad_audit":
        (output / "workflow_audit.json").write_text(
            json.dumps([{"actor": "executor", "to_state": "ACTION_APPLIED"}]),
            encoding="utf-8",
        )
    elif tamper == "forbidden_body":
        (output / "forbidden.json").write_text(
            json.dumps({"prompt": "do not persist this"}),
            encoding="utf-8",
        )
    elif tamper == "bad_baseline_id":
        baseline_path = output / "baseline_results.json"
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        baseline["run_id"] = "run-other"
        baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    elif tamper == "bad_baseline_modes":
        baseline_path = output / "baseline_results.json"
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        baseline["runs"] = baseline["runs"][:-1]
        baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    elif tamper == "bad_accounting":
        accounting_path = output / "investigator_verifier" / "accounting.json"
        accounting = json.loads(accounting_path.read_text(encoding="utf-8"))
        accounting["investigator_invocation_count"] = -1
        accounting_path.write_text(json.dumps(accounting), encoding="utf-8")
    elif tamper == "bad_trace_reference":
        manifest["modes"]["investigator"]["response_ids"] = ["not-a-response"]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif tamper == "bad_version_pin":
        baseline_path = output / "baseline_results.json"
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        baseline["runs"][1]["versions"]["agent_version"] = "wrong"
        baseline_path.write_text(json.dumps(baseline), encoding="utf-8")

    with pytest.raises(live_baselines.LiveArtifactError):
        live_baselines.validate_live_artifacts(
            output,
            execution.run_id,
            require_evaluation=tamper == "missing_evaluation",
            _nested=tamper == "wrong_nested_directory",
            expected_episode_id="ep-other1" if tamper == "wrong_nested_directory" else None,
        )


def test_live_packetless_boundaries_and_safe_serialization_are_deterministic() -> None:
    detector = Detector(store=EvidenceStore(project_root=PROJECT_ROOT))
    packetless_ids = ("ep-a91e7c", "ep-e35192")
    dispositions = tuple(
        live_baselines._packetless_investigation(detector.run(episode_id)).disposition
        for episode_id in packetless_ids
    )
    assert dispositions == (Disposition.NO_INCIDENT, Disposition.INDETERMINATE)
    assert live_baselines.baseline_episode_from_detection(
        detector.run(INCIDENT_ID)
    ).visible_evidence
    assert not live_baselines.baseline_episode_from_detection(
        detector.run("ep-a91e7c")
    ).visible_evidence
    with pytest.raises(live_baselines.LivePrerequisiteError, match="packetless"):
        live_baselines._packetless_investigation(detector.run(INCIDENT_ID))

    assert live_baselines._safe_usage(None) == (None, None, None)
    assert live_baselines._safe_usage((1, -1, 3)) == (1, None, 3)
    assert live_baselines._safe_usage(
        SimpleNamespace(input_tokens=1, output_tokens=2, total_tokens=3)
    ) == (
        1,
        2,
        3,
    )
    trace = ToolCallTrace("resp-1", "call-1", "get_evidence", "ok", 0)
    assert live_baselines._trace_record(trace)["response_id"] == "resp-1"
    assert live_baselines._trace_record({"name": "get_policy", "prompt": "secret"}) == {
        "name": "get_policy"
    }
    assert live_baselines._trace_record(object()) == {"status": "trace_unavailable"}
    safe = live_baselines._safe_json_text(
        json.dumps({"prompt": "secret", "nested": ["https://example.invalid", "ok"]})
    )
    assert "secret" not in safe
    assert '"nested"' in safe
    assert live_baselines._safe_json_value("") == {"output": ""}
