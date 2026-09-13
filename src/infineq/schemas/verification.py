"""Independent evidence and safety verification contracts."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import AwareDatetime, Field, field_validator, model_validator

from infineq.schemas.common import EvidenceId, OpaqueId, SchemaVersion, StrictModel


class VerificationStatus(StrEnum):
    """Verifier decision over an investigation result."""

    VERIFIED = "verified"
    REVISION_REQUIRED = "revision_required"
    BLOCKED = "blocked"


class ClaimCheck(StrictModel):
    """Whether cited observed evidence supports one auditable claim."""

    schema_version: SchemaVersion = "1.0"
    claim_id: OpaqueId
    evidence_ids: tuple[EvidenceId, ...]
    supported: bool
    note: Annotated[str, Field(min_length=1, max_length=500)]


class VerificationResultV1(StrictModel):
    """Verifier result with explicit failed checks and correction requests."""

    schema_version: SchemaVersion = "1.0"
    verification_id: OpaqueId
    incident_id: OpaqueId
    investigation_id: OpaqueId
    checked_at: AwareDatetime
    status: VerificationStatus
    claim_checks: tuple[ClaimCheck, ...]
    issues: tuple[str, ...]
    correction_requests: tuple[str, ...]
    policy_ref: OpaqueId

    @field_validator("checked_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def success_requires_all_checks(self) -> "VerificationResultV1":
        if self.status is VerificationStatus.VERIFIED:
            if any(not check.supported for check in self.claim_checks):
                raise ValueError("verified result cannot contain an unsupported claim")
            if self.issues or self.correction_requests:
                raise ValueError("verified result cannot contain open issues")
        elif self.status is VerificationStatus.REVISION_REQUIRED and not self.correction_requests:
            raise ValueError("revision_required needs at least one correction request")
        elif self.status is VerificationStatus.BLOCKED and not self.issues:
            raise ValueError("blocked verification needs at least one issue")
        return self
