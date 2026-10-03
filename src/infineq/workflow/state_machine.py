"""Finite workflow state holder."""

from collections.abc import Callable
from datetime import UTC, datetime

from infineq.evidence.audit import redact_error
from infineq.workflow.transitions import (
    FROZEN_TRANSITIONS,
    PolicyTransitionDecision,
    WorkflowActor,
    WorkflowAuditEvent,
    WorkflowFailure,
    WorkflowState,
)


class WorkflowTransitionError(ValueError):
    """Raised when a requested transition is outside the frozen graph."""


class WorkflowStateMachine:
    """Hold the current state and revision counter."""

    def __init__(
        self,
        *,
        workflow_id: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.workflow_id = workflow_id
        self._state = WorkflowState.REPLAY_READY
        self._revision_count = 0
        self._audit_events: list[WorkflowAuditEvent] = []
        self._clock = clock

    @property
    def state(self) -> WorkflowState:
        return self._state

    @property
    def revision_count(self) -> int:
        return self._revision_count

    @property
    def audit_events(self) -> tuple[WorkflowAuditEvent, ...]:
        return tuple(self._audit_events)

    def transition(
        self,
        target: WorkflowState,
        *,
        actor: WorkflowActor,
        reason: str,
        policy_decision: PolicyTransitionDecision | None = None,
    ) -> WorkflowAuditEvent:
        """Apply one currently allowed transition and emit its audit event."""

        if target is WorkflowState.AWAITING_HUMAN:
            return self._apply_presentation_policy(
                actor=actor,
                reason=reason,
                decision=policy_decision,
            )
        if policy_decision is not None:
            raise WorkflowTransitionError(
                "deterministic policy decision only authorizes presentation"
            )
        if target is WorkflowState.REVISION_REQUESTED:
            return self.request_revision(actor=actor, reason=reason)
        return self._apply_transition(target, actor=actor, reason=reason)

    def _apply_presentation_policy(
        self,
        *,
        actor: WorkflowActor,
        reason: str,
        decision: PolicyTransitionDecision | None,
    ) -> WorkflowAuditEvent:
        if actor is not WorkflowActor.POLICY or decision is None:
            raise WorkflowTransitionError(
                "PRESENTABLE -> AWAITING_HUMAN requires a deterministic policy decision"
            )
        if decision.source != "deterministic_policy":
            raise WorkflowTransitionError("only deterministic policy may authorize presentation")
        if not decision.allowed:
            return self._apply_transition(
                WorkflowState.INDETERMINATE,
                actor=WorkflowActor.POLICY,
                reason=decision.reason or reason,
            )
        return self._apply_transition(
            WorkflowState.AWAITING_HUMAN,
            actor=WorkflowActor.POLICY,
            reason=decision.reason or reason,
        )

    def _apply_transition(
        self,
        target: WorkflowState,
        *,
        actor: WorkflowActor,
        reason: str,
    ) -> WorkflowAuditEvent:
        if self._state is WorkflowState.INVESTIGATING and target is WorkflowState.ACTION_APPLIED:
            raise WorkflowTransitionError("direct action execution from investigation is forbidden")
        if target not in FROZEN_TRANSITIONS[self._state]:
            raise WorkflowTransitionError(
                f"transition {self._state.value} -> {target.value} is not allowed"
            )
        safe_reason = redact_error(reason) or "[REDACTED]"
        event = WorkflowAuditEvent(
            event_id=f"audit-{len(self._audit_events) + 1}",
            workflow_id=self.workflow_id,
            from_state=self._state,
            to_state=target,
            actor=actor,
            reason=safe_reason,
            occurred_at=self._now(),
            revision_count=self._revision_count,
        )
        self._state = target
        self._audit_events.append(event)
        return event

    def request_revision(
        self,
        *,
        reason: str,
        actor: WorkflowActor = WorkflowActor.WORKFLOW,
    ) -> WorkflowAuditEvent:
        """Request one correction, then fail closed on a second request."""

        if self._state is not WorkflowState.VERIFYING:
            raise WorkflowTransitionError("revision requests are only valid while verifying")
        if self._revision_count >= 1:
            return self._apply_transition(
                WorkflowState.INDETERMINATE,
                actor=actor,
                reason=f"second revision denied: {reason}",
            )
        self._revision_count = 1
        return self._apply_transition(
            WorkflowState.REVISION_REQUESTED,
            actor=actor,
            reason=reason,
        )

    def fail(
        self,
        failure: WorkflowFailure,
        *,
        reason: str,
        actor: WorkflowActor = WorkflowActor.WORKFLOW,
    ) -> WorkflowAuditEvent:
        """Map a typed failure to a terminal-safe workflow state."""

        target = (
            WorkflowState.INDETERMINATE
            if failure in {WorkflowFailure.BLOCKED, WorkflowFailure.STALE_REQUIRED_DATA}
            else WorkflowState.ANALYSIS_INCOMPLETE
        )
        return self._apply_transition(target, actor=actor, reason=f"{failure.value}: {reason}")

    def _now(self) -> datetime:
        value = self._clock() if self._clock is not None else datetime.now(UTC)
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


__all__ = ["WorkflowState", "WorkflowStateMachine", "WorkflowTransitionError"]
