"""Shared strict types for versioned Infineq contracts."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

SchemaVersion = Literal["1.0"]
OpaqueId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)+$", max_length=128)]
EvidenceId = Annotated[
    str,
    Field(
        pattern=(
            r"^ev:[a-z0-9-]+:[a-z0-9_-]+:[a-z0-9_-]+:"
            r"[a-z0-9_-]+:[a-z0-9_-]+:[a-f0-9]{8}$"
        ),
        max_length=256,
    ),
]


class StrictModel(BaseModel):
    """Base model for untrusted or cross-component data."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class DataOrigin(StrEnum):
    """Provenance categories visible to operators and evaluators."""

    SYNTHETIC_REPLAY = "synthetic_replay"


class TimeWindow(StrictModel):
    """Half-open UTC interval: start inclusive, end exclusive."""

    schema_version: SchemaVersion = "1.0"
    start: AwareDatetime
    end: AwareDatetime

    @field_validator("start", "end")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def end_must_follow_start(self) -> "TimeWindow":
        if self.end <= self.start:
            raise ValueError("window end must follow start")
        return self
