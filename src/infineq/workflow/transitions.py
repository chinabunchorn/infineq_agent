"""Shared types for the finite workflow transition boundary."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class WorkflowState(StrEnum):
    """The finite states permitted by the v1 workflow."""

    REPLAY_READY = "REPLAY_READY"
    DETECTED = "DETECTED"
    INVESTIGATING = "INVESTIGATING"
    VERIFYING = "VERIFYING"
    REVISION_REQUESTED = "REVISION_REQUESTED"
    PRESENTABLE = "PRESENTABLE"
    INDETERMINATE = "INDETERMINATE"
    ANALYSIS_INCOMPLETE = "ANALYSIS_INCOMPLETE"
    AWAITING_HUMAN = "AWAITING_HUMAN"
    REJECTED = "REJECTED"
    APPROVED = "APPROVED"
    ACTION_APPLIED = "ACTION_APPLIED"
    VERIFYING_RECOVERY = "VERIFYING_RECOVERY"
    CLOSED = "CLOSED"


class WorkflowActor(StrEnum):
    """The component responsible for a transition."""

    WORKFLOW = "workflow"
    DETECTOR = "detector"
    INVESTIGATOR = "investigator"
    VERIFIER = "verifier"
    POLICY = "policy"
    HUMAN = "human"
    EXECUTOR = "executor"
    RECOVERY = "recovery"


class WorkflowFailure(StrEnum):
    """Failure categories with deterministic terminal-state mapping."""

    BLOCKED = "blocked"
    TIMEOUT = "timeout"
    INVALID_SCHEMA = "invalid_schema"
    STALE_REQUIRED_DATA = "stale_required_data"
    TOOL_TRANSPORT = "tool_transport"


@dataclass(frozen=True, slots=True)
class PolicyTransitionDecision:
    """Result of the deterministic presentation-policy gate."""

    allowed: bool
    reason: str
    source: str = "deterministic_policy"


@dataclass(frozen=True, slots=True)
class WorkflowAuditEvent:
    """Redacted metadata for one accepted state transition."""

    event_id: str
    workflow_id: str
    from_state: WorkflowState
    to_state: WorkflowState
    actor: WorkflowActor
    reason: str
    occurred_at: datetime
    revision_count: int


FROZEN_TRANSITIONS: dict[WorkflowState, frozenset[WorkflowState]] = {
    WorkflowState.REPLAY_READY: frozenset({WorkflowState.DETECTED}),
    WorkflowState.DETECTED: frozenset({WorkflowState.INVESTIGATING, WorkflowState.CLOSED}),
    WorkflowState.INVESTIGATING: frozenset(
        {
            WorkflowState.VERIFYING,
            WorkflowState.INDETERMINATE,
            WorkflowState.ANALYSIS_INCOMPLETE,
        }
    ),
    WorkflowState.VERIFYING: frozenset(
        {
            WorkflowState.REVISION_REQUESTED,
            WorkflowState.PRESENTABLE,
            WorkflowState.INDETERMINATE,
            WorkflowState.ANALYSIS_INCOMPLETE,
        }
    ),
    WorkflowState.REVISION_REQUESTED: frozenset(
        {
            WorkflowState.INVESTIGATING,
            WorkflowState.INDETERMINATE,
            WorkflowState.ANALYSIS_INCOMPLETE,
        }
    ),
    WorkflowState.PRESENTABLE: frozenset(
        {WorkflowState.AWAITING_HUMAN, WorkflowState.INDETERMINATE}
    ),
    WorkflowState.INDETERMINATE: frozenset({WorkflowState.CLOSED}),
    WorkflowState.ANALYSIS_INCOMPLETE: frozenset({WorkflowState.CLOSED}),
    WorkflowState.AWAITING_HUMAN: frozenset({WorkflowState.APPROVED, WorkflowState.REJECTED}),
    WorkflowState.REJECTED: frozenset({WorkflowState.CLOSED}),
    WorkflowState.APPROVED: frozenset({WorkflowState.ACTION_APPLIED}),
    WorkflowState.ACTION_APPLIED: frozenset({WorkflowState.VERIFYING_RECOVERY}),
    WorkflowState.VERIFYING_RECOVERY: frozenset({WorkflowState.CLOSED}),
    WorkflowState.CLOSED: frozenset(),
}


__all__ = [
    "FROZEN_TRANSITIONS",
    "PolicyTransitionDecision",
    "WorkflowActor",
    "WorkflowAuditEvent",
    "WorkflowFailure",
    "WorkflowState",
]
