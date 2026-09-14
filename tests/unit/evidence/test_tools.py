from pathlib import Path

from infineq.detection.detector import Detector
from infineq.evidence.store import EvidenceStore
from infineq.evidence.tool_schemas import (
    GetDeploymentSnapshotRequest,
    GetEvidenceRequest,
    GetIncidentPacketRequest,
    GetPolicyRequest,
    GetRequestSamplesRequest,
    GetSignalWindowRequest,
    IncidentId,
    PrepareActionPlanRequest,
    RunbookQueryEnum,
    SearchRunbookRequest,
    SignalEnum,
    WindowEnum,
)
from infineq.evidence.tools import InvestigatorTools, VerifierTools
from infineq.schemas.action import ActionType
from infineq.schemas.incident import IncidentPacketV1

PROJECT_ROOT = Path(__file__).parents[3]
INCIDENT_ID: IncidentId = "ep-61d8aa"


def make_tools() -> InvestigatorTools:
    store = EvidenceStore(project_root=PROJECT_ROOT)
    return InvestigatorTools(store=store, detector=Detector(store=store))


def test_get_incident_packet_returns_only_the_typed_detected_packet() -> None:
    packet = make_tools().get_incident_packet(GetIncidentPacketRequest(incident_id=INCIDENT_ID))

    assert isinstance(packet, IncidentPacketV1)
    assert packet.incident_id == INCIDENT_ID
    assert packet.origin.value == "synthetic_replay"
    serialized = str(packet.model_dump(mode="json"))
    assert "root_cause" not in serialized
    assert "fault_label" not in serialized


def test_get_signal_window_returns_only_the_requested_frozen_window() -> None:
    response = make_tools().get_signal_window(
        GetSignalWindowRequest(
            incident_id=INCIDENT_ID,
            signal_enum=SignalEnum.TTFT,
            window_enum=WindowEnum.OBSERVATION,
        )
    )

    assert response.incident_id == INCIDENT_ID
    assert response.signal is SignalEnum.TTFT
    assert response.window is WindowEnum.OBSERVATION
    assert response.evidence
    assert all(item.evidence_id.startswith(f"ev:{INCIDENT_ID}:") for item in response.evidence)
    assert all(item.window_start_s >= 60.0 for item in response.evidence)
    assert all(item.window_end_s <= 75.0 for item in response.evidence)


def test_get_request_samples_caps_results_at_the_requested_twenty_sample_limit() -> None:
    response = make_tools().get_request_samples(
        GetRequestSamplesRequest(
            incident_id=INCIDENT_ID,
            window_enum=WindowEnum.OBSERVATION,
            limit=20,
        )
    )

    assert len(response.samples) == 20
    assert len(response.samples) <= 20
    assert all(60.0 <= sample.submit_s < 75.0 for sample in response.samples)
    assert all(sample.evidence_id.startswith(f"ev:{INCIDENT_ID}:") for sample in response.samples)


def test_get_deployment_snapshot_returns_a_read_only_snapshot_for_the_window() -> None:
    response = make_tools().get_deployment_snapshot(
        GetDeploymentSnapshotRequest(
            incident_id=INCIDENT_ID,
            window_enum=WindowEnum.OBSERVATION,
        )
    )

    assert response.incident_id == INCIDENT_ID
    assert response.window is WindowEnum.OBSERVATION
    assert response.snapshot.replicas == 1
    assert response.snapshot.ready_replicas == 1
    assert response.evidence_id.startswith(f"ev:{INCIDENT_ID}:")


def test_search_runbook_returns_curated_evidence() -> None:
    response = make_tools().search_runbook(
        SearchRunbookRequest(
            query_enum=RunbookQueryEnum.CAPACITY_QUEUEING,
            top_k=3,
        )
    )

    assert response.status.value == "available"
    assert response.placeholder is False
    assert response.results
    assert all(item.query_enum is RunbookQueryEnum.CAPACITY_QUEUEING for item in response.results)
    assert response.top_k == 3


def test_get_evidence_returns_only_indexed_evidence_from_one_episode() -> None:
    tools = make_tools()
    packet = tools.get_incident_packet(GetIncidentPacketRequest(incident_id=INCIDENT_ID))
    evidence_ids = tuple(reference.evidence_id for reference in packet.evidence_refs[:2])

    response = tools.get_evidence(GetEvidenceRequest(evidence_ids=evidence_ids))

    assert response.incident_id == INCIDENT_ID
    assert tuple(item.evidence_id for item in response.evidence) == evidence_ids
    serialized = str(response.model_dump(mode="json"))
    assert "oracle" not in serialized
    assert "root_cause" not in serialized
    assert "filesystem_path" not in serialized


def test_prepare_action_plan_is_a_simulator_only_non_executing_preview() -> None:
    preview = make_tools().prepare_action_plan(
        PrepareActionPlanRequest(
            incident_id=INCIDENT_ID,
            action_type=ActionType.SIMULATED_SCALE_OUT,
        )
    )

    assert preview.action_type is ActionType.SIMULATED_SCALE_OUT
    assert preview.target.kind == "simulator"
    assert preview.requires_human_approval is True
    assert preview.dry_run is True
    assert preview.executed is False


def test_verifier_tools_return_only_the_allowlisted_policy_and_evidence() -> None:
    tools = VerifierTools(store=EvidenceStore(project_root=PROJECT_ROOT))

    policy = tools.get_policy(GetPolicyRequest(policy_ref="policy-infineq-v1"))

    assert policy.policy_ref == "policy-infineq-v1"
    assert policy.allowed_action_types == (ActionType.SIMULATED_SCALE_OUT,)
    assert policy.allowed_target_kind == "simulator"
    assert policy.max_evidence_ids == 20
    assert policy.requires_human_approval is True
    assert policy.execution_allowed is False


def test_verifier_tools_have_no_investigator_or_action_surface() -> None:
    public_methods = {
        name
        for name in dir(VerifierTools)
        if not name.startswith("_") and callable(getattr(VerifierTools, name))
    }

    assert public_methods == {"get_evidence", "get_policy"}


def test_verifier_get_evidence_matches_investigator_evidence_boundary() -> None:
    store = EvidenceStore(project_root=PROJECT_ROOT)
    investigator = InvestigatorTools(store=store)
    verifier = VerifierTools(store=store)
    packet = investigator.get_incident_packet(GetIncidentPacketRequest(incident_id=INCIDENT_ID))
    request = GetEvidenceRequest(
        evidence_ids=tuple(reference.evidence_id for reference in packet.evidence_refs[:2])
    )

    response = verifier.get_evidence(request)

    assert response.incident_id == INCIDENT_ID
    assert tuple(item.evidence_id for item in response.evidence) == request.evidence_ids
