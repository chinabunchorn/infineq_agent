from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from infineq.detection.detector import Detector
from infineq.evidence.store import EvidenceStore
from infineq.evidence.tool_schemas import GetPolicyRequest
from infineq.evidence.tools import VerifierTools
from infineq.schemas.action import ActionPlanV1, ActionTarget, ActionType
from infineq.schemas.evidence import DataQuality, DataQualityStatus
from infineq.schemas.incident import IncidentPacketV1
from infineq.schemas.investigation import (
    Disposition,
    EvidenceCoverage,
    Hypothesis,
    IncidentFamily,
    InvestigationResultV1,
)
from infineq.schemas.verification import ClaimCheck, VerificationResultV1, VerificationStatus
from infineq.workflow.policy import evaluate_action_policy, evaluate_presentation_policy

PROJECT_ROOT = Path(__file__).parents[3]
POLICY_REF = "policy-infineq-v1"


def canonical_packet() -> IncidentPacketV1:
    packet = Detector(store=EvidenceStore(project_root=PROJECT_ROOT)).detect("ep-61d8aa")
    assert packet is not None
    return packet


def action_for(packet: IncidentPacketV1) -> ActionPlanV1:
    return ActionPlanV1(
        plan_id="plan-ep-61d8aa-scale-out",
        incident_id=packet.incident_id,
        action_type=ActionType.SIMULATED_SCALE_OUT,
        target=ActionTarget(kind="simulator", service_id=packet.service_id),
        from_replicas=1,
        to_replicas=2,
        requires_human_approval=True,
        created_at=packet.detected_at,
        expires_at=packet.detected_at + timedelta(seconds=300),
        expected_effect="Bounded simulator preview.",
        risks=("The observed signal may have a different cause.",),
        verification_criteria=("Re-measure the fixed latency and queue signals.",),
        policy_ref=POLICY_REF,
    )


def investigation_for(
    packet: IncidentPacketV1,
    *,
    proposed_action: ActionPlanV1 | None = None,
) -> InvestigationResultV1:
    evidence_ids = tuple(item.evidence_id for item in packet.evidence_refs[:2])
    return InvestigationResultV1(
        investigation_id="investigation-ep-61d8aa",
        incident_id=packet.incident_id,
        completed_at=packet.detected_at,
        disposition=Disposition.DIAGNOSED,
        summary=(
            "The comparison covers capacity_queueing, backend_slowdown, workload_shape_change, "
            "and replica_or_deployment_regression."
        ),
        hypotheses=(
            Hypothesis(
                hypothesis_id="hyp-capacity-queueing",
                family=IncidentFamily.CAPACITY_QUEUEING,
                rank=1,
                evidence_coverage=EvidenceCoverage.COMPLETE,
                statement="Capacity queueing is the leading supported explanation.",
                supporting_evidence_ids=(evidence_ids[0],),
                contradicting_evidence_ids=(),
                missing_evidence=(),
            ),
            Hypothesis(
                hypothesis_id="hyp-backend-slowdown",
                family=IncidentFamily.BACKEND_SLOWDOWN,
                rank=2,
                evidence_coverage=EvidenceCoverage.PARTIAL,
                statement="Backend slowdown is less supported.",
                supporting_evidence_ids=(),
                contradicting_evidence_ids=(evidence_ids[1],),
                missing_evidence=("stage timing",),
            ),
        ),
        leading_hypothesis_id="hyp-capacity-queueing",
        proposed_action=proposed_action,
        cited_evidence_ids=evidence_ids,
        limitations=("replica_or_deployment_regression remains an alternative.",),
    )


def policy() -> object:
    return VerifierTools(store=EvidenceStore(project_root=PROJECT_ROOT)).get_policy(
        GetPolicyRequest(policy_ref=POLICY_REF)
    )


def verification(investigation: InvestigationResultV1) -> VerificationResultV1:
    evidence_id = investigation.cited_evidence_ids[0]
    return VerificationResultV1(
        verification_id="verification-ep-61d8aa",
        incident_id=investigation.incident_id,
        investigation_id=investigation.investigation_id,
        checked_at=datetime.now(UTC),
        status=VerificationStatus.VERIFIED,
        claim_checks=(
            ClaimCheck(
                claim_id="claim-leading-hypothesis",
                evidence_ids=(evidence_id,),
                supported=True,
                note="supported",
            ),
        ),
        issues=(),
        correction_requests=(),
        policy_ref=POLICY_REF,
    )


def test_deterministic_policy_independently_authorizes_a_valid_simulator_card() -> None:
    packet = canonical_packet()
    investigation = investigation_for(packet, proposed_action=action_for(packet))

    decision = evaluate_presentation_policy(
        investigation=investigation,
        verification=verification(investigation),
        policy=policy(),
    )

    assert decision.allowed is True
    assert decision.source == "deterministic_policy"


def test_action_policy_can_score_investigator_without_pretending_human_already_approved() -> None:
    packet = canonical_packet()
    investigation = investigation_for(packet, proposed_action=action_for(packet))

    decision = evaluate_action_policy(
        investigation=investigation,
        policy=policy(),
        packet=packet,
    )

    assert decision.allowed is True


def test_action_policy_rejects_wrong_ttl_without_a_verifier_result() -> None:
    packet = canonical_packet()
    action = action_for(packet).model_copy(
        update={"expires_at": packet.detected_at + timedelta(seconds=3600)}
    )
    investigation = investigation_for(packet, proposed_action=action)

    decision = evaluate_action_policy(
        investigation=investigation,
        policy=policy(),
        packet=packet,
    )

    assert decision.allowed is False
    assert "ttl" in decision.reason.casefold()


def test_action_card_rejects_scale_out_when_packet_replica_is_not_ready() -> None:
    packet = Detector(store=EvidenceStore(project_root=PROJECT_ROOT)).detect("ep-4b6fa0")
    assert packet is not None
    assert packet.deployment.replicas == 1
    assert packet.deployment.ready_replicas == 0
    investigation = investigation_for(packet, proposed_action=action_for(packet))

    action_decision = evaluate_action_policy(
        investigation=investigation,
        policy=policy(),
        packet=packet,
    )
    presentation_decision = evaluate_presentation_policy(
        investigation=investigation,
        verification=verification(investigation),
        policy=policy(),
        packet=packet,
    )

    assert action_decision.allowed is False
    assert presentation_decision.allowed is False
    assert "replica" in action_decision.reason.casefold()


def test_action_policy_rejects_non_datetime_ttl_without_crashing() -> None:
    packet = canonical_packet()
    action = action_for(packet)
    malformed_action = ActionPlanV1.model_construct(
        **{
            **action.model_dump(mode="python"),
            "target": action.target,
            "expires_at": "not-a-datetime",
        }
    )
    investigation = investigation_for(packet).model_copy(
        update={"proposed_action": malformed_action}
    )

    decision = evaluate_action_policy(
        investigation=investigation,
        policy=policy(),
        packet=packet,
    )

    assert decision.allowed is False
    assert "ttl" in decision.reason.casefold()


def test_deterministic_policy_blocks_real_kubernetes_targets() -> None:
    packet = canonical_packet()
    unsafe_target = ActionTarget.model_construct(kind="kubernetes", service_id="prod-service")
    unsafe_action = ActionPlanV1.model_construct(
        schema_version="1.0",
        plan_id="plan-unsafe",
        incident_id=packet.incident_id,
        action_type=ActionType.SIMULATED_SCALE_OUT,
        target=unsafe_target,
        from_replicas=1,
        to_replicas=2,
        requires_human_approval=True,
        created_at=packet.detected_at,
        expires_at=packet.detected_at + timedelta(seconds=300),
        expected_effect="real target",
        risks=("real mutation",),
        verification_criteria=("observe",),
        policy_ref=POLICY_REF,
    )
    investigation = investigation_for(packet).model_copy(update={"proposed_action": unsafe_action})

    decision = evaluate_presentation_policy(
        investigation=investigation,
        verification=verification(investigation),
        policy=policy(),
    )

    assert decision.allowed is False
    assert "target" in decision.reason.casefold()


def test_deterministic_policy_blocks_action_without_required_human_approval() -> None:
    packet = canonical_packet()
    action = action_for(packet).model_copy(update={"requires_human_approval": False})
    investigation = investigation_for(packet).model_copy(update={"proposed_action": action})

    decision = evaluate_presentation_policy(
        investigation=investigation,
        verification=verification(investigation),
        policy=policy(),
    )

    assert decision.allowed is False
    assert "approval" in decision.reason.casefold()


def test_deterministic_policy_blocks_action_when_approval_field_is_missing() -> None:
    packet = canonical_packet()
    action = action_for(packet)
    action_values = action.model_dump(mode="python")
    action_values.pop("requires_human_approval")
    action_values["target"] = action.target
    missing_approval_action = ActionPlanV1.model_construct(**action_values)
    investigation = investigation_for(packet).model_copy(
        update={"proposed_action": missing_approval_action}
    )

    decision = evaluate_presentation_policy(
        investigation=investigation,
        verification=verification(investigation),
        policy=policy(),
    )

    assert decision.allowed is False
    assert "approval" in decision.reason.casefold()


def test_deterministic_policy_blocks_stale_or_unverified_inputs() -> None:
    packet = canonical_packet()
    investigation = investigation_for(packet, proposed_action=action_for(packet))
    unverified = verification(investigation).model_copy(
        update={"status": VerificationStatus.BLOCKED, "claim_checks": (), "issues": ("stale",)}
    )

    decision = evaluate_presentation_policy(
        investigation=investigation,
        verification=unverified,
        policy=policy(),
        required_data_fresh=False,
    )

    assert decision.allowed is False
    assert "fresh" in decision.reason.casefold() or "verified" in decision.reason.casefold()


@pytest.mark.parametrize(
    "case",
    [
        "not_verified",
        "not_diagnosed",
        "verification_incident",
        "verification_investigation",
        "stale_argument",
        "packet_incident",
        "bad_quality",
        "stale_packet",
        "missing_action",
        "invalid_target",
        "action_incident",
        "action_service",
        "target_kind",
        "action_type",
        "policy_action_type",
        "replica_limits",
        "policy_approval",
        "policy_reference",
        "dry_run",
        "ttl",
        "missing_leading",
        "unchecked_evidence",
    ],
)
def test_policy_denies_each_typed_presentation_boundary(case: str) -> None:
    packet = canonical_packet()
    action = action_for(packet)
    investigation = investigation_for(packet, proposed_action=action)
    checked = verification(investigation)
    current_policy = policy()
    kwargs: dict[str, object] = {
        "investigation": investigation,
        "verification": checked,
        "policy": current_policy,
        "packet": packet,
    }

    if case == "not_verified":
        kwargs["verification"] = checked.model_copy(update={"status": VerificationStatus.BLOCKED})
    elif case == "not_diagnosed":
        kwargs["investigation"] = investigation.model_copy(
            update={"disposition": Disposition.INDETERMINATE}
        )
    elif case == "verification_incident":
        kwargs["verification"] = checked.model_copy(update={"incident_id": "ep-other1"})
    elif case == "verification_investigation":
        kwargs["verification"] = checked.model_copy(
            update={"investigation_id": "investigation-other1"}
        )
    elif case == "stale_argument":
        kwargs["required_data_fresh"] = False
    elif case == "packet_incident":
        kwargs["packet"] = packet.model_copy(update={"incident_id": "ep-other1"})
    elif case == "bad_quality":
        kwargs["packet"] = packet.model_copy(
            update={
                "data_quality": DataQuality(
                    status=DataQualityStatus.DEGRADED,
                    sample_count=1,
                    freshness_seconds=1,
                    issues=("required series missing",),
                )
            }
        )
    elif case == "stale_packet":
        kwargs["packet"] = packet.model_copy(
            update={
                "data_quality": DataQuality(
                    status=DataQualityStatus.GOOD,
                    sample_count=1,
                    freshness_seconds=11,
                    issues=(),
                )
            }
        )
    elif case == "missing_action":
        kwargs["investigation"] = investigation.model_copy(update={"proposed_action": None})
    elif case == "invalid_target":
        kwargs["investigation"] = investigation.model_copy(
            update={
                "proposed_action": ActionPlanV1.model_construct(
                    **{**action.model_dump(), "target": object()}
                )
            }
        )
    elif case == "action_incident":
        kwargs["investigation"] = investigation.model_copy(
            update={"proposed_action": action.model_copy(update={"incident_id": "ep-other1"})}
        )
    elif case == "action_service":
        kwargs["investigation"] = investigation.model_copy(
            update={
                "proposed_action": action.model_copy(
                    update={"target": action.target.model_copy(update={"service_id": "svc-other"})}
                )
            }
        )
    elif case == "target_kind":
        kwargs["investigation"] = investigation.model_copy(
            update={
                "proposed_action": ActionPlanV1.model_construct(
                    **{
                        **action.model_dump(),
                        "target": ActionTarget.model_construct(
                            kind="kubernetes", service_id=packet.service_id
                        ),
                    }
                )
            }
        )
    elif case == "action_type":
        kwargs["investigation"] = investigation.model_copy(
            update={
                "proposed_action": ActionPlanV1.model_construct(
                    **{**action.model_dump(), "action_type": "delete_service"}
                )
            }
        )
    elif case == "policy_action_type":
        kwargs["policy"] = current_policy.model_copy(update={"allowed_action_types": ()})
    elif case == "replica_limits":
        kwargs["investigation"] = investigation.model_copy(
            update={
                "proposed_action": ActionPlanV1.model_construct(
                    **{**action.model_dump(), "from_replicas": 2}
                )
            }
        )
    elif case == "policy_approval":
        kwargs["policy"] = current_policy.model_copy(update={"requires_human_approval": False})
    elif case == "policy_reference":
        kwargs["investigation"] = investigation.model_copy(
            update={"proposed_action": action.model_copy(update={"policy_ref": "policy-other-v1"})}
        )
    elif case == "dry_run":
        kwargs["policy"] = current_policy.model_copy(
            update={"dry_run_only": False, "execution_allowed": True}
        )
    elif case == "ttl":
        kwargs["investigation"] = investigation.model_copy(
            update={
                "proposed_action": action.model_copy(
                    update={"expires_at": action.expires_at + timedelta(seconds=1)}
                )
            }
        )
    elif case == "missing_leading":
        kwargs["investigation"] = investigation.model_copy(
            update={"leading_hypothesis_id": "hypothesis-missing"}
        )
    elif case == "unchecked_evidence":
        kwargs["verification"] = checked.model_copy(update={"claim_checks": ()})

    decision = evaluate_presentation_policy(**kwargs)  # type: ignore[arg-type]

    assert decision.allowed is False
