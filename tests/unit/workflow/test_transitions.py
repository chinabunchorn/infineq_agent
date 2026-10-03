from datetime import UTC, datetime

import pytest

from infineq.workflow.state_machine import (
    WorkflowState,
    WorkflowStateMachine,
    WorkflowTransitionError,
)
from infineq.workflow.transitions import (
    FROZEN_TRANSITIONS,
    WorkflowActor,
    WorkflowFailure,
)


def test_workflow_audit_actors_cover_each_frozen_authority_boundary() -> None:
    assert tuple(actor.value for actor in WorkflowActor) == (
        "workflow",
        "detector",
        "investigator",
        "verifier",
        "policy",
        "human",
        "executor",
        "recovery",
    )


EXPECTED_TRANSITIONS = {
    WorkflowState.REPLAY_READY: {WorkflowState.DETECTED},
    WorkflowState.DETECTED: {WorkflowState.INVESTIGATING, WorkflowState.CLOSED},
    WorkflowState.INVESTIGATING: {
        WorkflowState.VERIFYING,
        WorkflowState.INDETERMINATE,
        WorkflowState.ANALYSIS_INCOMPLETE,
    },
    WorkflowState.VERIFYING: {
        WorkflowState.REVISION_REQUESTED,
        WorkflowState.PRESENTABLE,
        WorkflowState.INDETERMINATE,
        WorkflowState.ANALYSIS_INCOMPLETE,
    },
    WorkflowState.REVISION_REQUESTED: {
        WorkflowState.INVESTIGATING,
        WorkflowState.INDETERMINATE,
        WorkflowState.ANALYSIS_INCOMPLETE,
    },
    WorkflowState.PRESENTABLE: {WorkflowState.AWAITING_HUMAN, WorkflowState.INDETERMINATE},
    WorkflowState.INDETERMINATE: {WorkflowState.CLOSED},
    WorkflowState.ANALYSIS_INCOMPLETE: {WorkflowState.CLOSED},
    WorkflowState.AWAITING_HUMAN: {WorkflowState.APPROVED, WorkflowState.REJECTED},
    WorkflowState.REJECTED: {WorkflowState.CLOSED},
    WorkflowState.APPROVED: {WorkflowState.ACTION_APPLIED},
    WorkflowState.ACTION_APPLIED: {WorkflowState.VERIFYING_RECOVERY},
    WorkflowState.VERIFYING_RECOVERY: {WorkflowState.CLOSED},
    WorkflowState.CLOSED: set(),
}


def test_frozen_transition_table_contains_only_the_declared_workflow_edges() -> None:
    assert {
        state: frozenset(targets) for state, targets in EXPECTED_TRANSITIONS.items()
    } == FROZEN_TRANSITIONS


def test_nominal_workflow_edges_emit_one_redacted_audit_event_each() -> None:
    machine = WorkflowStateMachine(workflow_id="workflow-ep-61d8aa")

    for target in (
        WorkflowState.DETECTED,
        WorkflowState.INVESTIGATING,
        WorkflowState.VERIFYING,
        WorkflowState.PRESENTABLE,
    ):
        machine.transition(target, actor=WorkflowActor.WORKFLOW, reason="normal progress")

    assert machine.state is WorkflowState.PRESENTABLE
    assert len(machine.audit_events) == 4
    assert [event.from_state for event in machine.audit_events] == [
        WorkflowState.REPLAY_READY,
        WorkflowState.DETECTED,
        WorkflowState.INVESTIGATING,
        WorkflowState.VERIFYING,
    ]
    assert [event.to_state for event in machine.audit_events] == [
        WorkflowState.DETECTED,
        WorkflowState.INVESTIGATING,
        WorkflowState.VERIFYING,
        WorkflowState.PRESENTABLE,
    ]
    assert all(event.reason == "normal progress" for event in machine.audit_events)


def test_transition_audit_has_a_stable_id_and_clocked_timestamp() -> None:
    occurred_at = datetime(2026, 9, 14, 0, 0, tzinfo=UTC)
    machine = WorkflowStateMachine(workflow_id="workflow-ep-61d8aa", clock=lambda: occurred_at)

    event = machine.transition(
        WorkflowState.DETECTED,
        actor=WorkflowActor.WORKFLOW,
        reason="detector completed",
    )

    assert event.event_id == "audit-1"
    assert event.occurred_at == occurred_at
    assert event.revision_count == 0


def test_transition_audit_redacts_sensitive_reason_text() -> None:
    machine = WorkflowStateMachine(workflow_id="workflow-ep-61d8aa")

    event = machine.transition(
        WorkflowState.DETECTED,
        actor=WorkflowActor.DETECTOR,
        reason="token=secret-value path=/tmp/episode.json",
    )

    assert event.reason == "[REDACTED]"


def test_only_one_revision_cycle_is_allowed_and_second_revision_abstains() -> None:
    machine = WorkflowStateMachine(workflow_id="workflow-ep-61d8aa")
    for target in (
        WorkflowState.DETECTED,
        WorkflowState.INVESTIGATING,
        WorkflowState.VERIFYING,
    ):
        machine.transition(target, actor=WorkflowActor.WORKFLOW, reason="progress")

    first = machine.request_revision(reason="missing comparison")

    assert first.to_state is WorkflowState.REVISION_REQUESTED
    assert machine.revision_count == 1
    machine.transition(
        WorkflowState.INVESTIGATING,
        actor=WorkflowActor.WORKFLOW,
        reason="apply permitted correction",
    )
    machine.transition(WorkflowState.VERIFYING, actor=WorkflowActor.WORKFLOW, reason="verify again")

    second = machine.request_revision(reason="still missing comparison")

    assert second.to_state is WorkflowState.INDETERMINATE
    assert machine.state is WorkflowState.INDETERMINATE
    assert machine.revision_count == 1
    assert all(event.to_state is not WorkflowState.ACTION_APPLIED for event in machine.audit_events)


def test_generic_transition_cannot_bypass_the_revision_budget() -> None:
    machine = WorkflowStateMachine(workflow_id="workflow-ep-61d8aa")
    for target in (
        WorkflowState.DETECTED,
        WorkflowState.INVESTIGATING,
        WorkflowState.VERIFYING,
    ):
        machine.transition(target, actor=WorkflowActor.WORKFLOW, reason="progress")

    machine.transition(
        WorkflowState.REVISION_REQUESTED,
        actor=WorkflowActor.WORKFLOW,
        reason="first correction",
    )
    machine.transition(
        WorkflowState.INVESTIGATING,
        actor=WorkflowActor.WORKFLOW,
        reason="apply correction",
    )
    machine.transition(WorkflowState.VERIFYING, actor=WorkflowActor.WORKFLOW, reason="verify")

    machine.transition(
        WorkflowState.REVISION_REQUESTED,
        actor=WorkflowActor.WORKFLOW,
        reason="second correction",
    )

    assert machine.state is WorkflowState.INDETERMINATE
    assert machine.revision_count == 1


def test_failure_categories_end_in_safe_states_without_action_execution() -> None:
    machine = WorkflowStateMachine(workflow_id="workflow-ep-61d8aa")
    for target in (
        WorkflowState.DETECTED,
        WorkflowState.INVESTIGATING,
    ):
        machine.transition(target, actor=WorkflowActor.WORKFLOW, reason="progress")

    timeout_event = machine.fail(WorkflowFailure.TIMEOUT, reason="investigator timeout")

    assert timeout_event.to_state is WorkflowState.ANALYSIS_INCOMPLETE
    assert machine.state is WorkflowState.ANALYSIS_INCOMPLETE
    assert all(event.to_state is not WorkflowState.ACTION_APPLIED for event in machine.audit_events)

    stale_machine = WorkflowStateMachine(workflow_id="workflow-ep-61d8ab")
    for target in (
        WorkflowState.DETECTED,
        WorkflowState.INVESTIGATING,
        WorkflowState.VERIFYING,
    ):
        stale_machine.transition(target, actor=WorkflowActor.WORKFLOW, reason="progress")

    stale_event = stale_machine.fail(WorkflowFailure.STALE_REQUIRED_DATA, reason="stale data")

    assert stale_event.to_state is WorkflowState.INDETERMINATE
    assert stale_machine.state is WorkflowState.INDETERMINATE


def test_investigator_output_cannot_jump_directly_to_action_execution() -> None:
    machine = WorkflowStateMachine(workflow_id="workflow-ep-61d8aa")
    machine.transition(WorkflowState.DETECTED, actor=WorkflowActor.WORKFLOW, reason="detected")
    machine.transition(
        WorkflowState.INVESTIGATING,
        actor=WorkflowActor.WORKFLOW,
        reason="investigation started",
    )

    with pytest.raises(WorkflowTransitionError, match="direct action execution"):
        machine.transition(
            WorkflowState.ACTION_APPLIED,
            actor=WorkflowActor.WORKFLOW,
            reason="unsafe shortcut",
        )

    assert machine.state is WorkflowState.INVESTIGATING
    assert len(machine.audit_events) == 2
