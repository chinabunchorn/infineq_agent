"""Evidence and telemetry quality contracts."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import AwareDatetime, Field, field_validator

from infineq.schemas.common import (
    DataOrigin,
    EvidenceId,
    OpaqueId,
    SchemaVersion,
    StrictModel,
    TimeWindow,
)


class DataQualityStatus(StrEnum):
    """Whether evidence is usable for a diagnostic decision."""

    GOOD = "good"
    DEGRADED = "degraded"
    INSUFFICIENT = "insufficient"


class EvidenceRef(StrictModel):
    """Immutable reference to one normalized item of observed evidence."""

    schema_version: SchemaVersion = "1.0"
    evidence_id: EvidenceId
    incident_id: OpaqueId
    origin: DataOrigin
    source: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")]
    observed_at: AwareDatetime

    @field_validator("observed_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class SignalObservation(StrictModel):
    """A normalized aggregate with its evidence provenance."""

    schema_version: SchemaVersion = "1.0"
    signal: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")]
    value: float
    unit: Annotated[str, Field(min_length=1, max_length=32)]
    aggregation: Annotated[str, Field(min_length=1, max_length=32, pattern=r"^[a-z0-9_]+$")]
    window: TimeWindow
    evidence: EvidenceRef


class DataQuality(StrictModel):
    """Data sufficiency and freshness evaluated before agent reasoning."""

    schema_version: SchemaVersion = "1.0"
    status: DataQualityStatus
    sample_count: Annotated[int, Field(ge=0)]
    freshness_seconds: Annotated[float, Field(ge=0)]
    issues: tuple[str, ...] = ()
