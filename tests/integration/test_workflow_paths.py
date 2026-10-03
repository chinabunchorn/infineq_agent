from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from infineq.detection.detector import Detector
from infineq.errors import AgentTimeoutError, ToolTransportError
from infineq.evidence.store import EvidenceStore
from infineq.evidence.tool_schemas import GetPolicyRequest
from infineq.evidence.tools import VerifierTools
from infineq.schemas.action import ActionPlanV1, ActionTarget, ActionType
from infineq.schemas.incident import IncidentPacketV1
from infineq.schemas.investigation import (
    Disposition,
    EvidenceCoverage,
    Hypothesis,
    IncidentFamily,
    InvestigationResultV1,
)
from infineq.schemas.verification import ClaimCheck, VerificationResultV1, VerificationStatus
from infineq.workflow.corrections import CorrectionPacketV1
from infineq.workflow.orchestrator import WorkflowOrchestrator
from infineq.workflow.transitions import WorkflowActor, WorkflowFailure, WorkflowState

PROJECT_ROOT = Path(__file__).parents[2]
INCIDENT_ID = "ep-61d8aa"
POLICY_REF = "policy-infineq-v1"


class FakeInvestigator:
    def __init__(self, result: InvestigationResultV1) -> None:
        self.result = result
        self.calls: list[tuple[IncidentPacketV1, CorrectionPacketV1 | None]] = []

    def investigate(
        self,
        packet: IncidentPacketV1,
        *,
        correction_packet: CorrectionPacketV1 | None = None,
    ) -> InvestigationResultV1:
        self.calls.append((packet, correction_packet))
        return self.result


class FakeVerifier:
    def __init__(self, result: VerificationResultV1) -> None:
        self.result = result
        self.calls: list[tuple[IncidentPacketV1, InvestigationResultV1]] = []

    def verify(
        self,
        packet: IncidentPacketV1,
        investigation: InvestigationResultV1,
    ) -> VerificationResultV1:
        self.calls.append((packet, investigation))
        return self.result


class SequencedInvestigator:
    def __init__(self, results: tuple[InvestigationResultV1, ...]) -> None:
        self.results = list(results)
        self.calls: list[tuple[IncidentPacketV1, CorrectionPacketV1 | None]] = []
        self._returned_evidence_ids: set[str] = set()

    @property
    def returned_evidence_ids(self) -> frozenset[str]:
        return frozenset(self._returned_evidence_ids)

    def investigate(
        self,
        packet: IncidentPacketV1,
        *,
        correction_packet: CorrectionPacketV1 | None = None,
    ) -> InvestigationResultV1:
        self.calls.append((packet, correction_packet))
        result = self.results.pop(0)
        self._returned_evidence_ids.update(result.cited_evidence_ids)
        return result


class SequencedVerifier:
    def __init__(self, results: tuple[VerificationResultV1, ...]) -> None:
        self.results = list(results)
        self.calls: list[tuple[IncidentPacketV1, InvestigationResultV1]] = []

    def verify(
        self,
        packet: IncidentPacketV1,
        investigation: InvestigationResultV1,
    ) -> VerificationResultV1:
        self.calls.append((packet, investigation))
        return self.results.pop(0)


class TimeoutInvestigator:
    def __init__(self) -> None:
        self.calls: list[tuple[IncidentPacketV1, CorrectionPacketV1 | None]] = []

    def investigate(
        self,
        packet: IncidentPacketV1,
        *,
        correction_packet: CorrectionPacketV1 | None = None,
    ) -> InvestigationResultV1:
        self.calls.append((packet, correction_packet))
        raise AgentTimeoutError("investigator timed out")


class TimeoutVerifier:
    def __init__(self) -> None:
        self.calls: list[tuple[IncidentPacketV1, InvestigationResultV1]] = []

    def verify(
        self,
        packet: IncidentPacketV1,
        investigation: InvestigationResultV1,
    ) -> VerificationResultV1:
        self.calls.append((packet, investigation))
        raise AgentTimeoutError("verifier timed out")


def canonical_packet() -> IncidentPacketV1:
    packet = Detector(store=EvidenceStore(project_root=PROJECT_ROOT)).detect(INCIDENT_ID)
    assert packet is not None
    return packet


def policy_for() -> Any:
    return VerifierTools(store=EvidenceStore(project_root=PROJECT_ROOT)).get_policy(
        GetPolicyRequest(policy_ref=POLICY_REF)
    )


def action_for(packet: IncidentPacketV1) -> ActionPlanV1:
    return ActionPlanV1(
        plan_id=f"plan-{packet.incident_id}-scale-out",
        incident_id=packet.incident_id,
        action_type=ActionType.SIMULATED_SCALE_OUT,
        target=ActionTarget(kind="simulator", service_id=packet.service_id),
        from_replicas=1,
        to_replicas=2,
        requires_human_approval=True,
        created_at=packet.detected_at,
        expires_at=packet.detected_at + timedelta(seconds=300),
        expected_effect="Simulate one additional serving replica.",
        risks=("The observed signal may have a different cause.",),
        verification_criteria=("Re-measure the fixed latency and queue signals.",),
        policy_ref=POLICY_REF,
    )


def diagnosed_investigation(
    packet: IncidentPacketV1,
    *,
    proposed_action: ActionPlanV1 | None,
) -> InvestigationResultV1:
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


def verified_result(investigation: InvestigationResultV1) -> VerificationResultV1:
    return VerificationResultV1(
        verification_id=f"verification-{investigation.incident_id}",
        incident_id=investigation.incident_id,
        investigation_id=investigation.investigation_id,
        checked_at=datetime.now(UTC),
        status=VerificationStatus.VERIFIED,
        claim_checks=(
            ClaimCheck(
                claim_id="claim-leading-hypothesis",
                evidence_ids=(investigation.cited_evidence_ids[0],),
                supported=True,
                note="The cited evidence supports the bounded claim.",
            ),
        ),
        issues=(),
        correction_requests=(),
        policy_ref=POLICY_REF,
    )


def revision_requested_result(investigation: InvestigationResultV1) -> VerificationResultV1:
    return VerificationResultV1(
        verification_id=f"verification-{investigation.incident_id}",
        incident_id=investigation.incident_id,
        investigation_id=investigation.investigation_id,
        checked_at=datetime.now(UTC),
        status=VerificationStatus.REVISION_REQUIRED,
        claim_checks=(),
        issues=(),
        correction_requests=("add a credible competing explanation",),
        policy_ref=POLICY_REF,
    )


def test_verified_diagnosis_reaches_awaiting_human_without_execution() -> None:
    packet = canonical_packet()
    investigation = diagnosed_investigation(packet, proposed_action=action_for(packet))
    investigator = FakeInvestigator(investigation)
    verifier = FakeVerifier(verified_result(investigation))

    outcome = WorkflowOrchestrator(
        investigator=investigator,
        verifier=verifier,
        policy=policy_for(),
    ).run(packet, workflow_id=f"workflow-{packet.incident_id}")

    assert outcome.final_state is WorkflowState.AWAITING_HUMAN
    assert outcome.context.final_state is WorkflowState.AWAITING_HUMAN
    assert outcome.action_card == investigation.proposed_action
    assert len(investigator.calls) == 1
    assert len(verifier.calls) == 1
    assert [event.to_state for event in outcome.audit_events] == [
        WorkflowState.DETECTED,
        WorkflowState.INVESTIGATING,
        WorkflowState.VERIFYING,
        WorkflowState.PRESENTABLE,
        WorkflowState.AWAITING_HUMAN,
    ]
    assert outcome.context.policy_decision is not None
    assert outcome.context.policy_decision.allowed is True


def test_one_correction_packet_drives_one_corrected_investigator_call() -> None:
    packet = canonical_packet()
    first_investigation = diagnosed_investigation(packet, proposed_action=None)
    corrected_investigation = diagnosed_investigation(
        packet,
        proposed_action=action_for(packet),
    ).model_copy(update={"investigation_id": f"investigation-{packet.incident_id}-r1"})
    investigator = SequencedInvestigator((first_investigation, corrected_investigation))
    verifier = SequencedVerifier(
        (
            revision_requested_result(first_investigation),
            verified_result(corrected_investigation),
        )
    )

    outcome = WorkflowOrchestrator(
        investigator=investigator,
        verifier=verifier,
        policy=policy_for(),
    ).run(packet, workflow_id=f"workflow-{packet.incident_id}")

    assert outcome.final_state is WorkflowState.AWAITING_HUMAN
    assert len(investigator.calls) == 2
    assert investigator.calls[0][1] is None
    assert isinstance(investigator.calls[1][1], CorrectionPacketV1)
    assert investigator.calls[1][1].revision == 1
    assert investigator.calls[1][1].reuse_evidence_ids == tuple(
        sorted(first_investigation.cited_evidence_ids)
    )
    assert len(verifier.calls) == 2
    assert verifier.calls[1][1] == corrected_investigation
    assert outcome.context.previous_investigation == first_investigation
    assert outcome.context.current_investigation == corrected_investigation
    assert outcome.context.revision_count == 1
    assert [event.to_state for event in outcome.audit_events] == [
        WorkflowState.DETECTED,
        WorkflowState.INVESTIGATING,
        WorkflowState.VERIFYING,
        WorkflowState.REVISION_REQUESTED,
        WorkflowState.INVESTIGATING,
        WorkflowState.VERIFYING,
        WorkflowState.PRESENTABLE,
        WorkflowState.AWAITING_HUMAN,
    ]


def test_ttl_revision_reaches_action_card_only_after_corrected_policy_plan() -> None:
    packet = canonical_packet()
    bad_action = action_for(packet).model_copy(
        update={"expires_at": packet.detected_at + timedelta(minutes=15)}
    )
    first = diagnosed_investigation(packet, proposed_action=bad_action)
    corrected = diagnosed_investigation(packet, proposed_action=action_for(packet)).model_copy(
        update={"investigation_id": f"investigation-{packet.incident_id}-r1"}
    )
    ttl_revision = revision_requested_result(first).model_copy(
        update={"correction_requests": ("action TTL does not match policy",)}
    )
    investigator = SequencedInvestigator((first, corrected))
    verifier = SequencedVerifier((ttl_revision, verified_result(corrected)))
    outcome = WorkflowOrchestrator(
        investigator=investigator,
        verifier=verifier,
        policy=policy_for(),
    ).run(packet, workflow_id=f"workflow-ttl-revision-{packet.incident_id}")

    assert outcome.final_state is WorkflowState.AWAITING_HUMAN
    assert outcome.action_card == corrected.proposed_action
    assert outcome.action_card != bad_action
    assert len(investigator.calls) == len(verifier.calls) == 2
    correction = investigator.calls[1][1]
    assert correction is not None
    assert any(item.policy_code == "action_ttl" for item in correction.corrections)
    assert all(event.to_state is not WorkflowState.ACTION_APPLIED for event in outcome.audit_events)


def test_second_verification_correction_ends_indeterminate_without_an_action_card() -> None:
    packet = canonical_packet()
    first_investigation = diagnosed_investigation(packet, proposed_action=None)
    corrected_investigation = diagnosed_investigation(
        packet,
        proposed_action=action_for(packet),
    ).model_copy(update={"investigation_id": f"investigation-{packet.incident_id}-r1"})
    second_revision = revision_requested_result(corrected_investigation).model_copy(
        update={"verification_id": f"verification-{packet.incident_id}-r1"}
    )
    investigator = SequencedInvestigator((first_investigation, corrected_investigation))
    verifier = SequencedVerifier(
        (
            revision_requested_result(first_investigation),
            second_revision,
        )
    )

    outcome = WorkflowOrchestrator(
        investigator=investigator,
        verifier=verifier,
        policy=policy_for(),
    ).run(packet, workflow_id=f"workflow-{packet.incident_id}")

    assert outcome.final_state is WorkflowState.INDETERMINATE
    assert outcome.action_card is None
    assert len(investigator.calls) == 2
    assert len(verifier.calls) == 2
    assert outcome.context.current_investigation == corrected_investigation
    assert outcome.context.verification == second_revision
    assert outcome.context.revision_count == 1
    assert [event.to_state for event in outcome.audit_events] == [
        WorkflowState.DETECTED,
        WorkflowState.INVESTIGATING,
        WorkflowState.VERIFYING,
        WorkflowState.REVISION_REQUESTED,
        WorkflowState.INVESTIGATING,
        WorkflowState.VERIFYING,
        WorkflowState.INDETERMINATE,
    ]


def test_investigator_timeout_returns_analysis_incomplete_without_an_action_card() -> None:
    packet = canonical_packet()
    investigator = TimeoutInvestigator()
    verifier = FakeVerifier(
        verified_result(diagnosed_investigation(packet, proposed_action=action_for(packet)))
    )

    outcome = WorkflowOrchestrator(
        investigator=investigator,
        verifier=verifier,
        policy=policy_for(),
    ).run(packet, workflow_id=f"workflow-{packet.incident_id}")

    assert outcome.final_state is WorkflowState.ANALYSIS_INCOMPLETE
    assert outcome.action_card is None
    assert outcome.failure is not None
    assert outcome.failure.value == "timeout"
    assert len(investigator.calls) == 1
    assert verifier.calls == []
    assert outcome.context.current_investigation is None
    assert outcome.context.verification is None
    assert [event.to_state for event in outcome.audit_events] == [
        WorkflowState.DETECTED,
        WorkflowState.INVESTIGATING,
        WorkflowState.ANALYSIS_INCOMPLETE,
    ]


def test_verifier_timeout_returns_analysis_incomplete_without_an_action_card() -> None:
    packet = canonical_packet()
    investigation = diagnosed_investigation(packet, proposed_action=action_for(packet))
    investigator = FakeInvestigator(investigation)
    verifier = TimeoutVerifier()

    outcome = WorkflowOrchestrator(
        investigator=investigator,
        verifier=verifier,
        policy=policy_for(),
    ).run(packet, workflow_id=f"workflow-{packet.incident_id}")

    assert outcome.final_state is WorkflowState.ANALYSIS_INCOMPLETE
    assert outcome.action_card is None
    assert outcome.failure is not None
    assert outcome.failure.value == "timeout"
    assert len(investigator.calls) == 1
    assert len(verifier.calls) == 1
    assert outcome.context.current_investigation == investigation
    assert outcome.context.verification is None
    assert [event.to_state for event in outcome.audit_events] == [
        WorkflowState.DETECTED,
        WorkflowState.INVESTIGATING,
        WorkflowState.VERIFYING,
        WorkflowState.ANALYSIS_INCOMPLETE,
    ]


class TransportFailureInvestigator:
    def __init__(self) -> None:
        self.calls: list[tuple[IncidentPacketV1, CorrectionPacketV1 | None]] = []

    def investigate(
        self,
        packet: IncidentPacketV1,
        *,
        correction_packet: CorrectionPacketV1 | None = None,
    ) -> InvestigationResultV1:
        self.calls.append((packet, correction_packet))
        raise ToolTransportError("tool transport token=secret-token")


class TransportFailureVerifier:
    def __init__(self) -> None:
        self.calls: list[tuple[IncidentPacketV1, InvestigationResultV1]] = []

    def verify(
        self,
        packet: IncidentPacketV1,
        investigation: InvestigationResultV1,
    ) -> VerificationResultV1:
        self.calls.append((packet, investigation))
        raise ToolTransportError("tool transport token=secret-token")


@pytest.mark.parametrize("failing_boundary", ("investigator", "verifier"))
def test_typed_tool_transport_failure_from_either_agent_boundary_is_audited(
    failing_boundary: str,
) -> None:
    packet = canonical_packet()
    investigation = diagnosed_investigation(packet, proposed_action=action_for(packet))
    investigator = (
        TransportFailureInvestigator()
        if failing_boundary == "investigator"
        else FakeInvestigator(investigation)
    )
    verifier = (
        FakeVerifier(verified_result(investigation))
        if failing_boundary == "investigator"
        else TransportFailureVerifier()
    )

    outcome = WorkflowOrchestrator(
        investigator=investigator,
        verifier=verifier,
        policy=policy_for(),
    ).run(packet, workflow_id=f"workflow-{packet.incident_id}")

    expected_actor = (
        WorkflowActor.INVESTIGATOR if failing_boundary == "investigator" else WorkflowActor.VERIFIER
    )
    assert outcome.final_state is WorkflowState.ANALYSIS_INCOMPLETE
    assert outcome.action_card is None
    assert outcome.failure is WorkflowFailure.TOOL_TRANSPORT
    assert outcome.audit_events[-1].actor is expected_actor
    assert "tool_transport" in outcome.audit_events[-1].reason
    assert "secret-token" not in outcome.audit_events[-1].reason
    assert outcome.context.final_state is WorkflowState.ANALYSIS_INCOMPLETE


def test_required_data_that_becomes_stale_before_verification_ends_safely() -> None:
    packet = canonical_packet()
    investigation = diagnosed_investigation(packet, proposed_action=action_for(packet))
    investigator = FakeInvestigator(investigation)
    verifier = FakeVerifier(verified_result(investigation))
    stale_at = packet.detected_at + timedelta(seconds=11)

    outcome = WorkflowOrchestrator(
        investigator=investigator,
        verifier=verifier,
        policy=policy_for(),
        clock=lambda: stale_at,
    ).run(packet, workflow_id=f"workflow-{packet.incident_id}")

    assert outcome.final_state is WorkflowState.INDETERMINATE
    assert outcome.action_card is None
    assert outcome.failure is WorkflowFailure.STALE_REQUIRED_DATA
    assert len(investigator.calls) == 1
    assert verifier.calls == []
    assert outcome.context.verification is None
    assert outcome.audit_events[-1].to_state is WorkflowState.INDETERMINATE
    assert "stale" in outcome.audit_events[-1].reason


class NeverCalledInvestigator:
    def __init__(self) -> None:
        self.calls = 0

    def investigate(
        self,
        packet: IncidentPacketV1,
        *,
        correction_packet: CorrectionPacketV1 | None = None,
    ) -> InvestigationResultV1:
        self.calls += 1
        raise AssertionError("no-incident input must not invoke the Investigator")


class NeverCalledVerifier:
    def __init__(self) -> None:
        self.calls = 0

    def verify(
        self,
        packet: IncidentPacketV1,
        investigation: InvestigationResultV1,
    ) -> VerificationResultV1:
        self.calls += 1
        raise AssertionError("no-incident input must not invoke the Verifier")


def test_no_incident_detection_run_closes_without_agents_or_action_card() -> None:
    detection = Detector(store=EvidenceStore(project_root=PROJECT_ROOT)).run("ep-6a4c31")
    investigator = NeverCalledInvestigator()
    verifier = NeverCalledVerifier()

    assert detection.packet is None
    assert detection.decision == "no_incident"
    outcome = WorkflowOrchestrator(
        investigator=investigator,
        verifier=verifier,
        policy=policy_for(),
    ).run_detection(detection, workflow_id="workflow-ep-6a4c31")

    assert outcome.final_state is WorkflowState.CLOSED
    assert outcome.action_card is None
    assert outcome.failure is None
    assert outcome.context.incident_packet is None
    assert outcome.context.detection_run is detection
    assert outcome.context.current_investigation is None
    assert outcome.context.verification is None
    assert outcome.context.investigator_tool_calls == 0
    assert outcome.context.verifier_tool_calls == 0
    assert investigator.calls == 0
    assert verifier.calls == 0
    assert [event.to_state for event in outcome.audit_events] == [
        WorkflowState.DETECTED,
        WorkflowState.CLOSED,
    ]


def forbidden_action_for(packet: IncidentPacketV1, variant: str) -> ActionPlanV1:
    valid_action = action_for(packet)
    values = valid_action.model_dump(mode="python")
    values["target"] = valid_action.target
    if variant == "real_kubernetes":
        values["target"] = ActionTarget.model_construct(
            kind="kubernetes",
            service_id="prod-service",
        )
    elif variant == "wrong_target":
        values["target"] = ActionTarget.model_construct(
            kind="simulator",
            service_id="svc-other",
        )
    elif variant == "wrong_type":
        values["action_type"] = "kubernetes_scale_out"
    elif variant == "wrong_approval":
        values["requires_human_approval"] = False
    elif variant == "wrong_policy":
        values["policy_ref"] = "policy-other"
    elif variant == "wrong_limits":
        values["from_replicas"] = 2
        values["to_replicas"] = 3
    else:
        raise AssertionError(f"unknown forbidden action variant: {variant}")
    return ActionPlanV1.model_construct(**values)


@pytest.mark.parametrize(
    "variant",
    (
        "real_kubernetes",
        "wrong_target",
        "wrong_type",
        "wrong_approval",
        "wrong_policy",
        "wrong_limits",
    ),
)
def test_forbidden_action_proposal_is_deterministically_blocked_without_a_card(
    variant: str,
) -> None:
    packet = canonical_packet()
    unsafe_action = forbidden_action_for(packet, variant)
    investigation = diagnosed_investigation(packet, proposed_action=action_for(packet)).model_copy(
        update={"proposed_action": unsafe_action}
    )
    outcome = WorkflowOrchestrator(
        investigator=FakeInvestigator(investigation),
        verifier=FakeVerifier(verified_result(investigation)),
        policy=policy_for(),
    ).run(packet, workflow_id=f"workflow-{packet.incident_id}")

    assert outcome.final_state is WorkflowState.INDETERMINATE
    assert outcome.action_card is None
    assert outcome.failure is WorkflowFailure.BLOCKED
    assert outcome.context.policy_decision is not None
    assert outcome.context.policy_decision.allowed is False
    assert all(event.to_state is not WorkflowState.AWAITING_HUMAN for event in outcome.audit_events)
    assert outcome.audit_events[-1].actor is WorkflowActor.POLICY


def disagreeing_verified_result(investigation: InvestigationResultV1) -> VerificationResultV1:
    return verified_result(investigation).model_copy(
        update={
            "claim_checks": (
                ClaimCheck(
                    claim_id="claim-non-leading-evidence",
                    evidence_ids=(investigation.cited_evidence_ids[1],),
                    supported=True,
                    note="The verifier did not check the leading hypothesis evidence.",
                ),
            ),
        }
    )


def test_unresolved_agent_disagreement_ends_indeterminate_after_one_correction() -> None:
    packet = canonical_packet()
    first_investigation = diagnosed_investigation(packet, proposed_action=None)
    corrected_investigation = diagnosed_investigation(
        packet,
        proposed_action=action_for(packet),
    ).model_copy(update={"investigation_id": f"investigation-{packet.incident_id}-r1"})
    investigator = SequencedInvestigator((first_investigation, corrected_investigation))
    verifier = SequencedVerifier(
        (
            revision_requested_result(first_investigation),
            disagreeing_verified_result(corrected_investigation),
        )
    )

    outcome = WorkflowOrchestrator(
        investigator=investigator,
        verifier=verifier,
        policy=policy_for(),
    ).run(packet, workflow_id=f"workflow-{packet.incident_id}")

    assert outcome.final_state is WorkflowState.INDETERMINATE
    assert outcome.action_card is None
    assert outcome.failure is WorkflowFailure.BLOCKED
    assert outcome.context.revision_count == 1
    assert len(investigator.calls) == 2
    assert len(verifier.calls) == 2
    assert all(event.to_state is not WorkflowState.AWAITING_HUMAN for event in outcome.audit_events)
    assert outcome.audit_events[-1].actor is WorkflowActor.POLICY
