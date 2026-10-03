from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from infineq.detection.detector import Detector
from infineq.errors import (
    DataMissingError,
    DataStaleError,
    ToolPolicyDeniedError,
    ToolTransportError,
)
from infineq.evidence.store import EvidenceStore
from infineq.evidence.tool_schemas import GetPolicyRequest
from infineq.evidence.tools import VerifierTools
from infineq.schemas.investigation import (
    Disposition,
    EvidenceCoverage,
    Hypothesis,
    IncidentFamily,
    InvestigationResultV1,
)
from infineq.schemas.verification import VerificationResultV1, VerificationStatus
from infineq.workflow.context import WorkflowContext, WorkflowToolBudget
from infineq.workflow.corrections import CorrectionCategory, CorrectionItem, CorrectionPacketV1
from infineq.workflow.orchestrator import (
    WorkflowOrchestrator,
    _returned_evidence_ids,
    _successful_tool_calls,
    required_data_is_fresh,
    validate_corrected_evidence_use,
)
from infineq.workflow.state_machine import WorkflowStateMachine
from infineq.workflow.transitions import WorkflowActor, WorkflowFailure, WorkflowState

PROJECT_ROOT = Path(__file__).parents[3]
INCIDENT_ID = "ep-61d8aa"


def canonical_packet():
    packet = Detector(store=EvidenceStore(project_root=PROJECT_ROOT)).detect(INCIDENT_ID)
    assert packet is not None
    return packet


def incomplete_context_values():
    packet = canonical_packet()
    machine = WorkflowStateMachine(workflow_id=f"workflow-{packet.incident_id}")
    machine.transition(WorkflowState.DETECTED, actor=WorkflowActor.DETECTOR, reason="detected")
    machine.transition(
        WorkflowState.INVESTIGATING,
        actor=WorkflowActor.WORKFLOW,
        reason="investigating",
    )
    machine.fail(WorkflowFailure.TIMEOUT, reason="timeout")
    return {
        "workflow_id": machine.workflow_id,
        "incident_packet": packet,
        "revision_count": 0,
        "investigator_tool_budget": WorkflowToolBudget(max_calls=6, used_calls=0),
        "verifier_tool_budget": WorkflowToolBudget(max_calls=2, used_calls=0),
        "final_state": machine.state,
        "audit_events": machine.audit_events,
    }


def test_tool_budget_reports_only_remaining_successful_calls() -> None:
    budget = WorkflowToolBudget(max_calls=6, used_calls=2)

    assert budget.remaining_calls == 4
    assert budget.remaining == 4
    assert budget.limit == 6
    assert budget.used == 2


def test_workflow_context_forbids_free_form_mutable_state() -> None:
    values = incomplete_context_values()
    values["mutable_state"] = {"next": "investigate"}

    with pytest.raises(ValidationError, match="extra"):
        WorkflowContext.model_validate(values)


def test_workflow_context_binds_the_exact_packet_and_terminal_audit() -> None:
    values = incomplete_context_values()
    context = WorkflowContext.model_validate(values)

    assert context.incident_packet is values["incident_packet"]
    assert context.final_state is WorkflowState.ANALYSIS_INCOMPLETE
    assert context.audit_events[-1].to_state is WorkflowState.ANALYSIS_INCOMPLETE
    with pytest.raises((TypeError, ValidationError)):
        context.final_state = WorkflowState.CLOSED  # type: ignore[misc]


def test_agent_accounting_helpers_are_safe_and_canonical() -> None:
    class Metadata:
        returned_evidence_ids = ("evidence-b", "evidence-a", "evidence-a")
        successful_tool_calls = 3

    assert _returned_evidence_ids(Metadata()) == ("evidence-a", "evidence-b")
    assert _successful_tool_calls(Metadata()) == 3

    class InvalidMetadata:
        returned_evidence_ids = "not-a-collection-of-ids"
        successful_tool_calls = True

    assert _returned_evidence_ids(InvalidMetadata(), fallback=("fallback",)) == ("fallback",)
    assert _successful_tool_calls(InvalidMetadata()) == 0


def test_corrected_investigation_cannot_introduce_evidence_without_lookup() -> None:
    packet = canonical_packet()
    evidence_id = packet.evidence_refs[0].evidence_id
    correction = CorrectionPacketV1(
        correction_id=f"correction-{packet.incident_id}-r1",
        incident_id=packet.incident_id,
        investigation_id="investigation-before",
        verification_id="verification-before",
        corrections=(CorrectionItem(category=CorrectionCategory.MISSING_ALTERNATIVE),),
        reuse_evidence_ids=(),
        remaining_investigator_tool_calls=0,
    )
    corrected = InvestigationResultV1(
        investigation_id="investigation-after",
        incident_id=packet.incident_id,
        completed_at=packet.detected_at,
        disposition=Disposition.INDETERMINATE,
        summary="The corrected result remains indeterminate.",
        hypotheses=(),
        cited_evidence_ids=(evidence_id,),
    )

    with pytest.raises(ValueError, match="unapproved evidence"):
        validate_corrected_evidence_use(
            packet=packet,
            correction_packet=correction,
            corrected_investigation=corrected,
            investigator_calls_before=0,
            investigator_calls_after=0,
        )


def _incomplete_investigation(packet, *, incident_id: str | None = None) -> InvestigationResultV1:
    return InvestigationResultV1(
        investigation_id=f"investigation-{packet.incident_id}",
        incident_id=incident_id or packet.incident_id,
        completed_at=packet.detected_at,
        disposition=Disposition.ANALYSIS_INCOMPLETE,
        summary="The agent boundary did not return a complete result.",
        hypotheses=(),
        leading_hypothesis_id=None,
        proposed_action=None,
        cited_evidence_ids=(),
        limitations=("typed failure",),
    )


def _blocked_verification(
    packet,
    investigation: InvestigationResultV1,
    *,
    incident_id: str | None = None,
    investigation_id: str | None = None,
) -> VerificationResultV1:
    return VerificationResultV1(
        verification_id=f"verification-{packet.incident_id}",
        incident_id=incident_id or packet.incident_id,
        investigation_id=investigation_id or investigation.investigation_id,
        checked_at=packet.detected_at,
        status=VerificationStatus.BLOCKED,
        claim_checks=(),
        issues=("typed failure",),
        correction_requests=(),
        policy_ref="policy-infineq-v1",
    )


def test_tool_budget_rejects_calls_above_the_declared_limit() -> None:
    with pytest.raises(ValidationError, match="exceed"):
        WorkflowToolBudget(max_calls=2, used_calls=3)


def test_context_rejects_string_evidence_ids_and_untyped_policy() -> None:
    values = incomplete_context_values()
    values["returned_evidence_ids"] = "ev-not-a-collection"
    with pytest.raises((TypeError, ValidationError), match="evidence"):
        WorkflowContext.model_validate(values)

    values = incomplete_context_values()
    values["policy_decision"] = {"allowed": False}
    with pytest.raises((TypeError, ValidationError), match="policy"):
        WorkflowContext.model_validate(values)


def test_packetless_context_requires_typed_untouched_detection() -> None:
    values = incomplete_context_values()
    values.pop("incident_packet")
    values["detection_run"] = None
    with pytest.raises(ValidationError, match="typed detection"):
        WorkflowContext.model_validate(values)


def test_packet_context_rejects_foreign_evidence_and_investigation() -> None:
    packet = canonical_packet()
    values = incomplete_context_values()
    values["returned_evidence_ids"] = ("ev:ep-other1:service_metrics:w0:ttft:p95:deadbeef",)
    with pytest.raises(ValidationError, match="returned evidence"):
        WorkflowContext.model_validate(values)

    values = incomplete_context_values()
    values["current_investigation"] = _incomplete_investigation(packet, incident_id="ep-other1")
    with pytest.raises(ValidationError, match="investigation"):
        WorkflowContext.model_validate(values)


def test_context_binds_verification_and_correction_to_current_revision() -> None:
    packet = canonical_packet()
    investigation = _incomplete_investigation(packet)

    values = incomplete_context_values()
    values["current_investigation"] = investigation
    values["verification"] = _blocked_verification(packet, investigation, incident_id="ep-other1")
    with pytest.raises(ValidationError, match="verification"):
        WorkflowContext.model_validate(values)

    values = incomplete_context_values()
    values["current_investigation"] = investigation
    values["verification"] = _blocked_verification(
        packet,
        investigation,
        investigation_id="investigation-other1",
    )
    with pytest.raises(ValidationError, match="verification"):
        WorkflowContext.model_validate(values)

    correction = CorrectionPacketV1(
        correction_id="correction-ep-61d8aa-r1",
        incident_id=packet.incident_id,
        investigation_id=investigation.investigation_id,
        verification_id="verification-ep-61d8aa",
        corrections=(CorrectionItem(category=CorrectionCategory.MISSING_ALTERNATIVE),),
        reuse_evidence_ids=(),
        remaining_investigator_tool_calls=0,
    )
    values = incomplete_context_values()
    values["current_investigation"] = investigation
    values["correction_packet"] = correction
    with pytest.raises(ValidationError, match="revision"):
        WorkflowContext.model_validate(values)


def test_outcome_rejects_context_mismatches_and_action_cards_outside_approval() -> None:
    values = incomplete_context_values()
    context = WorkflowContext.model_validate(values)
    with pytest.raises(ValidationError, match="final states"):
        from infineq.workflow.context import WorkflowOutcome

        WorkflowOutcome(
            context=context,
            final_state=WorkflowState.CLOSED,
            audit_events=context.audit_events,
        )


def _workflow_policy():
    return VerifierTools(store=EvidenceStore(project_root=PROJECT_ROOT)).get_policy(
        GetPolicyRequest(policy_ref="policy-infineq-v1")
    )


def _diagnosed(packet, *, incident_id: str | None = None) -> InvestigationResultV1:
    evidence_id = packet.evidence_refs[0].evidence_id
    return InvestigationResultV1(
        investigation_id=f"investigation-{packet.incident_id}",
        incident_id=incident_id or packet.incident_id,
        completed_at=packet.detected_at,
        disposition=Disposition.DIAGNOSED,
        summary="typed diagnosis",
        hypotheses=(
            Hypothesis(
                hypothesis_id="hypothesis-leading",
                family=IncidentFamily.CAPACITY_QUEUEING,
                rank=1,
                evidence_coverage=EvidenceCoverage.COMPLETE,
                statement="The leading typed hypothesis is supported.",
                supporting_evidence_ids=(evidence_id,),
                contradicting_evidence_ids=(),
                missing_evidence=(),
            ),
        ),
        leading_hypothesis_id="hypothesis-leading",
        proposed_action=None,
        cited_evidence_ids=(evidence_id,),
    )


def _verification(packet, investigation, status: VerificationStatus) -> VerificationResultV1:
    return VerificationResultV1(
        verification_id=f"verification-{packet.incident_id}",
        incident_id=packet.incident_id,
        investigation_id=investigation.investigation_id,
        checked_at=packet.detected_at,
        status=status,
        claim_checks=(),
        issues=("blocked",) if status is VerificationStatus.BLOCKED else (),
        correction_requests=("missing alternative",)
        if status is VerificationStatus.REVISION_REQUIRED
        else (),
        policy_ref="policy-infineq-v1",
    )


class _StubInvestigator:
    def __init__(self, result: object, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.lookup_allowance: object | None = None

    def investigate(self, packet, *, correction_packet=None):
        if self.error is not None:
            raise self.error
        return self.result


class _StubVerifier:
    def __init__(self, result: object, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.lookup_allowance: object | None = None

    def verify(self, packet, investigation):
        if self.error is not None:
            raise self.error
        return self.result


def test_packetless_detector_results_close_without_invoking_agent_boundaries() -> None:
    detector = Detector(store=EvidenceStore(project_root=PROJECT_ROOT))
    orchestrator = WorkflowOrchestrator(
        investigator=_StubInvestigator(object()),
        verifier=_StubVerifier(object()),
        policy=_workflow_policy(),
    )

    no_incident = orchestrator.run(detector.run("ep-a91e7c"))
    abstained = orchestrator.run(detector.run("ep-e35192"))

    assert no_incident.final_state is WorkflowState.CLOSED
    assert abstained.final_state is WorkflowState.CLOSED
    assert no_incident.context.incident_packet is None
    assert abstained.context.detection_run.decision == "abstain"
    with pytest.raises(TypeError, match="DetectionRun"):
        orchestrator.run_detection(object())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("error", "failure"),
    [
        (TimeoutError("timeout"), WorkflowFailure.TIMEOUT),
        (ToolTransportError("transport"), WorkflowFailure.TOOL_TRANSPORT),
        (DataStaleError("stale"), WorkflowFailure.STALE_REQUIRED_DATA),
        (DataMissingError("missing"), WorkflowFailure.BLOCKED),
        (ToolPolicyDeniedError("denied"), WorkflowFailure.BLOCKED),
    ],
)
def test_orchestrator_maps_dependency_failures_to_typed_terminal_states(error, failure) -> None:
    packet = canonical_packet()
    orchestrator = WorkflowOrchestrator(
        investigator=_StubInvestigator(object(), error=error),
        verifier=_StubVerifier(object()),
        policy=_workflow_policy(),
    )

    outcome = orchestrator.run(packet)

    assert outcome.failure is failure


def test_orchestrator_rejects_untyped_or_mismatched_agent_results_and_stale_data() -> None:
    packet = canonical_packet()
    invalid_investigator = WorkflowOrchestrator(
        investigator=_StubInvestigator(object()),
        verifier=_StubVerifier(object()),
        policy=_workflow_policy(),
    )
    assert invalid_investigator.run(packet).failure is WorkflowFailure.INVALID_SCHEMA

    mismatched = WorkflowOrchestrator(
        investigator=_StubInvestigator(_diagnosed(packet, incident_id="ep-other1")),
        verifier=_StubVerifier(object()),
        policy=_workflow_policy(),
    )
    assert mismatched.run(packet).failure is WorkflowFailure.INVALID_SCHEMA

    stale = WorkflowOrchestrator(
        investigator=_StubInvestigator(_diagnosed(packet)),
        verifier=_StubVerifier(object()),
        policy=_workflow_policy(),
        clock=lambda: datetime.now(UTC),
    )
    assert stale.run(packet).failure is WorkflowFailure.STALE_REQUIRED_DATA


def test_orchestrator_preserves_verifier_block_and_rejects_invalid_revision_allowance() -> None:
    packet = canonical_packet()
    investigation = _diagnosed(packet)
    blocked = WorkflowOrchestrator(
        investigator=_StubInvestigator(investigation),
        verifier=_StubVerifier(_verification(packet, investigation, VerificationStatus.BLOCKED)),
        policy=_workflow_policy(),
    )
    assert blocked.run(packet).failure is WorkflowFailure.BLOCKED

    invalid_allowance = _StubVerifier(
        _verification(packet, investigation, VerificationStatus.REVISION_REQUIRED)
    )
    invalid_allowance.lookup_allowance = object()
    outcome = WorkflowOrchestrator(
        investigator=_StubInvestigator(investigation),
        verifier=invalid_allowance,
        policy=_workflow_policy(),
    ).run(packet)
    assert outcome.failure is WorkflowFailure.INVALID_SCHEMA


def test_required_data_freshness_rejects_wrong_inputs_and_old_packets() -> None:
    packet = canonical_packet()
    assert required_data_is_fresh(object(), now=packet.detected_at) is False
    assert required_data_is_fresh(packet, now=packet.detected_at, max_age_seconds=-1) is False
    assert required_data_is_fresh(packet, now=datetime.now(UTC)) is False
