"""Deterministic presentation-policy checks for the Phase 5 workflow."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

from infineq.evidence.tool_schemas import PolicyResponse
from infineq.schemas.action import ActionPlanV1, ActionTarget, ActionType
from infineq.schemas.evidence import DataQualityStatus
from infineq.schemas.incident import IncidentPacketV1
from infineq.schemas.investigation import Disposition, InvestigationResultV1
from infineq.schemas.verification import VerificationResultV1, VerificationStatus
from infineq.workflow.transitions import PolicyTransitionDecision

DETERMINISTIC_POLICY_SOURCE: Final = "deterministic_policy"


def _deny(reason: str) -> PolicyTransitionDecision:
    return PolicyTransitionDecision(
        allowed=False,
        reason=reason,
        source=DETERMINISTIC_POLICY_SOURCE,
    )


def _allow() -> PolicyTransitionDecision:
    return PolicyTransitionDecision(
        allowed=True,
        reason="action and simulator-only policy match",
        source=DETERMINISTIC_POLICY_SOURCE,
    )


def evaluate_action_policy(
    *,
    investigation: InvestigationResultV1,
    policy: PolicyResponse,
    required_data_fresh: bool = True,
    packet: IncidentPacketV1 | None = None,
) -> PolicyTransitionDecision:
    """Validate one proposed action without requiring a Verifier decision."""

    if investigation.disposition is not Disposition.DIAGNOSED:
        return _deny("investigation is not diagnosed")
    if not required_data_fresh:
        return _deny("required evidence is not fresh")
    if packet is not None:
        if packet.incident_id != investigation.incident_id:
            return _deny("packet and investigation incidents do not match")
        if packet.data_quality.status is not DataQualityStatus.GOOD:
            return _deny("required evidence quality is not good")
        if packet.data_quality.freshness_seconds > 10:
            return _deny("required evidence is not fresh")

    action = investigation.proposed_action
    if not isinstance(action, ActionPlanV1):
        return _deny("action plan is missing")
    if not isinstance(getattr(action, "target", None), ActionTarget):
        return _deny("action target is invalid")
    if packet is not None:
        if action.incident_id != packet.incident_id:
            return _deny("action and packet incidents do not match")
        if action.target.service_id != packet.service_id:
            return _deny("action target does not match the incident service")
        if (
            packet.deployment.replicas != policy.from_replicas
            or packet.deployment.ready_replicas != policy.from_replicas
        ):
            return _deny("deployment replicas are not ready for the proposed action")
    if action.target.kind != policy.allowed_target_kind:
        return _deny("action target is not simulator-only")
    if action.action_type is not ActionType.SIMULATED_SCALE_OUT:
        return _deny("action type is not allow-listed")
    if action.action_type not in policy.allowed_action_types:
        return _deny("action type does not match policy")
    if action.from_replicas != policy.from_replicas or action.to_replicas != policy.to_replicas:
        return _deny("replica limits do not match policy")
    if (
        "requires_human_approval" not in action.model_fields_set
        or action.requires_human_approval is not True
        or policy.requires_human_approval is not True
    ):
        return _deny("human approval requirement is missing")
    if action.policy_ref != policy.policy_ref.value:
        return _deny("action policy reference does not match")
    if policy.dry_run_only is not True or policy.execution_allowed is not False:
        return _deny("policy does not permit presentation of a dry-run action")
    created_at = getattr(action, "created_at", None)
    expires_at = getattr(action, "expires_at", None)
    if not isinstance(created_at, datetime) or not isinstance(expires_at, datetime):
        return _deny("action TTL is invalid")
    try:
        ttl = expires_at - created_at
    except TypeError:
        return _deny("action TTL is invalid")
    if ttl != timedelta(seconds=policy.action_ttl_seconds):
        return _deny("action TTL does not match policy")
    return _allow()


def evaluate_presentation_policy(
    *,
    investigation: InvestigationResultV1,
    verification: VerificationResultV1,
    policy: PolicyResponse,
    required_data_fresh: bool = True,
    packet: IncidentPacketV1 | None = None,
) -> PolicyTransitionDecision:
    """Decide whether the finite workflow may present an action card."""

    if verification.status is not VerificationStatus.VERIFIED:
        return _deny("verification is not verified")
    if verification.incident_id != investigation.incident_id:
        return _deny("verification and investigation incidents do not match")
    if verification.investigation_id != investigation.investigation_id:
        return _deny("verification does not match the investigation")
    action_decision = evaluate_action_policy(
        investigation=investigation,
        policy=policy,
        required_data_fresh=required_data_fresh,
        packet=packet,
    )
    if not action_decision.allowed:
        return action_decision

    leading = next(
        (
            hypothesis
            for hypothesis in investigation.hypotheses
            if hypothesis.hypothesis_id == investigation.leading_hypothesis_id
        ),
        None,
    )
    if leading is None:
        return _deny("leading hypothesis is missing")
    verifier_checked_ids = {
        evidence_id for check in verification.claim_checks for evidence_id in check.evidence_ids
    }
    if not set(leading.supporting_evidence_ids).issubset(verifier_checked_ids):
        return _deny("Verifier did not check the leading hypothesis evidence")
    return _allow()


# Short aliases keep the boundary easy to call from deterministic workflow code.
evaluate_policy = evaluate_presentation_policy
deterministic_policy_decision = evaluate_presentation_policy


class DeterministicPolicy:
    """Small stateless façade for callers that prefer an object boundary."""

    def decide(
        self,
        *,
        investigation: InvestigationResultV1,
        verification: VerificationResultV1,
        policy: PolicyResponse,
        required_data_fresh: bool = True,
        packet: IncidentPacketV1 | None = None,
    ) -> PolicyTransitionDecision:
        return evaluate_presentation_policy(
            investigation=investigation,
            verification=verification,
            policy=policy,
            required_data_fresh=required_data_fresh,
            packet=packet,
        )


__all__ = [
    "DETERMINISTIC_POLICY_SOURCE",
    "DeterministicPolicy",
    "deterministic_policy_decision",
    "evaluate_action_policy",
    "evaluate_policy",
    "evaluate_presentation_policy",
]
