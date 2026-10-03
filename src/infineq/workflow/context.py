"""Strict immutable state captured by one local workflow run."""

from __future__ import annotations

from collections.abc import Iterable
from typing import ClassVar

from pydantic import ConfigDict, Field, StrictInt, field_validator, model_validator

from infineq.detection.detector import DetectionRun
from infineq.schemas.action import ActionPlanV1
from infineq.schemas.common import EvidenceId, OpaqueId, StrictModel
from infineq.schemas.incident import IncidentPacketV1
from infineq.schemas.investigation import InvestigationResultV1
from infineq.schemas.verification import VerificationResultV1
from infineq.workflow.corrections import CorrectionPacketV1
from infineq.workflow.transitions import (
    PolicyTransitionDecision,
    WorkflowAuditEvent,
    WorkflowFailure,
    WorkflowState,
)


class WorkflowToolBudget(StrictModel):
    """Immutable successful-call accounting for one agent boundary."""

    max_calls: StrictInt = Field(ge=0, le=6)
    used_calls: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def used_calls_must_fit(self) -> WorkflowToolBudget:
        if self.used_calls > self.max_calls:
            raise ValueError("used tool calls exceed the workflow budget")
        return self

    @property
    def remaining_calls(self) -> int:
        """Return the calls still available to this boundary."""

        return self.max_calls - self.used_calls

    @property
    def remaining(self) -> int:
        """Compatibility alias for the remaining-call count."""

        return self.remaining_calls

    @property
    def limit(self) -> int:
        """Compatibility alias for the fixed maximum call count."""

        return self.max_calls

    @property
    def used(self) -> int:
        """Compatibility alias for successful calls used."""

        return self.used_calls


class WorkflowContext(StrictModel):
    """Typed, immutable snapshot of all state passed between workflow steps."""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid",
        frozen=True,
        arbitrary_types_allowed=True,
    )

    workflow_id: OpaqueId
    incident_packet: IncidentPacketV1 | None = None
    detection_run: DetectionRun | None = None
    current_investigation: InvestigationResultV1 | None = None
    previous_investigation: InvestigationResultV1 | None = None
    verification: VerificationResultV1 | None = None
    correction_packet: CorrectionPacketV1 | None = None
    revision_count: StrictInt = Field(ge=0, le=1)
    returned_evidence_ids: tuple[EvidenceId, ...] = ()
    investigator_returned_evidence_ids: tuple[EvidenceId, ...] = ()
    verifier_returned_evidence_ids: tuple[EvidenceId, ...] = ()
    investigator_tool_budget: WorkflowToolBudget
    verifier_tool_budget: WorkflowToolBudget
    policy_decision: PolicyTransitionDecision | None = None
    final_state: WorkflowState
    audit_events: tuple[WorkflowAuditEvent, ...] = ()

    @field_validator("returned_evidence_ids", mode="before")
    @classmethod
    def canonicalize_returned_evidence_ids(cls, value: object) -> object:
        if isinstance(value, (str, bytes)):
            raise TypeError("returned evidence IDs must be an iterable of IDs")
        if isinstance(value, Iterable):
            return tuple(sorted(set(value)))
        return value

    @field_validator(
        "investigator_returned_evidence_ids",
        "verifier_returned_evidence_ids",
        mode="before",
    )
    @classmethod
    def canonicalize_agent_evidence_ids(cls, value: object) -> object:
        return cls.canonicalize_returned_evidence_ids(value)

    @field_validator("policy_decision")
    @classmethod
    def require_typed_policy_decision(
        cls, value: PolicyTransitionDecision | None
    ) -> PolicyTransitionDecision | None:
        if value is not None and not isinstance(value, PolicyTransitionDecision):
            raise TypeError("policy decision must be typed deterministic policy output")
        return value

    @model_validator(mode="after")
    def bind_all_component_ids(self) -> WorkflowContext:
        packet = self.incident_packet
        if packet is None:
            detection = self.detection_run
            if not isinstance(detection, DetectionRun):
                raise ValueError("packetless workflow context requires a typed detection run")
            if detection.packet is not None or detection.decision not in {"no_incident", "abstain"}:
                raise ValueError(
                    "packetless workflow context requires a no-incident or abstention detection"
                )
            if any(
                value is not None
                for value in (
                    self.current_investigation,
                    self.previous_investigation,
                    self.verification,
                    self.correction_packet,
                    self.policy_decision,
                )
            ):
                raise ValueError("no-incident context cannot contain agent or policy output")
            if (
                self.revision_count != 0
                or self.returned_evidence_ids
                or self.investigator_returned_evidence_ids
                or self.verifier_returned_evidence_ids
                or self.investigator_tool_budget.used_calls != 0
                or self.verifier_tool_budget.used_calls != 0
                or self.final_state is not WorkflowState.CLOSED
            ):
                raise ValueError("no-incident context must be an untouched closed workflow")
        else:
            if self.detection_run is not None:
                detection_packet = self.detection_run.packet
                if detection_packet is None or detection_packet.incident_id != packet.incident_id:
                    raise ValueError("detection does not belong to the incident packet")
            packet_id = packet.incident_id
            packet_evidence_ids = {item.evidence_id for item in packet.evidence_refs}
            for evidence_ids in (
                self.returned_evidence_ids,
                self.investigator_returned_evidence_ids,
                self.verifier_returned_evidence_ids,
            ):
                if not set(evidence_ids).issubset(packet_evidence_ids):
                    raise ValueError("returned evidence does not belong to the incident packet")
            for investigation in (self.previous_investigation, self.current_investigation):
                if investigation is not None and investigation.incident_id != packet_id:
                    raise ValueError("investigation does not belong to the incident packet")
            if self.verification is not None:
                if self.verification.incident_id != packet_id:
                    raise ValueError("verification does not belong to the incident packet")
                if (
                    self.current_investigation is not None
                    and self.verification.investigation_id
                    != self.current_investigation.investigation_id
                ):
                    raise ValueError("verification does not match the current investigation")
            if self.correction_packet is not None:
                if self.correction_packet.incident_id != packet_id:
                    raise ValueError("correction does not belong to the incident packet")
                if (
                    self.previous_investigation is not None
                    and self.correction_packet.investigation_id
                    != self.previous_investigation.investigation_id
                ):
                    raise ValueError("correction does not match the previous investigation")
                if self.revision_count != 1:
                    raise ValueError("a correction packet requires revision one")
            if self.revision_count == 1 and self.correction_packet is None:
                raise ValueError("revision one requires a correction packet")
        for event in self.audit_events:
            if event.workflow_id != self.workflow_id:
                raise ValueError("audit event belongs to a different workflow")
        if self.audit_events and self.audit_events[-1].to_state is not self.final_state:
            raise ValueError("final state must match the terminal audit transition")
        return self

    @property
    def packet(self) -> IncidentPacketV1 | None:
        """Short alias for the exact incident packet, when one exists."""

        return self.incident_packet

    @property
    def state(self) -> WorkflowState:
        """Alias for the final finite workflow state."""

        return self.final_state

    @property
    def investigator_tool_calls(self) -> int:
        """Successful Investigator calls used in the run."""

        return self.investigator_tool_budget.used_calls

    @property
    def verifier_tool_calls(self) -> int:
        """Successful Verifier calls used in the run."""

        return self.verifier_tool_budget.used_calls

    @property
    def investigator_evidence_ids(self) -> tuple[EvidenceId, ...]:
        """Alias for evidence returned by the Investigator boundary."""

        return self.investigator_returned_evidence_ids

    @property
    def verifier_evidence_ids(self) -> tuple[EvidenceId, ...]:
        """Alias for evidence returned by the Verifier boundary."""

        return self.verifier_returned_evidence_ids


class WorkflowOutcome(StrictModel):
    """Typed result returned for both successful and failed workflow paths."""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid",
        frozen=True,
        arbitrary_types_allowed=True,
    )

    context: WorkflowContext
    final_state: WorkflowState
    audit_events: tuple[WorkflowAuditEvent, ...]
    action_card: ActionPlanV1 | None = None
    failure: WorkflowFailure | None = None

    @model_validator(mode="after")
    def context_and_audit_must_match(self) -> WorkflowOutcome:
        if self.final_state is not self.context.final_state:
            raise ValueError("outcome and context final states differ")
        if self.audit_events != self.context.audit_events:
            raise ValueError("outcome and context audit events differ")
        if self.final_state is not WorkflowState.AWAITING_HUMAN and self.action_card is not None:
            raise ValueError("an action card requires the human-approval boundary")
        return self

    @property
    def state(self) -> WorkflowState:
        """Alias for the final finite workflow state."""

        return self.final_state

    @property
    def action_plan(self) -> ActionPlanV1 | None:
        """Alias for the unrendered, typed plan at the approval boundary."""

        return self.action_card

    @property
    def success(self) -> bool:
        """Whether the run reached the human-approval boundary."""

        return self.final_state is WorkflowState.AWAITING_HUMAN


ToolBudget = WorkflowToolBudget

__all__ = ["ToolBudget", "WorkflowContext", "WorkflowOutcome", "WorkflowToolBudget"]
