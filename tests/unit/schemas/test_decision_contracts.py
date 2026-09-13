from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from infineq.schemas.action import (
    ActionPlanV1,
    ActionTarget,
    ActionType,
    Decision,
    HumanDecisionV1,
)
from infineq.schemas.common import TimeWindow
from infineq.schemas.investigation import (
    Disposition,
    EvidenceCoverage,
    Hypothesis,
    IncidentFamily,
    InvestigationResultV1,
)
from infineq.schemas.recovery import (
    ComparisonOperator,
    RecoveryCriterion,
    RecoveryResultV1,
    RecoveryState,
)
from infineq.schemas.verification import ClaimCheck, VerificationResultV1, VerificationStatus

NOW = datetime(2026, 9, 13, 4, 0, tzinfo=UTC)
INCIDENT_ID = "inc-7f31a9"
EVIDENCE_ID = "ev:inc-7f31a9:metrics:w15:queue:max:aa12bb34"


def make_action() -> ActionPlanV1:
    return ActionPlanV1(
        plan_id="plan-aa12bb",
        incident_id=INCIDENT_ID,
        action_type=ActionType.SIMULATED_SCALE_OUT,
        target=ActionTarget(kind="simulator", service_id="svc-infineq-demo"),
        from_replicas=1,
        to_replicas=2,
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        expected_effect="Increase simulated serving capacity.",
        risks=("The injected cause might be wrong.",),
        verification_criteria=("Re-measure TTFT and queue depth.",),
        policy_ref="policy-infineq-v1",
    )


def test_action_and_human_decision_encode_the_frozen_approval_boundary() -> None:
    action = make_action()
    decision = HumanDecisionV1(
        decision_id="decision-bb12cc",
        plan_id=action.plan_id,
        incident_id=action.incident_id,
        plan_hash="a" * 64,
        decision=Decision.APPROVE,
        actor_label="demo-operator",
        decided_at=NOW + timedelta(seconds=10),
    )

    assert action.requires_human_approval is True
    assert decision.decision is Decision.APPROVE


@pytest.mark.parametrize(
    "changes",
    [
        {"requires_human_approval": False},
        {"from_replicas": 2, "to_replicas": 3},
        {"action_type": "restart_real_cluster"},
        {"target": {"kind": "kubernetes", "service_id": "svc-infineq-demo"}},
    ],
)
def test_action_rejects_every_non_frozen_write_boundary(changes) -> None:
    payload = make_action().model_dump()
    payload.update(changes)

    with pytest.raises(ValidationError):
        ActionPlanV1.model_validate(payload)


def test_investigation_requires_grounded_diagnosis_and_blocks_action_on_abstention() -> None:
    hypothesis = Hypothesis(
        hypothesis_id="hyp-queue-aa12",
        family=IncidentFamily.CAPACITY_QUEUEING,
        rank=1,
        evidence_coverage=EvidenceCoverage.COMPLETE,
        statement="Queue pressure is the leading likely cause.",
        supporting_evidence_ids=(EVIDENCE_ID,),
        contradicting_evidence_ids=(),
        missing_evidence=(),
        next_check=None,
    )
    result = InvestigationResultV1(
        investigation_id="investigation-aa12",
        incident_id=INCIDENT_ID,
        completed_at=NOW,
        disposition=Disposition.DIAGNOSED,
        summary="Evidence supports queue pressure over the compared alternatives.",
        hypotheses=(hypothesis,),
        leading_hypothesis_id=hypothesis.hypothesis_id,
        proposed_action=make_action(),
        cited_evidence_ids=(EVIDENCE_ID,),
        limitations=("Synthetic replay only.",),
    )

    assert result.leading_hypothesis_id == hypothesis.hypothesis_id

    with pytest.raises(ValidationError):
        result.model_copy(
            update={"disposition": Disposition.INDETERMINATE},
        ).model_dump()
        InvestigationResultV1.model_validate(
            {
                **result.model_dump(),
                "disposition": Disposition.INDETERMINATE,
            }
        )


def test_verification_and_recovery_cannot_report_success_when_a_check_failed() -> None:
    with pytest.raises(ValidationError):
        VerificationResultV1(
            verification_id="verification-aa12",
            incident_id=INCIDENT_ID,
            investigation_id="investigation-aa12",
            checked_at=NOW,
            status=VerificationStatus.VERIFIED,
            claim_checks=(
                ClaimCheck(
                    claim_id="claim-aa12",
                    evidence_ids=(EVIDENCE_ID,),
                    supported=False,
                    note="Evidence does not support the claim.",
                ),
            ),
            issues=(),
            correction_requests=(),
            policy_ref="policy-infineq-v1",
        )

    with pytest.raises(ValidationError):
        RecoveryResultV1(
            incident_id=INCIDENT_ID,
            action_plan_id="plan-aa12bb",
            checked_at=NOW,
            window=TimeWindow(start=NOW - timedelta(seconds=30), end=NOW),
            state=RecoveryState.VERIFIED,
            criteria=(
                RecoveryCriterion(
                    metric="ttft",
                    operator=ComparisonOperator.LESS_THAN_OR_EQUAL,
                    threshold=2_000.0,
                    observed_value=2_500.0,
                    unit="ms",
                    passed=False,
                    evidence_ids=(EVIDENCE_ID,),
                ),
            ),
            evidence_ids=(EVIDENCE_ID,),
            summary="Recovery was not observed.",
        )
