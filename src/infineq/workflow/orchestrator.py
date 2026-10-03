"""Deterministic orchestration for the first Phase 5 workflow slice."""

from __future__ import annotations

from collections.abc import Callable, Collection
from datetime import UTC, datetime
from typing import Protocol

from infineq.detection.detector import DetectionRun
from infineq.errors import (
    AgentTimeoutError,
    DataMissingError,
    DataStaleError,
    ToolPolicyDeniedError,
    ToolTransportError,
)
from infineq.evidence.tool_schemas import PolicyResponse
from infineq.schemas.evidence import DataQualityStatus
from infineq.schemas.incident import IncidentPacketV1
from infineq.schemas.investigation import Disposition, InvestigationResultV1
from infineq.schemas.verification import VerificationResultV1, VerificationStatus
from infineq.workflow.context import WorkflowContext, WorkflowOutcome, WorkflowToolBudget
from infineq.workflow.corrections import (
    CorrectionPacketV1,
    LookupAllowanceV1,
    build_correction_packet,
)
from infineq.workflow.policy import evaluate_presentation_policy
from infineq.workflow.state_machine import WorkflowStateMachine
from infineq.workflow.transitions import (
    PolicyTransitionDecision,
    WorkflowActor,
    WorkflowFailure,
    WorkflowState,
)


class InvestigatorProtocol(Protocol):
    """Narrow Investigator dependency used by the finite workflow."""

    def investigate(
        self,
        packet: IncidentPacketV1,
        *,
        correction_packet: CorrectionPacketV1 | None = None,
    ) -> InvestigationResultV1:
        """Return one typed investigation for the exact packet."""

        ...


class VerifierProtocol(Protocol):
    """Narrow Verifier dependency used by the finite workflow."""

    def verify(
        self,
        packet: IncidentPacketV1,
        investigation: InvestigationResultV1,
    ) -> VerificationResultV1:
        """Return one typed verification for the exact investigation."""

        ...


def _safe_attribute(value: object, name: str, default: object = None) -> object:
    """Read optional run metadata without allowing metadata access to crash a run."""

    try:
        result = getattr(value, name, default)
    except Exception:
        return default
    if callable(result):
        try:
            return result()
        except Exception:
            return default
    return result


def _agent_value(agent: object, name: str, default: object) -> object:
    """Read one optional agent accounting attribute."""

    return _safe_attribute(agent, name, default)


def _agent_status(agent: object) -> str | None:
    """Return a normalized last-run status when an agent exposes one."""

    last_run = _safe_attribute(agent, "last_run")
    status = _safe_attribute(last_run, "status")
    if status is None:
        return None
    value = _safe_attribute(status, "value", status)
    return value if isinstance(value, str) else None


def _returned_evidence_ids(
    agent: object,
    *,
    fallback: Collection[str] = (),
) -> tuple[str, ...]:
    """Read safe evidence accounting, falling back only to typed citations."""

    value = _agent_value(agent, "returned_evidence_ids", None)
    if value is None:
        last_run = _safe_attribute(agent, "last_run")
        value = _safe_attribute(last_run, "returned_evidence_ids", ())
    if not isinstance(value, Collection) or isinstance(value, (str, bytes)):
        value = ()
    ids = {item for item in value if isinstance(item, str)}
    if not ids:
        ids.update(item for item in fallback if isinstance(item, str))
    return tuple(sorted(ids))


def _successful_tool_calls(agent: object) -> int:
    """Read a non-negative successful-call count from an optional agent record."""

    value = _agent_value(agent, "successful_tool_calls", None)
    if value is None:
        value = _agent_value(agent, "tool_calls_used", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _failure_for_exception(error: Exception) -> WorkflowFailure:
    """Map expected dependency failures to the finite workflow categories."""

    if isinstance(error, (AgentTimeoutError, TimeoutError)):
        return WorkflowFailure.TIMEOUT
    if isinstance(error, ToolTransportError):
        return WorkflowFailure.TOOL_TRANSPORT
    if isinstance(error, DataStaleError):
        return WorkflowFailure.STALE_REQUIRED_DATA
    if isinstance(error, (DataMissingError, ToolPolicyDeniedError)):
        return WorkflowFailure.BLOCKED
    return WorkflowFailure.INVALID_SCHEMA


def _result_failure(agent: object, result: object) -> WorkflowFailure | None:
    """Turn an agent's typed incomplete result into a terminal workflow failure."""

    status = _agent_status(agent)
    if status == "timeout":
        return WorkflowFailure.TIMEOUT
    if status == "tool_transport_error":
        return WorkflowFailure.TOOL_TRANSPORT
    if (
        isinstance(result, InvestigationResultV1)
        and result.disposition is Disposition.ANALYSIS_INCOMPLETE
    ):
        return WorkflowFailure.INVALID_SCHEMA
    return None


MAX_REQUIRED_DATA_AGE_SECONDS = 10.0


def required_data_is_fresh(
    packet: IncidentPacketV1,
    *,
    now: datetime,
    max_age_seconds: float = MAX_REQUIRED_DATA_AGE_SECONDS,
) -> bool:
    """Check packet quality and evidence timestamps without consulting an agent."""

    if not isinstance(packet, IncidentPacketV1):
        return False
    if not isinstance(now, datetime):
        return False
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    now = now.astimezone(UTC)
    if (
        packet.data_quality.status is not DataQualityStatus.GOOD
        or packet.data_quality.freshness_seconds > max_age_seconds
    ):
        return False
    return all(
        (now - evidence.observed_at).total_seconds() <= max_age_seconds
        for evidence in packet.evidence_refs
    )


def validate_corrected_evidence_use(
    *,
    packet: IncidentPacketV1,
    correction_packet: CorrectionPacketV1,
    corrected_investigation: InvestigationResultV1,
    investigator_calls_before: int,
    investigator_calls_after: int,
) -> None:
    """Enforce evidence reuse and the single explicitly permitted lookup."""

    if corrected_investigation.incident_id != packet.incident_id:
        raise ValueError("corrected investigation does not match the packet")
    packet_evidence_ids = {item.evidence_id for item in packet.evidence_refs}
    corrected_ids = set(corrected_investigation.cited_evidence_ids)
    if not corrected_ids.issubset(packet_evidence_ids):
        raise ValueError("corrected investigation cites evidence outside the packet")

    reusable_ids = set(correction_packet.reuse_evidence_ids)
    new_ids = corrected_ids - reusable_ids
    if correction_packet.lookup_allowance is None and new_ids:
        raise ValueError("corrected investigation introduced an unapproved evidence lookup")
    allowed_correction_calls = 1 if correction_packet.lookup_allowance is not None else 0
    if investigator_calls_after < investigator_calls_before:
        raise ValueError("Investigator accounting moved backwards")
    if investigator_calls_after - investigator_calls_before > allowed_correction_calls:
        raise ValueError("correction exceeded its explicit lookup allowance")
    if investigator_calls_after > 6:
        raise ValueError("corrected investigation exceeded the Investigator budget")


class WorkflowOrchestrator:
    """Run the typed Investigator → Verifier boundary without an executor."""

    def __init__(
        self,
        *,
        investigator: InvestigatorProtocol,
        verifier: VerifierProtocol,
        policy: PolicyResponse,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(policy, PolicyResponse):
            raise TypeError("workflow policy must be a typed PolicyResponse")
        self._investigator = investigator
        self._verifier = verifier
        self._policy = policy
        self._clock = clock

    def _call_investigator(
        self,
        packet: IncidentPacketV1,
        *,
        correction_packet: CorrectionPacketV1 | None = None,
    ) -> tuple[InvestigationResultV1 | None, WorkflowFailure | None]:
        try:
            if correction_packet is None:
                result = self._investigator.investigate(packet)
            else:
                result = self._investigator.investigate(
                    packet,
                    correction_packet=correction_packet,
                )
        except Exception as error:
            return None, _failure_for_exception(error)
        if not isinstance(result, InvestigationResultV1):
            return None, WorkflowFailure.INVALID_SCHEMA
        if result.incident_id != packet.incident_id:
            return None, WorkflowFailure.INVALID_SCHEMA
        return result, _result_failure(self._investigator, result)

    def _call_verifier(
        self,
        packet: IncidentPacketV1,
        investigation: InvestigationResultV1,
    ) -> tuple[VerificationResultV1 | None, WorkflowFailure | None]:
        try:
            result = self._verifier.verify(packet, investigation)
        except Exception as error:
            return None, _failure_for_exception(error)
        if not isinstance(result, VerificationResultV1):
            return None, WorkflowFailure.INVALID_SCHEMA
        if (
            result.incident_id != packet.incident_id
            or result.investigation_id != investigation.investigation_id
        ):
            return None, WorkflowFailure.INVALID_SCHEMA
        return result, _result_failure(self._verifier, result)

    def _lookup_allowance(self) -> tuple[LookupAllowanceV1 | None, WorkflowFailure | None]:
        value = _agent_value(self._verifier, "lookup_allowance", None)
        if value is None:
            value = _agent_value(self._verifier, "permitted_lookup", None)
        if value is None:
            return None, None
        if not isinstance(value, LookupAllowanceV1):
            return None, WorkflowFailure.INVALID_SCHEMA
        return value, None

    def _required_data_is_fresh(self, packet: IncidentPacketV1) -> bool:
        now = packet.detected_at
        if self._clock is not None:
            try:
                now = self._clock()
            except Exception:
                return False
        return required_data_is_fresh(packet, now=now)

    def run_detection(
        self,
        detection: DetectionRun,
        *,
        workflow_id: str | None = None,
    ) -> WorkflowOutcome:
        """Close a typed no-incident detector result without inventing a packet."""

        if not isinstance(detection, DetectionRun):
            raise TypeError("workflow accepts only a typed DetectionRun")
        if detection.packet is not None:
            return self.run(detection.packet, workflow_id=workflow_id)
        if detection.decision not in {"no_incident", "abstain"}:
            raise ValueError("a packetless detection is not a supported abstention result")

        resolved_workflow_id = workflow_id or f"workflow-{detection.episode_id}"
        machine = WorkflowStateMachine(workflow_id=resolved_workflow_id)
        machine.transition(
            WorkflowState.DETECTED,
            actor=WorkflowActor.DETECTOR,
            reason=(
                "detector returned no incident"
                if detection.decision == "no_incident"
                else "detector abstained because required data was not decision-ready"
            ),
        )
        machine.transition(
            WorkflowState.CLOSED,
            actor=WorkflowActor.WORKFLOW,
            reason=(
                "no incident; agent boundaries were not invoked"
                if detection.decision == "no_incident"
                else "data quality abstention; agent boundaries were not invoked"
            ),
        )
        context = WorkflowContext(
            workflow_id=resolved_workflow_id,
            incident_packet=None,
            detection_run=detection,
            revision_count=0,
            returned_evidence_ids=(),
            investigator_returned_evidence_ids=(),
            verifier_returned_evidence_ids=(),
            investigator_tool_budget=WorkflowToolBudget(max_calls=6, used_calls=0),
            verifier_tool_budget=WorkflowToolBudget(max_calls=2, used_calls=0),
            policy_decision=None,
            final_state=machine.state,
            audit_events=machine.audit_events,
        )
        return WorkflowOutcome(
            context=context,
            final_state=machine.state,
            audit_events=machine.audit_events,
            action_card=None,
            failure=None,
        )

    def run(
        self,
        packet: IncidentPacketV1 | DetectionRun,
        *,
        workflow_id: str | None = None,
    ) -> WorkflowOutcome:
        """Run one episode through at most one Investigator correction cycle."""

        if isinstance(packet, DetectionRun):
            return self.run_detection(packet, workflow_id=workflow_id)
        if not isinstance(packet, IncidentPacketV1):
            raise TypeError("workflow accepts only IncidentPacketV1 or DetectionRun")
        resolved_workflow_id = workflow_id or f"workflow-{packet.incident_id}"
        machine = WorkflowStateMachine(workflow_id=resolved_workflow_id)
        machine.transition(
            WorkflowState.DETECTED,
            actor=WorkflowActor.DETECTOR,
            reason="deterministic incident packet accepted",
        )
        machine.transition(
            WorkflowState.INVESTIGATING,
            actor=WorkflowActor.WORKFLOW,
            reason="investigation started",
        )

        previous_investigation: InvestigationResultV1 | None = None
        correction_packet: CorrectionPacketV1 | None = None
        investigation, failure = self._call_investigator(packet)
        if (
            investigation is not None
            and failure is None
            and investigation.disposition is not Disposition.DIAGNOSED
        ):
            failure = WorkflowFailure.BLOCKED
        if failure is not None:
            machine.fail(
                failure,
                actor=WorkflowActor.INVESTIGATOR,
                reason="initial Investigator run did not complete",
            )
            return self._outcome(
                machine=machine,
                packet=packet,
                investigation=investigation,
                previous_investigation=None,
                verification=None,
                correction_packet=None,
                policy_decision=None,
                failure=failure,
            )

        assert investigation is not None
        machine.transition(
            WorkflowState.VERIFYING,
            actor=WorkflowActor.WORKFLOW,
            reason="investigation completed",
        )
        if not self._required_data_is_fresh(packet):
            failure = WorkflowFailure.STALE_REQUIRED_DATA
            machine.fail(
                failure,
                reason="required evidence became stale before Verifier boundary",
            )
            return self._outcome(
                machine=machine,
                packet=packet,
                investigation=investigation,
                previous_investigation=None,
                verification=None,
                correction_packet=None,
                policy_decision=None,
                failure=failure,
            )
        verification, failure = self._call_verifier(packet, investigation)
        if failure is not None:
            machine.fail(
                failure,
                actor=WorkflowActor.VERIFIER,
                reason="initial Verifier run did not complete",
            )
            return self._outcome(
                machine=machine,
                packet=packet,
                investigation=investigation,
                previous_investigation=None,
                verification=verification,
                correction_packet=None,
                policy_decision=None,
                failure=failure,
            )
        assert verification is not None

        if verification.status is VerificationStatus.REVISION_REQUIRED:
            lookup_allowance, failure = self._lookup_allowance()
            if failure is not None:
                machine.fail(failure, reason="Verifier lookup allowance was invalid")
                return self._outcome(
                    machine=machine,
                    packet=packet,
                    investigation=investigation,
                    previous_investigation=None,
                    verification=verification,
                    correction_packet=None,
                    policy_decision=None,
                    failure=failure,
                )
            try:
                correction_packet = build_correction_packet(
                    verification,
                    packet=packet,
                    investigation=investigation,
                    prior_returned_evidence_ids=_returned_evidence_ids(
                        self._investigator,
                        fallback=investigation.cited_evidence_ids,
                    ),
                    remaining_investigator_tool_calls=max(
                        0,
                        6 - _successful_tool_calls(self._investigator),
                    ),
                    lookup_allowance=lookup_allowance,
                )
            except Exception as error:
                failure = _failure_for_exception(error)
                machine.fail(failure, reason="typed correction packet could not be built")
                return self._outcome(
                    machine=machine,
                    packet=packet,
                    investigation=investigation,
                    previous_investigation=None,
                    verification=verification,
                    correction_packet=None,
                    policy_decision=None,
                    failure=failure,
                )

            machine.request_revision(
                actor=WorkflowActor.VERIFIER,
                reason="verifier requested one typed correction",
            )
            machine.transition(
                WorkflowState.INVESTIGATING,
                actor=WorkflowActor.WORKFLOW,
                reason="one permitted correction started",
            )
            previous_investigation = investigation
            calls_before = _successful_tool_calls(self._investigator)
            corrected, failure = self._call_investigator(
                packet,
                correction_packet=correction_packet,
            )
            calls_after = _successful_tool_calls(self._investigator)
            if failure is not None:
                machine.fail(
                    failure,
                    actor=WorkflowActor.INVESTIGATOR,
                    reason="corrected Investigator run did not complete",
                )
                return self._outcome(
                    machine=machine,
                    packet=packet,
                    investigation=corrected,
                    previous_investigation=previous_investigation,
                    verification=None,
                    correction_packet=correction_packet,
                    policy_decision=None,
                    failure=failure,
                )
            assert corrected is not None
            try:
                validate_corrected_evidence_use(
                    packet=packet,
                    correction_packet=correction_packet,
                    corrected_investigation=corrected,
                    investigator_calls_before=calls_before,
                    investigator_calls_after=calls_after,
                )
            except Exception as error:
                failure = _failure_for_exception(error)
                machine.fail(failure, reason="corrected evidence use was not permitted")
                return self._outcome(
                    machine=machine,
                    packet=packet,
                    investigation=corrected,
                    previous_investigation=previous_investigation,
                    verification=None,
                    correction_packet=correction_packet,
                    policy_decision=None,
                    failure=failure,
                )
            investigation = corrected
            machine.transition(
                WorkflowState.VERIFYING,
                actor=WorkflowActor.WORKFLOW,
                reason="corrected investigation completed",
            )
            if not self._required_data_is_fresh(packet):
                failure = WorkflowFailure.STALE_REQUIRED_DATA
                machine.fail(
                    failure,
                    reason="required evidence became stale before Verifier boundary",
                )
                return self._outcome(
                    machine=machine,
                    packet=packet,
                    investigation=investigation,
                    previous_investigation=previous_investigation,
                    verification=None,
                    correction_packet=correction_packet,
                    policy_decision=None,
                    failure=failure,
                )
            verification, failure = self._call_verifier(packet, investigation)
            if failure is not None:
                machine.fail(
                    failure,
                    actor=WorkflowActor.VERIFIER,
                    reason="corrected Verifier run did not complete",
                )
                return self._outcome(
                    machine=machine,
                    packet=packet,
                    investigation=investigation,
                    previous_investigation=previous_investigation,
                    verification=verification,
                    correction_packet=correction_packet,
                    policy_decision=None,
                    failure=failure,
                )
            assert verification is not None
            if verification.status is VerificationStatus.REVISION_REQUIRED:
                machine.request_revision(
                    actor=WorkflowActor.VERIFIER,
                    reason="second correction request denied",
                )
                return self._outcome(
                    machine=machine,
                    packet=packet,
                    investigation=investigation,
                    previous_investigation=previous_investigation,
                    verification=verification,
                    correction_packet=correction_packet,
                    policy_decision=None,
                    failure=WorkflowFailure.BLOCKED,
                )

        if verification.status is VerificationStatus.BLOCKED:
            machine.fail(WorkflowFailure.BLOCKED, reason="Verifier blocked presentation")
            return self._outcome(
                machine=machine,
                packet=packet,
                investigation=investigation,
                previous_investigation=previous_investigation,
                verification=verification,
                correction_packet=correction_packet,
                policy_decision=None,
                failure=WorkflowFailure.BLOCKED,
            )

        if verification.status is not VerificationStatus.VERIFIED:
            machine.fail(
                WorkflowFailure.INVALID_SCHEMA,
                reason="Verifier did not return a usable status",
            )
            return self._outcome(
                machine=machine,
                packet=packet,
                investigation=investigation,
                previous_investigation=previous_investigation,
                verification=verification,
                correction_packet=correction_packet,
                policy_decision=None,
                failure=WorkflowFailure.INVALID_SCHEMA,
            )

        machine.transition(
            WorkflowState.PRESENTABLE,
            actor=WorkflowActor.WORKFLOW,
            reason="verification completed",
        )
        try:
            policy_decision = evaluate_presentation_policy(
                investigation=investigation,
                verification=verification,
                policy=self._policy,
                packet=packet,
            )
        except Exception as error:
            failure = _failure_for_exception(error)
            machine.transition(
                WorkflowState.INDETERMINATE,
                actor=WorkflowActor.WORKFLOW,
                reason="deterministic policy evaluation failed",
            )
            return self._outcome(
                machine=machine,
                packet=packet,
                investigation=investigation,
                previous_investigation=previous_investigation,
                verification=verification,
                correction_packet=correction_packet,
                policy_decision=None,
                failure=failure,
            )
        machine.transition(
            WorkflowState.AWAITING_HUMAN,
            actor=WorkflowActor.POLICY,
            reason="deterministic presentation policy evaluated",
            policy_decision=policy_decision,
        )
        return self._outcome(
            machine=machine,
            packet=packet,
            investigation=investigation,
            previous_investigation=previous_investigation,
            verification=verification,
            correction_packet=correction_packet,
            policy_decision=policy_decision,
            failure=None if policy_decision.allowed else WorkflowFailure.BLOCKED,
        )

    orchestrate = run

    def _outcome(
        self,
        *,
        machine: WorkflowStateMachine,
        packet: IncidentPacketV1,
        investigation: InvestigationResultV1 | None,
        previous_investigation: InvestigationResultV1 | None,
        verification: VerificationResultV1 | None,
        correction_packet: CorrectionPacketV1 | None,
        policy_decision: PolicyTransitionDecision | None,
        failure: WorkflowFailure | None,
    ) -> WorkflowOutcome:
        packet_evidence_ids = {item.evidence_id for item in packet.evidence_refs}
        investigation_fallback = (
            investigation.cited_evidence_ids
            if investigation is not None
            else previous_investigation.cited_evidence_ids
            if previous_investigation is not None
            else ()
        )
        investigator_evidence_ids = tuple(
            item
            for item in _returned_evidence_ids(
                self._investigator,
                fallback=investigation_fallback,
            )
            if item in packet_evidence_ids
        )
        verifier_fallback = (
            tuple(
                evidence_id
                for check in verification.claim_checks
                for evidence_id in check.evidence_ids
            )
            if verification is not None
            else ()
        )
        verifier_evidence_ids = tuple(
            item
            for item in _returned_evidence_ids(
                self._verifier,
                fallback=verifier_fallback,
            )
            if item in packet_evidence_ids
        )
        returned_evidence_ids = tuple(
            sorted(set(investigator_evidence_ids) | set(verifier_evidence_ids))
        )
        investigator_calls = min(max(_successful_tool_calls(self._investigator), 0), 6)
        verifier_calls = min(max(_successful_tool_calls(self._verifier), 0), 2)
        context = WorkflowContext(
            workflow_id=machine.workflow_id,
            incident_packet=packet,
            current_investigation=investigation,
            previous_investigation=previous_investigation,
            verification=verification,
            correction_packet=correction_packet,
            revision_count=machine.revision_count,
            returned_evidence_ids=returned_evidence_ids,
            investigator_returned_evidence_ids=investigator_evidence_ids,
            verifier_returned_evidence_ids=verifier_evidence_ids,
            investigator_tool_budget=WorkflowToolBudget(
                max_calls=6,
                used_calls=investigator_calls,
            ),
            verifier_tool_budget=WorkflowToolBudget(
                max_calls=2,
                used_calls=verifier_calls,
            ),
            policy_decision=policy_decision,
            final_state=machine.state,
            audit_events=machine.audit_events,
        )
        action_card = (
            investigation.proposed_action
            if machine.state is WorkflowState.AWAITING_HUMAN and investigation is not None
            else None
        )
        return WorkflowOutcome(
            context=context,
            final_state=machine.state,
            audit_events=machine.audit_events,
            action_card=action_card,
            failure=failure,
        )


Orchestrator = WorkflowOrchestrator

__all__ = [
    "InvestigatorProtocol",
    "Orchestrator",
    "VerifierProtocol",
    "WorkflowOrchestrator",
    "required_data_is_fresh",
    "validate_corrected_evidence_use",
]
