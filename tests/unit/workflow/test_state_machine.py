import pytest

from infineq.workflow.state_machine import (
    WorkflowState,
    WorkflowStateMachine,
    WorkflowTransitionError,
)
from infineq.workflow.transitions import PolicyTransitionDecision, WorkflowActor, WorkflowFailure

FROZEN_STATES = (
    "REPLAY_READY",
    "DETECTED",
    "INVESTIGATING",
    "VERIFYING",
    "REVISION_REQUESTED",
    "PRESENTABLE",
    "INDETERMINATE",
    "ANALYSIS_INCOMPLETE",
    "AWAITING_HUMAN",
    "REJECTED",
    "APPROVED",
    "ACTION_APPLIED",
    "VERIFYING_RECOVERY",
    "CLOSED",
)


def test_new_workflow_starts_in_replay_ready_without_an_audit_event() -> None:
    machine = WorkflowStateMachine(workflow_id="workflow-ep-61d8aa")

    assert machine.state is WorkflowState.REPLAY_READY
    assert machine.revision_count == 0
    assert machine.audit_events == ()


def test_workflow_exposes_exact_frozen_state_set() -> None:
    assert tuple(state.value for state in WorkflowState) == FROZEN_STATES


def test_presentable_cannot_wait_for_human_without_a_deterministic_policy_decision() -> None:
    machine = WorkflowStateMachine(workflow_id="workflow-ep-61d8aa")
    for target in (
        WorkflowState.DETECTED,
        WorkflowState.INVESTIGATING,
        WorkflowState.VERIFYING,
        WorkflowState.PRESENTABLE,
    ):
        machine.transition(target, actor=WorkflowActor.WORKFLOW, reason="progress")

    with pytest.raises(WorkflowTransitionError, match="deterministic policy"):
        machine.transition(
            WorkflowState.AWAITING_HUMAN,
            actor=WorkflowActor.POLICY,
            reason="show action card",
        )

    assert machine.state is WorkflowState.PRESENTABLE
    assert len(machine.audit_events) == 4


def test_only_an_allowed_deterministic_policy_decision_can_present_an_action_card() -> None:
    machine = WorkflowStateMachine(workflow_id="workflow-ep-61d8aa")
    for target in (
        WorkflowState.DETECTED,
        WorkflowState.INVESTIGATING,
        WorkflowState.VERIFYING,
        WorkflowState.PRESENTABLE,
    ):
        machine.transition(target, actor=WorkflowActor.WORKFLOW, reason="progress")

    event = machine.transition(
        WorkflowState.AWAITING_HUMAN,
        actor=WorkflowActor.POLICY,
        reason="render the verified card",
        policy_decision=PolicyTransitionDecision(
            allowed=True,
            reason="verified evidence and simulator-only policy match",
        ),
    )

    assert event.to_state is WorkflowState.AWAITING_HUMAN
    assert event.actor is WorkflowActor.POLICY
    assert machine.state is WorkflowState.AWAITING_HUMAN
    assert len(machine.audit_events) == 5


def test_a_denied_deterministic_policy_decision_terminates_without_an_action_card() -> None:
    machine = WorkflowStateMachine(workflow_id="workflow-ep-61d8aa")
    for target in (
        WorkflowState.DETECTED,
        WorkflowState.INVESTIGATING,
        WorkflowState.VERIFYING,
        WorkflowState.PRESENTABLE,
    ):
        machine.transition(target, actor=WorkflowActor.WORKFLOW, reason="progress")

    event = machine.transition(
        WorkflowState.AWAITING_HUMAN,
        actor=WorkflowActor.POLICY,
        reason="attempt presentation",
        policy_decision=PolicyTransitionDecision(
            allowed=False,
            reason="real infrastructure target is prohibited",
        ),
    )

    assert event.to_state is WorkflowState.INDETERMINATE
    assert machine.state is WorkflowState.INDETERMINATE
    assert all(
        event.to_state not in {WorkflowState.AWAITING_HUMAN, WorkflowState.ACTION_APPLIED}
        for event in machine.audit_events
    )


def test_policy_decision_must_be_owned_by_the_deterministic_policy_actor() -> None:
    machine = WorkflowStateMachine(workflow_id="workflow-ep-61d8aa")
    for target in (
        WorkflowState.DETECTED,
        WorkflowState.INVESTIGATING,
        WorkflowState.VERIFYING,
        WorkflowState.PRESENTABLE,
    ):
        machine.transition(target, actor=WorkflowActor.WORKFLOW, reason="progress")

    with pytest.raises(WorkflowTransitionError, match="deterministic policy"):
        machine.transition(
            WorkflowState.AWAITING_HUMAN,
            actor=WorkflowActor.WORKFLOW,
            reason="spoofed authorization",
            policy_decision=PolicyTransitionDecision(allowed=True, reason="spoofed"),
        )


@pytest.mark.parametrize(
    ("failure", "expected_state"),
    [
        (WorkflowFailure.BLOCKED, WorkflowState.INDETERMINATE),
        (WorkflowFailure.TIMEOUT, WorkflowState.ANALYSIS_INCOMPLETE),
        (WorkflowFailure.INVALID_SCHEMA, WorkflowState.ANALYSIS_INCOMPLETE),
        (WorkflowFailure.STALE_REQUIRED_DATA, WorkflowState.INDETERMINATE),
        (WorkflowFailure.TOOL_TRANSPORT, WorkflowState.ANALYSIS_INCOMPLETE),
    ],
)
def test_failure_paths_emit_no_action_card(
    failure: WorkflowFailure,
    expected_state: WorkflowState,
) -> None:
    machine = WorkflowStateMachine(workflow_id="workflow-ep-61d8aa")
    for target in (
        WorkflowState.DETECTED,
        WorkflowState.INVESTIGATING,
        WorkflowState.VERIFYING,
    ):
        machine.transition(target, actor=WorkflowActor.WORKFLOW, reason="progress")

    event = machine.fail(failure, reason=failure.value)

    assert event.to_state is expected_state
    assert machine.state is expected_state
    assert all(
        audit.to_state not in {WorkflowState.AWAITING_HUMAN, WorkflowState.ACTION_APPLIED}
        for audit in machine.audit_events
    )


def test_policy_decision_cannot_authorize_an_ordinary_state_transition() -> None:
    machine = WorkflowStateMachine(workflow_id="workflow-ep-61d8aa")

    with pytest.raises(WorkflowTransitionError, match="only authorizes presentation"):
        machine.transition(
            WorkflowState.DETECTED,
            actor=WorkflowActor.POLICY,
            reason="spoofed transition",
            policy_decision=PolicyTransitionDecision(allowed=True, reason="spoofed"),
        )

    assert machine.state is WorkflowState.REPLAY_READY
    assert machine.audit_events == ()


def _presentable_machine() -> WorkflowStateMachine:
    machine = WorkflowStateMachine(workflow_id="workflow-ep-61d8aa")
    for target in (
        WorkflowState.DETECTED,
        WorkflowState.INVESTIGATING,
        WorkflowState.VERIFYING,
        WorkflowState.PRESENTABLE,
    ):
        machine.transition(target, actor=WorkflowActor.WORKFLOW, reason="progress")
    return machine


def test_presentation_rejects_a_non_deterministic_policy_authority() -> None:
    machine = _presentable_machine()

    with pytest.raises(WorkflowTransitionError, match="only deterministic policy"):
        machine.transition(
            WorkflowState.AWAITING_HUMAN,
            actor=WorkflowActor.POLICY,
            reason="spoofed authorization",
            policy_decision=PolicyTransitionDecision(
                allowed=True,
                reason="agent-supplied authorization",
                source="agent",
            ),
        )

    assert machine.state is WorkflowState.PRESENTABLE


def test_invalid_transition_is_rejected_without_mutating_workflow_state() -> None:
    machine = WorkflowStateMachine(workflow_id="workflow-ep-61d8aa")

    with pytest.raises(WorkflowTransitionError, match="not allowed"):
        machine.transition(
            WorkflowState.CLOSED,
            actor=WorkflowActor.WORKFLOW,
            reason="skip required checks",
        )

    assert machine.state is WorkflowState.REPLAY_READY
    assert machine.audit_events == ()


def test_revision_request_is_rejected_outside_the_verifying_state() -> None:
    machine = WorkflowStateMachine(workflow_id="workflow-ep-61d8aa")

    with pytest.raises(WorkflowTransitionError, match="only valid while verifying"):
        machine.request_revision(reason="untrusted correction")

    assert machine.revision_count == 0
    assert machine.state is WorkflowState.REPLAY_READY
