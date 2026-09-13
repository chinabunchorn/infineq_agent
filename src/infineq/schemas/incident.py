"""Deterministic incident packet contracts."""

from datetime import UTC, datetime
from typing import Annotated

from pydantic import AwareDatetime, Field, field_validator, model_validator

from infineq.schemas.common import (
    DataOrigin,
    EvidenceId,
    OpaqueId,
    SchemaVersion,
    StrictModel,
    TimeWindow,
)
from infineq.schemas.evidence import DataQuality, EvidenceRef, SignalObservation


class SLOStatus(StrictModel):
    """Measured status of one service-level objective."""

    schema_version: SchemaVersion = "1.0"
    metric: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")]
    threshold: float
    unit: Annotated[str, Field(min_length=1, max_length=32)]
    violated: bool
    evidence_ids: tuple[EvidenceId, ...]


class DeploymentSnapshot(StrictModel):
    """Read-only serving deployment state at one replay instant."""

    schema_version: SchemaVersion = "1.0"
    replicas: Annotated[int, Field(ge=0)]
    ready_replicas: Annotated[int, Field(ge=0)]
    revision: Annotated[str, Field(min_length=1, max_length=128)]
    observed_at: AwareDatetime
    evidence_ids: tuple[EvidenceId, ...]

    @field_validator("observed_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def ready_cannot_exceed_total(self) -> "DeploymentSnapshot":
        if self.ready_replicas > self.replicas:
            raise ValueError("ready replicas cannot exceed total replicas")
        return self


class IncidentPacketV1(StrictModel):
    """Agent input produced by deterministic detection code."""

    schema_version: SchemaVersion = "1.0"
    incident_id: OpaqueId
    service_id: OpaqueId
    detected_at: AwareDatetime
    origin: DataOrigin
    baseline_window: TimeWindow
    observation_window: TimeWindow
    slo: SLOStatus
    signals: Annotated[tuple[SignalObservation, ...], Field(min_length=1)]
    deployment: DeploymentSnapshot
    data_quality: DataQuality
    detector_version: Annotated[str, Field(min_length=1, max_length=64)]
    evidence_refs: Annotated[tuple[EvidenceRef, ...], Field(min_length=1)]

    @field_validator("detected_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def evidence_must_be_local_and_indexed(self) -> "IncidentPacketV1":
        indexed = {item.evidence_id: item for item in self.evidence_refs}
        if len(indexed) != len(self.evidence_refs):
            raise ValueError("evidence IDs must be unique")
        for item in self.evidence_refs:
            if item.incident_id != self.incident_id or not item.evidence_id.startswith(
                f"ev:{self.incident_id}:"
            ):
                raise ValueError("evidence must belong to the incident packet")
            if item.origin is not self.origin:
                raise ValueError("evidence origin must match the incident packet")

        used_ids = set(self.slo.evidence_ids) | set(self.deployment.evidence_ids)
        for signal in self.signals:
            used_ids.add(signal.evidence.evidence_id)
            if indexed.get(signal.evidence.evidence_id) != signal.evidence:
                raise ValueError("signal evidence must match an indexed evidence reference")
        if not used_ids.issubset(indexed):
            raise ValueError("all cited evidence must exist in the packet index")
        return self
