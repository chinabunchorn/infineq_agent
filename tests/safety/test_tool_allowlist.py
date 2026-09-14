"""Adversarial tests for the fixed investigator and verifier tool boundary."""

import inspect
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from infineq.errors import DataMissingError, ToolPolicyDeniedError
from infineq.evidence.store import EvidenceStore
from infineq.evidence.tool_schemas import (
    DeploymentSnapshotResponse,
    GetDeploymentSnapshotRequest,
    GetEvidenceRequest,
    GetIncidentPacketRequest,
    GetPolicyRequest,
    GetRequestSamplesRequest,
    GetSignalWindowRequest,
    PrepareActionPlanRequest,
    RunbookQueryEnum,
    SearchRunbookRequest,
    SignalEnum,
    WindowEnum,
)
from infineq.evidence.tools import InvestigatorTools, VerifierTools
from infineq.schemas.action import ActionType
from infineq.schemas.incident import DeploymentSnapshot

PROJECT_ROOT = Path(__file__).parents[2]
INCIDENT_ID = "ep-61d8aa"
OTHER_INCIDENT_ID = "ep-a91e7c"


def make_investigator() -> InvestigatorTools:
    return InvestigatorTools(store=EvidenceStore(project_root=PROJECT_ROOT))


def make_verifier() -> VerifierTools:
    return VerifierTools(store=EvidenceStore(project_root=PROJECT_ROOT))


def test_request_schemas_reject_unknown_signal_window_and_action_enums() -> None:
    with pytest.raises(ValidationError):
        GetSignalWindowRequest(
            incident_id=INCIDENT_ID,
            signal_enum="unknown_signal",
            window_enum=WindowEnum.OBSERVATION,
        )
    with pytest.raises(ValidationError):
        GetSignalWindowRequest(
            incident_id=INCIDENT_ID,
            signal_enum=SignalEnum.TTFT,
            window_enum="rolling_15m",
        )
    with pytest.raises(ValidationError):
        PrepareActionPlanRequest(
            incident_id=INCIDENT_ID,
            action_type="scale_real_cluster",
        )


def test_request_schemas_reject_limits_above_the_frozen_bounds() -> None:
    store = EvidenceStore(project_root=PROJECT_ROOT)
    evidence_ids = tuple(
        item.evidence_id for item in store.load_episode(INCIDENT_ID).evidence_index[:21]
    )

    with pytest.raises(ValidationError):
        GetRequestSamplesRequest(
            incident_id=INCIDENT_ID,
            window_enum=WindowEnum.OBSERVATION,
            limit=21,
        )
    with pytest.raises(ValidationError):
        GetEvidenceRequest(evidence_ids=evidence_ids)
    with pytest.raises(ValidationError):
        SearchRunbookRequest(query_enum=RunbookQueryEnum.CAPACITY_QUEUEING, top_k=4)


@pytest.mark.parametrize(
    "value",
    [
        "../hidden_oracles",
        "../../etc/passwd",
        "/etc/passwd",
        "https://example.test/secret",
        "$(whoami)",
        "; rm -rf /",
        "ep-61d8aa && touch pwned",
    ],
)
def test_tool_request_schemas_reject_traversal_urls_and_shell_metacharacters(
    value: str,
) -> None:
    with pytest.raises(ValidationError):
        GetIncidentPacketRequest(incident_id=value)

    with pytest.raises(ValidationError):
        GetPolicyRequest(policy_ref=value)


@pytest.mark.parametrize(
    "value",
    [
        "ev:ep-61d8aa:service_metrics:w000-015:ttft:p95:deadbeef/../../secret",
        "https://example.test/evidence",
        "ev:ep-61d8aa:service_metrics:w000-015:ttft:p95:deadbeef;id",
        "$(cat /etc/passwd)",
    ],
)
def test_evidence_ids_reject_traversal_urls_and_shell_text(value: str) -> None:
    with pytest.raises(ValidationError):
        GetEvidenceRequest(evidence_ids=(value,))


def test_runbook_accepts_only_a_curated_query_enum_not_arbitrary_text() -> None:
    with pytest.raises(ValidationError):
        SearchRunbookRequest(query_enum="what happened to the cluster?", top_k=1)

    response = make_investigator().search_runbook(
        SearchRunbookRequest(query_enum=RunbookQueryEnum.CAPACITY_QUEUEING, top_k=3)
    )
    assert response.placeholder is False
    assert response.status.value == "available"
    assert response.results
    assert all(item.query_enum is RunbookQueryEnum.CAPACITY_QUEUEING for item in response.results)


def test_both_tool_surfaces_reject_evidence_from_two_episodes() -> None:
    store = EvidenceStore(project_root=PROJECT_ROOT)
    first = store.load_episode(INCIDENT_ID).evidence_index[0].evidence_id
    second = store.load_episode(OTHER_INCIDENT_ID).evidence_index[0].evidence_id
    request = GetEvidenceRequest(evidence_ids=(first, second))

    with pytest.raises(ToolPolicyDeniedError):
        make_investigator().get_evidence(request)
    with pytest.raises(ToolPolicyDeniedError):
        make_verifier().get_evidence(request)


def test_verifier_policy_is_fixed_and_unknown_policy_refs_are_rejected() -> None:
    policy = make_verifier().get_policy(GetPolicyRequest(policy_ref="policy-infineq-v1"))

    assert policy.policy_ref == "policy-infineq-v1"
    assert policy.allowed_action_types == (ActionType.SIMULATED_SCALE_OUT,)
    assert policy.allowed_target_kind == "simulator"
    assert policy.from_replicas == 1
    assert policy.to_replicas == 2
    assert policy.action_ttl_seconds == 300
    assert policy.requires_human_approval is True
    assert policy.execution_allowed is False

    with pytest.raises(ValidationError):
        GetPolicyRequest(policy_ref="policy-unknown")

    with pytest.raises(ValidationError):
        GetIncidentPacketRequest.model_validate(
            {"incident_id": INCIDENT_ID, "oracle_root": "/hidden"}
        )
    with pytest.raises(ValidationError):
        GetPolicyRequest.model_validate(
            {"policy_ref": "policy-infineq-v1", "filesystem_path": "/etc/passwd"}
        )


def test_public_responses_do_not_contain_oracle_fields_or_filesystem_paths() -> None:
    investigator = make_investigator()
    packet = investigator.get_incident_packet(GetIncidentPacketRequest(incident_id=INCIDENT_ID))
    evidence_ids = tuple(reference.evidence_id for reference in packet.evidence_refs[:2])
    responses = (
        packet,
        investigator.get_signal_window(
            GetSignalWindowRequest(
                incident_id=INCIDENT_ID,
                signal_enum=SignalEnum.TTFT,
                window_enum=WindowEnum.OBSERVATION,
            )
        ),
        investigator.get_request_samples(
            GetRequestSamplesRequest(
                incident_id=INCIDENT_ID,
                window_enum=WindowEnum.OBSERVATION,
                limit=20,
            )
        ),
        investigator.get_deployment_snapshot(
            GetDeploymentSnapshotRequest(
                incident_id=INCIDENT_ID,
                window_enum=WindowEnum.OBSERVATION,
            )
        ),
        investigator.search_runbook(
            SearchRunbookRequest(query_enum=RunbookQueryEnum.CAPACITY_QUEUEING, top_k=3)
        ),
        investigator.get_evidence(GetEvidenceRequest(evidence_ids=evidence_ids)),
        investigator.prepare_action_plan(
            PrepareActionPlanRequest(
                incident_id=INCIDENT_ID,
                action_type=ActionType.SIMULATED_SCALE_OUT,
            )
        ),
        make_verifier().get_policy(GetPolicyRequest(policy_ref="policy-infineq-v1")),
        make_verifier().get_evidence(GetEvidenceRequest(evidence_ids=evidence_ids)),
    )
    serialized = json.dumps([response.model_dump(mode="json") for response in responses])
    lowered = serialized.lower()

    for forbidden in (
        "oracle",
        "root_cause",
        "fault_label",
        "scenario_family",
        "mechanism",
        "filesystem_path",
        "artifact_path",
        "hidden_oracles",
    ):
        assert forbidden not in lowered
    assert "/users/" not in lowered
    assert "/etc/" not in lowered


def test_evidence_record_boundary_rejects_oracle_fields_and_paths() -> None:
    from datetime import UTC, datetime

    from infineq.evidence.tool_schemas import EvidenceRecord

    base = {
        "evidence_id": "ev:ep-61d8aa:service_metrics:w000-015:ttft:p95:deadbeef",
        "incident_id": INCIDENT_ID,
        "source": "service_metrics",
        "signal": SignalEnum.TTFT,
        "aggregation": "p95",
        "unit": "ms",
        "window_start_s": 0.0,
        "window_end_s": 15.0,
        "freshness_s": 0.0,
        "observed_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    for value in (
        {"root_cause": "capacity_queueing"},
        {"oracle": {"mechanism": "hidden"}},
        {"filesystem_path": "/etc/passwd"},
    ):
        with pytest.raises(ValidationError):
            EvidenceRecord(**base, value=value)

    for field, value in (
        ("source", "/etc/passwd"),
        ("aggregation", "../secret"),
        ("unit", "https://example.test"),
    ):
        with pytest.raises(ValidationError):
            EvidenceRecord(**{**base, field: value}, value=1.0)


def test_deployment_snapshot_response_rejects_a_filesystem_revision() -> None:
    from datetime import UTC, datetime

    snapshot = DeploymentSnapshot(
        replicas=1,
        ready_replicas=1,
        revision="/etc/passwd",
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        evidence_ids=("ev:ep-61d8aa:infrastructure_events:t000:replicas:snapshot:deadbeef",),
    )

    with pytest.raises(ValidationError):
        DeploymentSnapshotResponse(
            incident_id=INCIDENT_ID,
            window=WindowEnum.OBSERVATION,
            snapshot=snapshot,
            evidence_id=snapshot.evidence_ids[0],
        )


def test_protected_evidence_is_rejected_without_disclosing_the_value() -> None:
    store = EvidenceStore(project_root=PROJECT_ROOT)
    entry = store.load_episode(INCIDENT_ID).evidence_index[0]
    poisoned_entry = entry.model_copy(update={"value": {"filesystem_path": "/etc/passwd"}})
    request = GetEvidenceRequest(evidence_ids=(entry.evidence_id,))

    with (
        patch.object(store, "get_evidence", return_value=poisoned_entry),
        pytest.raises(DataMissingError) as error,
    ):
        InvestigatorTools(store=store).get_evidence(request)

    assert "/etc/passwd" not in str(error.value)


def test_action_preview_is_explicitly_non_executing_and_never_calls_a_process() -> None:
    investigator = make_investigator()

    with patch("subprocess.run", side_effect=AssertionError("process execution forbidden")):
        preview = investigator.prepare_action_plan(
            PrepareActionPlanRequest(
                incident_id=INCIDENT_ID,
                action_type=ActionType.SIMULATED_SCALE_OUT,
            )
        )

    assert preview.dry_run is True
    assert preview.executed is False
    assert preview.execution_allowed is False
    assert preview.target.kind == "simulator"
    assert not hasattr(investigator, "execute_action")


def test_verifier_surface_contains_only_two_read_methods() -> None:
    public_methods = {
        name
        for name, member in inspect.getmembers(VerifierTools, predicate=inspect.isfunction)
        if not name.startswith("_")
    }

    assert public_methods == {"get_evidence", "get_policy"}
    assert not any("action" in name or "investigator" in name for name in public_methods)
