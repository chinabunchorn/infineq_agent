"""Approval-bound remediation contracts."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from infineq.schemas.common import OpaqueId, SchemaVersion, StrictModel


class ActionType(StrEnum):
    """The only v1 state-changing action."""

    SIMULATED_SCALE_OUT = "simulated_scale_out"


class Decision(StrEnum):
    """Human choices at the approval gate."""

    APPROVE = "approve"
    REJECT = "reject"
    REQUEST_MORE_EVIDENCE = "request_more_evidence"


class ActionTarget(StrictModel):
    """A simulator target; real infrastructure targets are intentionally impossible."""

    schema_version: SchemaVersion = "1.0"
    kind: Literal["simulator"] = "simulator"
    service_id: OpaqueId


class ActionPlanV1(StrictModel):
    """A bounded simulator action proposal, never an execution request."""

    schema_version: SchemaVersion = "1.0"
    plan_id: OpaqueId
    incident_id: OpaqueId
    action_type: ActionType
    target: ActionTarget
    from_replicas: Annotated[int, Field(ge=0)]
    to_replicas: Annotated[int, Field(ge=0)]
    requires_human_approval: Literal[True] = True
    created_at: AwareDatetime
    expires_at: AwareDatetime
    expected_effect: Annotated[str, Field(min_length=1, max_length=500)]
    risks: Annotated[tuple[str, ...], Field(min_length=1, max_length=10)]
    verification_criteria: Annotated[tuple[str, ...], Field(min_length=1, max_length=10)]
    policy_ref: OpaqueId

    @field_validator("created_at", "expires_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def enforce_v1_action_boundary(self) -> "ActionPlanV1":
        if self.from_replicas != 1 or self.to_replicas != 2:
            raise ValueError("v1 permits simulated scale-out only from one replica to two")
        if self.expires_at <= self.created_at:
            raise ValueError("action plan expiry must follow creation")
        return self


class HumanDecisionV1(StrictModel):
    """Auditable human decision bound to an exact action-plan hash."""

    schema_version: SchemaVersion = "1.0"
    decision_id: OpaqueId
    plan_id: OpaqueId
    incident_id: OpaqueId
    plan_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    decision: Decision
    actor_label: Annotated[str, Field(min_length=1, max_length=128)]
    decided_at: AwareDatetime

    @field_validator("decided_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)
