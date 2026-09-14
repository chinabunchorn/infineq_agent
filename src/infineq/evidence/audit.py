"""Redacted, immutable audit records for the evidence tool boundary."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from functools import wraps
from typing import Any, Concatenate, ParamSpec, TypeVar, cast

from pydantic import AwareDatetime, BaseModel, Field, StrictStr, field_validator, model_validator

from infineq.schemas.common import SchemaVersion, StrictModel
from infineq.security.redaction import REDACTED, redact_text


class AgentRole(StrEnum):
    """Agent roles allowed to invoke the v1 evidence tools."""

    INVESTIGATOR = "investigator"
    VERIFIER = "verifier"


class ToolAuditStatus(StrEnum):
    """Outcome recorded for one tool-boundary invocation."""

    SUCCESS = "success"
    ERROR = "error"


_AUDIT_ID_PATTERN = r"^(?:ev:[a-z0-9-]+(?::[a-z0-9_-]+){4}:[a-f0-9]{8}|runbook-[a-z0-9-]+)$"
_AUDIT_ID_RE = re.compile(_AUDIT_ID_PATTERN)
_HASH_PATTERN = r"^[a-f0-9]{64}$"
_UNSAFE_ERROR_MARKERS = (
    "artifact_path",
    "acceptable_conclusions",
    "fault_label",
    "filesystem_path",
    "forbidden_conclusions",
    "hidden_oracle",
    "mechanism",
    "oracle",
    "prompt",
    "recovery_truth",
    "root_cause",
    "scenario_family",
    "tool_body",
)
_PATH_OR_URL = re.compile(
    r"(?i)(?:https?://|file://|(?:^|[\s=(])(?:[a-z]:[\\/]|\.\.?[\\/]|~[\\/]|/))"
)


P = ParamSpec("P")
R = TypeVar("R")


def redact_error(error: object | None) -> str | None:
    """Return a bounded error message with sensitive context removed."""

    if error is None:
        return None
    try:
        raw = str(error)
    except Exception:
        return REDACTED
    redacted = redact_text(raw)
    lowered = raw.casefold()
    unsafe = (
        redacted != raw
        or len(raw) > 500
        or _PATH_OR_URL.search(raw) is not None
        or any(marker in raw for marker in ("{", "}", "[", "]"))
        or any(marker in lowered for marker in _UNSAFE_ERROR_MARKERS)
    )
    return REDACTED if unsafe else redacted


class ToolAuditRecord(StrictModel):
    """Path-free, response-body-free record of one validated tool invocation."""

    schema_version: SchemaVersion = "1.0"
    tool_name: StrictStr = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    validated_arguments_hash: StrictStr = Field(pattern=_HASH_PATTERN)
    agent_role: AgentRole
    started_at: AwareDatetime
    ended_at: AwareDatetime
    status: ToolAuditStatus
    returned_evidence_ids: tuple[StrictStr, ...] = Field(default=(), max_length=20)
    redacted_error: StrictStr | None = Field(default=None, max_length=500)

    @field_validator("started_at", "ended_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @field_validator("returned_evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("returned evidence IDs must be unique")
        if any(_AUDIT_ID_RE.fullmatch(value) is None for value in values):
            raise ValueError("returned evidence IDs are not allow-listed")
        return values

    @field_validator("redacted_error", mode="before")
    @classmethod
    def sanitize_error(cls, value: object | None) -> str | None:
        return redact_error(value)

    @model_validator(mode="after")
    def end_must_follow_start(self) -> ToolAuditRecord:
        if self.ended_at < self.started_at:
            raise ValueError("audit end must not precede audit start")
        return self


class AuditRecorder:
    """Keep bounded immutable audit records in memory without persisting bodies."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._records: list[ToolAuditRecord] = []
        self._clock = clock

    @property
    def records(self) -> tuple[ToolAuditRecord, ...]:
        """Return a read-only snapshot of recorded events."""

        return tuple(self._records)

    def now(self) -> datetime:
        """Return an aware UTC timestamp, falling back safely if a clock fails."""

        try:
            value = self._clock() if self._clock is not None else datetime.now(UTC)
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            return value.astimezone(UTC)
        except Exception:
            return datetime.now(UTC)

    def record(
        self,
        *,
        tool_name: str,
        validated_arguments: BaseModel | Mapping[str, object],
        agent_role: AgentRole,
        started_at: datetime,
        ended_at: datetime,
        status: ToolAuditStatus,
        returned_evidence_ids: tuple[str, ...] = (),
        redacted_error: object | None = None,
    ) -> ToolAuditRecord:
        """Validate and append one audit record without storing invocation bodies."""

        record = ToolAuditRecord(
            tool_name=tool_name,
            validated_arguments_hash=validated_arguments_hash(validated_arguments),
            agent_role=agent_role,
            started_at=started_at,
            ended_at=ended_at,
            status=status,
            returned_evidence_ids=returned_evidence_ids,
            redacted_error=redact_error(redacted_error),
        )
        self._records.append(record)
        return record


class AuditBoundary:
    """Invoke a tool and record only the outermost boundary call."""

    def __init__(self, *, recorder: AuditRecorder, agent_role: AgentRole) -> None:
        self._recorder = recorder
        self._agent_role = agent_role
        self._depth = 0

    @property
    def records(self) -> tuple[ToolAuditRecord, ...]:
        """Return the recorder's immutable record snapshot."""

        return self._recorder.records

    def invoke(
        self,
        *,
        tool_name: str,
        validated_arguments: BaseModel | Mapping[str, object],
        operation: Callable[[], R],
    ) -> R:
        """Run one operation and never let audit failures alter its outcome."""

        outermost = self._depth == 0
        self._depth += 1
        if not outermost:
            try:
                return operation()
            finally:
                self._depth -= 1

        started_at = self._recorder.now()
        try:
            result = operation()
        except BaseException as error:
            ended_at = self._end_time(started_at)
            self._safe_record(
                tool_name=tool_name,
                validated_arguments=validated_arguments,
                started_at=started_at,
                ended_at=ended_at,
                status=ToolAuditStatus.ERROR,
                redacted_error=error,
            )
            raise
        else:
            ended_at = self._end_time(started_at)
            self._safe_record(
                tool_name=tool_name,
                validated_arguments=validated_arguments,
                started_at=started_at,
                ended_at=ended_at,
                status=ToolAuditStatus.SUCCESS,
                returned_evidence_ids=_returned_evidence_ids(result),
            )
            return result
        finally:
            self._depth -= 1

    def _end_time(self, started_at: datetime) -> datetime:
        return max(self._recorder.now(), started_at)

    def _safe_record(
        self,
        *,
        tool_name: str,
        validated_arguments: BaseModel | Mapping[str, object],
        started_at: datetime,
        ended_at: datetime,
        status: ToolAuditStatus,
        returned_evidence_ids: tuple[str, ...] = (),
        redacted_error: object | None = None,
    ) -> None:
        try:
            self._recorder.record(
                tool_name=tool_name,
                validated_arguments=validated_arguments,
                agent_role=self._agent_role,
                started_at=started_at,
                ended_at=ended_at,
                status=status,
                returned_evidence_ids=returned_evidence_ids,
                redacted_error=redacted_error,
            )
        except Exception:
            return


def _returned_evidence_ids(result: object) -> tuple[str, ...]:
    """Extract only typed evidence identifiers, never the returned body."""

    found: list[str] = []
    seen: set[str] = set()

    def add(value: object) -> None:
        if not isinstance(value, str) or _AUDIT_ID_RE.fullmatch(value) is None:
            return
        if value not in seen:
            seen.add(value)
            found.append(value)

    def visit(value: object) -> None:
        if isinstance(value, BaseModel):
            visit(value.model_dump(mode="python"))
        elif isinstance(value, Mapping):
            for key, item in value.items():
                if key == "evidence_id":
                    add(item)
                elif key == "evidence_ids" and isinstance(item, (list, tuple)):
                    for evidence_id in item:
                        add(evidence_id)
                visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)

    try:
        visit(result)
    except Exception:
        return ()
    return tuple(found[:20])


def audited_tool(
    tool_name: str,
) -> Callable[[Callable[Concatenate[Any, P], R]], Callable[Concatenate[Any, P], R]]:
    """Decorate a request-model tool without changing its public call schema."""

    def decorate(
        method: Callable[Concatenate[Any, P], R],
    ) -> Callable[Concatenate[Any, P], R]:
        @wraps(method)
        def wrapped(self: Any, *args: P.args, **kwargs: P.kwargs) -> R:
            request: object | None = args[0] if args else kwargs.get("request")
            boundary = getattr(self, "_audit_boundary", None)
            if not isinstance(boundary, AuditBoundary):
                return method(self, *args, **kwargs)
            return boundary.invoke(
                tool_name=tool_name,
                validated_arguments=cast(BaseModel | Mapping[str, object], request),
                operation=lambda: method(self, *args, **kwargs),
            )

        return cast(Callable[Concatenate[Any, P], R], wrapped)

    return decorate


def _canonical_json_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return _canonical_json_value(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): _canonical_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_json_value(item) for item in value]
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return value


def validated_arguments_hash(arguments: BaseModel | Mapping[str, object]) -> str:
    """Return a deterministic SHA-256 for validated arguments and their type."""

    if isinstance(arguments, BaseModel):
        argument_type = f"{arguments.__class__.__module__}.{arguments.__class__.__qualname__}"
    elif isinstance(arguments, Mapping):
        argument_type = "mapping"
    else:
        raise TypeError("audit arguments must be a validated model or mapping")
    payload = json.dumps(
        {"type": argument_type, "arguments": _canonical_json_value(arguments)},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


AuditRecord = ToolAuditRecord
AuditStatus = ToolAuditStatus


__all__ = [
    "AgentRole",
    "AuditBoundary",
    "AuditRecord",
    "AuditRecorder",
    "AuditStatus",
    "ToolAuditRecord",
    "ToolAuditStatus",
    "audited_tool",
    "redact_error",
    "validated_arguments_hash",
]
