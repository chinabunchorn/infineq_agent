"""Post-action recovery measurement contracts."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import AwareDatetime, Field, field_validator, model_validator

from infineq.schemas.common import EvidenceId, OpaqueId, SchemaVersion, StrictModel, TimeWindow


class ComparisonOperator(StrEnum):
    """Deterministic comparison used by recovery criteria."""

    LESS_THAN = "lt"
    LESS_THAN_OR_EQUAL = "lte"
    GREATER_THAN = "gt"
    GREATER_THAN_OR_EQUAL = "gte"
    EQUAL = "eq"


class RecoveryState(StrEnum):
    """Whether recovery was measured, failed, or not attempted."""

    VERIFIED = "verified"
    NOT_VERIFIED = "not_verified"
    NOT_ATTEMPTED = "not_attempted"


class RecoveryCriterion(StrictModel):
    """One measured recovery condition."""

    schema_version: SchemaVersion = "1.0"
    metric: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")]
    operator: ComparisonOperator
    threshold: float
    observed_value: float | None
    unit: Annotated[str, Field(min_length=1, max_length=32)]
    passed: bool
    evidence_ids: tuple[EvidenceId, ...]


class RecoveryResultV1(StrictModel):
    """Measured outcome after a simulator action or explicit non-attempt."""

    schema_version: SchemaVersion = "1.0"
    incident_id: OpaqueId
    action_plan_id: OpaqueId | None
    checked_at: AwareDatetime
    window: TimeWindow | None
    state: RecoveryState
    criteria: Annotated[tuple[RecoveryCriterion, ...], Field(max_length=10)]
    evidence_ids: tuple[EvidenceId, ...]
    summary: Annotated[str, Field(min_length=1, max_length=1_000)]

    @field_validator("checked_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def verified_requires_passing_measurements(self) -> "RecoveryResultV1":
        if self.state is RecoveryState.VERIFIED:
            if not self.criteria or any(not item.passed for item in self.criteria):
                raise ValueError("verified recovery requires all measured criteria to pass")
            if self.action_plan_id is None or self.window is None:
                raise ValueError("verified recovery requires an action and measurement window")
            if any(item.observed_value is None or not item.evidence_ids for item in self.criteria):
                raise ValueError("verified recovery requires measured criteria with evidence")
            if not set(self.evidence_ids).issuperset(
                evidence_id for item in self.criteria for evidence_id in item.evidence_ids
            ):
                raise ValueError("recovery evidence must index every criterion citation")
            if not self.evidence_ids:
                raise ValueError("verified recovery requires evidence")
        elif self.state is RecoveryState.NOT_ATTEMPTED and (
            self.action_plan_id is not None or self.criteria or self.window is not None
        ):
            raise ValueError("not_attempted recovery cannot contain action measurements")
        return self
