"""Strict request and response contracts for the fixed evidence tool surface."""

import re
from datetime import date
from enum import StrEnum
from typing import Annotated, Any, Literal, TypeAlias
from urllib.parse import urlsplit

from pydantic import (
    AliasChoices,
    AwareDatetime,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from infineq.schemas.action import ActionPlanV1, ActionType
from infineq.schemas.common import EvidenceId, SchemaVersion, StrictModel
from infineq.schemas.incident import DeploymentSnapshot

IncidentId: TypeAlias = Annotated[  # noqa: UP040
    StrictStr,
    Field(min_length=4, max_length=64, pattern=r"^ep-[a-z0-9]+$"),
]


class SignalEnum(StrEnum):
    """Signals exposed by the normalized observed corpus."""

    TTFT = "ttft"
    QUEUE_DEPTH = "queue_depth"
    QUEUE_WAIT = "queue_wait"
    ITL = "itl"
    INPUT_TOKENS = "input_tokens"
    OUTPUT_TOKENS = "output_tokens"
    PREFILL = "prefill"
    ERROR_RATE = "error_rate"
    ARRIVAL_RATE = "arrival_rate"
    RUNNING_REQUESTS = "running_requests"
    WAITING_REQUESTS = "waiting_requests"
    END_TO_END = "end_to_end"
    GENERATION_THROUGHPUT = "generation_throughput"
    PROMPT_THROUGHPUT = "prompt_throughput"
    REQUEST = "request"
    REPLICAS = "replicas"
    CONFIGURATION_CHANGE = "configuration_change"
    REQUEST_OUTCOME = "request_outcome"
    READY_REPLICAS = "ready_replicas"
    RESTART_EVENT = "restart_event"


class WindowEnum(StrEnum):
    """The two fixed detector windows; both are half-open intervals."""

    BASELINE = "baseline"
    OBSERVATION = "observation"


class RequestStatus(StrEnum):
    """Request outcomes available in the normalized replay data."""

    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"


class RunbookQueryEnum(StrEnum):
    """The only curated symptom families permitted for runbook lookup."""

    CAPACITY_QUEUEING = "capacity_queueing"
    BACKEND_SLOWDOWN = "backend_slowdown"
    BACKEND_ERROR = "backend_error"
    WORKLOAD_SHAPE_CHANGE = "workload_shape_change"
    REPLICA_OR_DEPLOYMENT_REGRESSION = "replica_or_deployment_regression"


class RunbookSearchStatus(StrEnum):
    """Whether curated runbook evidence is available to the tool."""

    AVAILABLE = "available"
    PLACEHOLDER = "placeholder"


class PolicyRef(StrEnum):
    """The only policy document available to the verifier in v1."""

    INFINEQ_V1 = "policy-infineq-v1"


class GetIncidentPacketRequest(StrictModel):
    """Request one detector-produced packet for one opaque episode."""

    incident_id: IncidentId


class GetPolicyRequest(StrictModel):
    """Request the single fixed policy document exposed to the verifier."""

    policy_ref: PolicyRef


class GetSignalWindowRequest(StrictModel):
    """Request one allow-listed signal within one fixed packet window."""

    incident_id: IncidentId
    signal_enum: SignalEnum
    window_enum: WindowEnum


class GetRequestSamplesRequest(StrictModel):
    """Request at most twenty request samples from one fixed packet window."""

    incident_id: IncidentId
    window_enum: WindowEnum
    limit: StrictInt = Field(ge=1, le=20)


class GetDeploymentSnapshotRequest(StrictModel):
    """Request one deployment snapshot from one fixed packet window."""

    incident_id: IncidentId
    window_enum: WindowEnum


class SearchRunbookRequest(StrictModel):
    """Request bounded lookup by a curated enum, never free-form text."""

    query_enum: RunbookQueryEnum
    top_k: StrictInt = Field(ge=1, le=3)


class GetEvidenceRequest(StrictModel):
    """Request up to twenty immutable evidence references."""

    evidence_ids: tuple[EvidenceId, ...] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def evidence_ids_are_unique(self) -> "GetEvidenceRequest":
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("evidence IDs must be unique")
        return self


class PrepareActionPlanRequest(StrictModel):
    """Request a simulator-only action preview for one detected incident."""

    incident_id: IncidentId
    action_type: ActionType


_PROTECTED_KEY_NAMES = frozenset(
    {
        "allowed_actions",
        "acceptable_conclusions",
        "artifact_path",
        "expected_detector_behavior",
        "expected_leading_family",
        "filesystem_path",
        "forbidden_conclusions",
        "fault_label",
        "generation_seed",
        "injected_end_s",
        "injected_onset_s",
        "mechanism",
        "oracle",
        "oracle_root",
        "path",
        "prohibited_actions",
        "recovery_truth",
        "required_contradicting_evidence",
        "required_evidence",
        "root",
        "root_cause",
        "scenario_family",
    }
)
_PATH_OR_COMMAND_TEXT = re.compile(r"(?:[/\\]|^~|://|\.\.[/\\]|\$\(|`|;|&&|\|\|)")


def _contains_protected_output(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = str(key).casefold()
            if (
                normalized_key in _PROTECTED_KEY_NAMES
                or "oracle" in normalized_key
                or "path" in normalized_key
            ):
                return True
            if _contains_protected_output(item):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_contains_protected_output(item) for item in value)
    return isinstance(value, str) and _PATH_OR_COMMAND_TEXT.search(value) is not None


class EvidenceRecord(StrictModel):
    """Path-free normalized evidence returned by an evidence tool."""

    schema_version: SchemaVersion = "1.0"
    evidence_id: EvidenceId
    incident_id: IncidentId
    source: StrictStr = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    signal: SignalEnum
    aggregation: StrictStr = Field(min_length=1, max_length=32, pattern=r"^[a-z0-9_]+$")
    unit: StrictStr = Field(min_length=1, max_length=32, pattern=r"^[a-z0-9_]+$")
    value: Any
    window_start_s: StrictFloat = Field(ge=0)
    window_end_s: StrictFloat = Field(ge=0)
    freshness_s: StrictFloat = Field(ge=0)
    data_origin: Literal["synthetic_replay"] = "synthetic_replay"
    observed_at: AwareDatetime

    @field_validator("value")
    @classmethod
    def reject_protected_output(cls, value: Any) -> Any:
        """Keep oracle and filesystem context out of every evidence response."""

        if _contains_protected_output(value):
            raise ValueError("evidence contains protected fields")
        return value


class EvidenceBatchResponse(StrictModel):
    """Path-free evidence records tied to one episode."""

    schema_version: SchemaVersion = "1.0"
    incident_id: IncidentId
    evidence: tuple[EvidenceRecord, ...] = Field(min_length=1, max_length=20)


class SignalWindowResponse(StrictModel):
    """Bounded signal evidence for one fixed interval."""

    schema_version: SchemaVersion = "1.0"
    incident_id: IncidentId
    signal: SignalEnum
    window: WindowEnum
    evidence: tuple[EvidenceRecord, ...] = Field(max_length=20)


class RequestSample(StrictModel):
    """Safe, normalized fields for one request; prompt content is impossible."""

    schema_version: SchemaVersion = "1.0"
    evidence_id: EvidenceId
    incident_id: IncidentId
    request_id: StrictStr = Field(pattern=r"^req-[0-9]+$", max_length=64)
    submit_s: StrictFloat = Field(ge=0)
    completion_s: StrictFloat | None = Field(default=None, ge=0)
    status: RequestStatus
    ttft_ms: StrictFloat | None = Field(default=None, ge=0)
    itl_ms: StrictFloat | None = Field(default=None, ge=0)
    queue_wait_ms: StrictFloat | None = Field(default=None, ge=0)
    input_tokens: StrictInt | None = Field(default=None, ge=0)
    output_tokens: StrictInt | None = Field(default=None, ge=0)
    replica_index: StrictInt | None = Field(default=None, ge=0)


class RequestSamplesResponse(StrictModel):
    """A deterministic, twenty-record maximum request sample response."""

    schema_version: SchemaVersion = "1.0"
    incident_id: IncidentId
    window: WindowEnum
    samples: tuple[RequestSample, ...] = Field(max_length=20)


class DeploymentSnapshotResponse(StrictModel):
    """A path-free deployment snapshot with its immutable evidence ID."""

    schema_version: SchemaVersion = "1.0"
    incident_id: IncidentId
    window: WindowEnum
    snapshot: DeploymentSnapshot
    evidence_id: EvidenceId

    @field_validator("snapshot")
    @classmethod
    def reject_protected_revision(cls, snapshot: DeploymentSnapshot) -> DeploymentSnapshot:
        """Do not return a path-like deployment revision."""

        if _contains_protected_output(snapshot.revision):
            raise ValueError("deployment snapshot contains protected fields")
        return snapshot


class PolicyResponse(StrictModel):
    """The immutable v1 policy facts needed by an independent verifier."""

    schema_version: SchemaVersion = "1.0"
    policy_ref: PolicyRef
    max_evidence_ids: StrictInt = Field(ge=1, le=20)
    max_request_samples: StrictInt = Field(ge=1, le=20)
    max_runbook_results: StrictInt = Field(ge=1, le=3)
    allowed_action_types: tuple[ActionType, ...] = Field(min_length=1, max_length=1)
    allowed_target_kind: Literal["simulator"] = "simulator"
    from_replicas: StrictInt = Field(ge=0)
    to_replicas: StrictInt = Field(ge=0)
    action_ttl_seconds: StrictInt = Field(gt=0, le=300)
    requires_human_approval: Literal[True] = True
    dry_run_only: Literal[True] = True
    execution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def enforce_frozen_policy(self) -> "PolicyResponse":
        if self.policy_ref is not PolicyRef.INFINEQ_V1:
            raise ValueError("policy reference is not allow-listed")
        if self.allowed_action_types != (ActionType.SIMULATED_SCALE_OUT,):
            raise ValueError("action type is not allow-listed")
        if self.from_replicas != 1 or self.to_replicas != 2:
            raise ValueError("replica bounds are not allow-listed")
        if self.action_ttl_seconds != 300:
            raise ValueError("action TTL is not allow-listed")
        return self


class DryRunActionPlan(ActionPlanV1):
    """ActionPlanV1 with explicit non-execution markers."""

    dry_run: Literal[True] = True
    executed: Literal[False] = False
    execution_allowed: Literal[False] = False


_RUNBOOK_SHELL_TEXT = re.compile(
    r"(?:`|\$\(|\$\{|&&|[|;<>])|"
    r"(?:^|\s)(?:az|bash|cat|cd|chmod|chown|curl|docker|echo|env|eval|exec|export|find|git|grep|helm|kill|kubectl|make|mv|nc|pip|printf|python(?:3)?|rm|scp|sed|service|sh|sleep|source|ssh|sudo|systemctl|terraform|timeout|wget|which|uv)\b|"
    r"(?:^|\s)(?:/|~/|\.\.?/)",
    re.IGNORECASE,
)
_RUNBOOK_PROTECTED_TEXT = frozenset(
    {
        "artifact_path",
        "fault_label",
        "filesystem_path",
        "hidden_oracles",
        "oracle",
        "root_cause",
    }
)


def _validate_runbook_text(value: str) -> str:
    normalized = value.casefold()
    if _RUNBOOK_SHELL_TEXT.search(value) or any(
        marker in normalized for marker in _RUNBOOK_PROTECTED_TEXT
    ):
        raise ValueError("runbook text must not contain executable or protected content")
    return value


def _validate_runbook_text_items(values: tuple[StrictStr, ...]) -> tuple[StrictStr, ...]:
    for value in values:
        _validate_runbook_text(value)
    return values


class RunbookResult(StrictModel):
    """Immutable, safe runbook metadata; executable shell text is not representable."""

    schema_version: SchemaVersion = "1.0"
    query_enum: RunbookQueryEnum = Field(
        validation_alias=AliasChoices("query_enum", "symptom_family")
    )
    evidence_id: StrictStr = Field(pattern=r"^runbook-[a-z0-9-]+$", max_length=128)
    title: StrictStr = Field(min_length=1, max_length=200)
    summary: StrictStr = Field(min_length=1, max_length=500)
    prerequisites: tuple[StrictStr, ...] = Field(min_length=1, max_length=10)
    diagnostic_checks: tuple[StrictStr, ...] = Field(min_length=1, max_length=10)
    bounded_action_class: Literal["simulator_only"] = "simulator_only"
    approval_required: Literal[True] = True
    rollback_criteria: tuple[StrictStr, ...] = Field(min_length=1, max_length=10)
    verification_criteria: tuple[StrictStr, ...] = Field(min_length=1, max_length=10)
    source_url: StrictStr = Field(min_length=1, max_length=512)
    retrieval_date: date
    source_version: StrictStr = Field(min_length=1, max_length=128)

    @property
    def symptom_family(self) -> RunbookQueryEnum:
        """Name the query enum as the runbook's symptom family."""

        return self.query_enum

    @field_validator("title", "summary", "source_version")
    @classmethod
    def reject_unsafe_runbook_text(cls, value: str) -> str:
        return _validate_runbook_text(value)

    @field_validator(
        "prerequisites",
        "diagnostic_checks",
        "rollback_criteria",
        "verification_criteria",
    )
    @classmethod
    def reject_unsafe_runbook_text_items(
        cls, values: tuple[StrictStr, ...]
    ) -> tuple[StrictStr, ...]:
        return _validate_runbook_text_items(values)

    @field_validator("source_url")
    @classmethod
    def require_public_https_source(cls, value: str) -> str:
        try:
            parsed = urlsplit(value)
        except ValueError:
            raise ValueError("runbook source must be a public HTTPS URL") from None
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("runbook source must be a public HTTPS URL")
        return value

    @model_validator(mode="after")
    def enforce_safe_runbook_contract(self) -> "RunbookResult":
        if self.evidence_id != self.evidence_id.casefold():
            raise ValueError("runbook evidence IDs must be lowercase")
        return self


class RunbookSearchResponse(StrictModel):
    """A bounded curated lookup or an explicit Task 6 placeholder."""

    schema_version: SchemaVersion = "1.0"
    query_enum: RunbookQueryEnum
    top_k: StrictInt = Field(ge=1, le=3)
    status: RunbookSearchStatus
    placeholder: bool
    results: tuple[RunbookResult, ...] = Field(max_length=3)
    notice: StrictStr = Field(min_length=1, max_length=300)


__all__ = [
    "DeploymentSnapshotResponse",
    "DryRunActionPlan",
    "EvidenceBatchResponse",
    "EvidenceRecord",
    "GetDeploymentSnapshotRequest",
    "GetEvidenceRequest",
    "GetIncidentPacketRequest",
    "GetPolicyRequest",
    "GetRequestSamplesRequest",
    "GetSignalWindowRequest",
    "IncidentId",
    "PolicyRef",
    "PolicyResponse",
    "PrepareActionPlanRequest",
    "RequestSample",
    "RequestSamplesResponse",
    "RequestStatus",
    "RunbookQueryEnum",
    "RunbookResult",
    "RunbookSearchResponse",
    "RunbookSearchStatus",
    "SearchRunbookRequest",
    "SignalEnum",
    "SignalWindowResponse",
    "WindowEnum",
]
