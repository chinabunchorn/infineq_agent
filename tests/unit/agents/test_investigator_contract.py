from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from infineq.agents.investigator import (
    FROZEN_INCIDENT_FAMILIES,
    Investigator,
    validate_investigation_result,
)
from infineq.detection.detector import Detector
from infineq.evidence.store import EvidenceStore
from infineq.schemas.action import ActionPlanV1, ActionType
from infineq.schemas.incident import IncidentPacketV1
from infineq.schemas.investigation import (
    Disposition,
    InvestigationResultV1,
)

PROJECT_ROOT = Path(__file__).parents[3]
FIXTURE_ROOT = PROJECT_ROOT / "tests" / "fixtures" / "agent_outputs" / "investigator"


def canonical_packet() -> IncidentPacketV1:
    packet = Detector(store=EvidenceStore(project_root=PROJECT_ROOT)).detect("ep-61d8aa")
    assert packet is not None
    return packet


def valid_result(packet: IncidentPacketV1) -> dict[str, Any]:
    evidence_ids = tuple(item.evidence_id for item in packet.evidence_refs[:3])
    hypotheses = [
        {
            "hypothesis_id": "hyp-capacity-queueing",
            "family": "capacity_queueing",
            "rank": 1,
            "evidence_coverage": "complete",
            "statement": "Capacity queueing is the leading supported explanation.",
            "supporting_evidence_ids": [evidence_ids[0]],
            "contradicting_evidence_ids": [],
            "missing_evidence": [],
            "next_check": None,
        },
        {
            "hypothesis_id": "hyp-backend-slowdown",
            "family": "backend_slowdown",
            "rank": 2,
            "evidence_coverage": "partial",
            "statement": "Backend slowdown is less supported by the available measurements.",
            "supporting_evidence_ids": [],
            "contradicting_evidence_ids": [evidence_ids[1]],
            "missing_evidence": ["per-stage execution breakdown"],
            "next_check": "Inspect the stage timing window.",
        },
        {
            "hypothesis_id": "hyp-workload-shape-change",
            "family": "workload_shape_change",
            "rank": 3,
            "evidence_coverage": "partial",
            "statement": "Workload-shape change remains an alternative to check.",
            "supporting_evidence_ids": [],
            "contradicting_evidence_ids": [evidence_ids[2]],
            "missing_evidence": ["token-distribution comparison"],
            "next_check": "Compare input-token distributions.",
        },
    ]
    return {
        "investigation_id": "investigation-ep-61d8aa",
        "incident_id": packet.incident_id,
        "completed_at": datetime.now(UTC).isoformat(),
        "disposition": "diagnosed",
        "summary": (
            "The packet supports capacity queueing; replica or deployment regression is excluded "
            "because the deployment evidence is stable."
        ),
        "hypotheses": hypotheses,
        "leading_hypothesis_id": "hyp-capacity-queueing",
        "proposed_action": None,
        "cited_evidence_ids": list(evidence_ids),
        "limitations": [
            "replica_or_deployment_regression is excluded by stable deployment evidence",
        ],
    }


def test_investigator_accepts_only_an_incident_packet() -> None:
    investigator = Investigator(client=object(), tool_executor=object())

    with pytest.raises(TypeError, match="IncidentPacketV1"):
        investigator.investigate({"incident_id": "ep-61d8aa"})  # type: ignore[arg-type]


def test_valid_investigation_compares_the_frozen_families_and_is_schema_compatible() -> None:
    packet = canonical_packet()
    payload = valid_result(packet)

    result = validate_investigation_result(
        payload,
        packet=packet,
        returned_evidence_ids=set(payload["cited_evidence_ids"]),
    )

    assert isinstance(result, InvestigationResultV1)
    assert result.disposition is Disposition.DIAGNOSED
    assert len(result.hypotheses) <= 3
    assert set(FROZEN_INCIDENT_FAMILIES) == {
        "capacity_queueing",
        "backend_slowdown",
        "workload_shape_change",
        "replica_or_deployment_regression",
    }
    assert any("replica_or_deployment_regression" in text for text in result.limitations)


def test_invalid_fixtures_cover_unseen_ids_missing_alternatives_free_action_and_extra_fields() -> (
    None
):
    packet = canonical_packet()
    returned_ids = {item.evidence_id for item in packet.evidence_refs}

    for fixture_name in (
        "fabricated_evidence.json",
        "missing_fourth_family.json",
        "free_form_action.json",
        "extra_field.json",
        "overconfident_causal_wording.json",
    ):
        payload = json.loads((FIXTURE_ROOT / fixture_name).read_text())
        with pytest.raises((ValidationError, ValueError)):
            validate_investigation_result(
                payload,
                packet=packet,
                returned_evidence_ids=returned_ids,
            )


def test_non_frozen_incident_family_is_rejected() -> None:
    packet = canonical_packet()
    payload = valid_result(packet)
    payload["hypotheses"][0]["family"] = "healthy"

    with pytest.raises(ValueError, match="frozen"):
        validate_investigation_result(
            payload,
            packet=packet,
            returned_evidence_ids=set(payload["cited_evidence_ids"]),
        )


def test_insufficient_coverage_cannot_propose_an_action() -> None:
    packet = canonical_packet()
    payload = valid_result(packet)
    evidence_ids = tuple(item.evidence_id for item in packet.evidence_refs[:1])
    payload["hypotheses"] = [
        {
            "hypothesis_id": "hyp-capacity-queueing",
            "family": "capacity_queueing",
            "rank": 1,
            "evidence_coverage": "insufficient",
            "statement": "Capacity queueing needs more evidence.",
            "supporting_evidence_ids": list(evidence_ids),
            "contradicting_evidence_ids": [],
            "missing_evidence": ["queue window"],
            "next_check": "Retrieve the queue window.",
        }
    ]
    payload["leading_hypothesis_id"] = "hyp-capacity-queueing"
    payload["proposed_action"] = {
        "plan_id": "plan-ep-61d8aa-scale-out",
        "incident_id": packet.incident_id,
        "action_type": "simulated_scale_out",
        "target": {"kind": "simulator", "service_id": packet.service_id},
        "from_replicas": 1,
        "to_replicas": 2,
        "requires_human_approval": True,
        "created_at": datetime(2026, 9, 14, 1, 0, tzinfo=UTC).isoformat(),
        "expires_at": datetime(2026, 9, 14, 1, 5, tzinfo=UTC).isoformat(),
        "expected_effect": "Bounded simulator preview.",
        "risks": ["The cause may differ."],
        "verification_criteria": ["Recheck the fixed window."],
        "policy_ref": "policy-infineq-v1",
    }

    with pytest.raises(ValueError, match="action proposal"):
        validate_investigation_result(
            payload,
            packet=packet,
            returned_evidence_ids=set(evidence_ids),
        )


def test_no_incident_has_no_leading_cause_or_action() -> None:
    packet = canonical_packet()
    result = validate_investigation_result(
        {
            "investigation_id": "investigation-ep-61d8aa",
            "incident_id": packet.incident_id,
            "completed_at": datetime.now(UTC).isoformat(),
            "disposition": "no_incident",
            "summary": "No sustained incident was established.",
            "hypotheses": [],
            "leading_hypothesis_id": None,
            "proposed_action": None,
            "cited_evidence_ids": [],
            "limitations": [
                "capacity_queueing, backend_slowdown, workload_shape_change, and "
                "replica_or_deployment_regression were not established",
            ],
        },
        packet=packet,
        returned_evidence_ids=set(),
    )

    assert result.disposition is Disposition.NO_INCIDENT
    assert result.leading_hypothesis_id is None
    assert result.proposed_action is None


def test_hidden_reasoning_is_not_a_valid_output_field() -> None:
    packet = canonical_packet()
    payload = valid_result(packet)
    payload["reasoning"] = "private chain of thought"

    with pytest.raises(ValidationError):
        InvestigationResultV1.model_validate(payload)


def test_complete_queue_hypothesis_may_carry_only_the_frozen_dry_run_action() -> None:
    packet = canonical_packet()
    payload = valid_result(packet)
    evidence_ids = tuple(item.evidence_id for item in packet.evidence_refs[:3])
    payload["proposed_action"] = {
        "plan_id": "plan-ep-61d8aa-scale-out",
        "incident_id": packet.incident_id,
        "action_type": "simulated_scale_out",
        "target": {"kind": "simulator", "service_id": packet.service_id},
        "from_replicas": 1,
        "to_replicas": 2,
        "requires_human_approval": True,
        "created_at": datetime(2026, 9, 14, 1, 0, tzinfo=UTC).isoformat(),
        "expires_at": datetime(2026, 9, 14, 1, 5, tzinfo=UTC).isoformat(),
        "expected_effect": "Bounded simulator preview.",
        "risks": ["The cause may differ."],
        "verification_criteria": ["Recheck the fixed window."],
        "policy_ref": "policy-infineq-v1",
    }
    result = validate_investigation_result(
        payload,
        packet=packet,
        returned_evidence_ids=set(evidence_ids),
    )

    assert isinstance(result.proposed_action, ActionPlanV1)
    assert result.proposed_action.action_type is ActionType.SIMULATED_SCALE_OUT
    assert result.proposed_action.target.kind == "simulator"


def test_validator_allows_improves_without_treating_it_as_proves() -> None:
    packet = canonical_packet()
    payload = valid_result(packet)
    payload["limitations"] = [
        *payload["limitations"],
        "The simulator preview improves queue behavior.",
    ]

    result = validate_investigation_result(
        payload,
        packet=packet,
        returned_evidence_ids=set(payload["cited_evidence_ids"]),
    )

    assert result.limitations[-1] == "The simulator preview improves queue behavior."
