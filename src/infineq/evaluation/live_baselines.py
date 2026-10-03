"""Explicitly gated live matched baselines for one canonical development episode."""

from __future__ import annotations

import json
import os
import re
import traceback
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from time import perf_counter
from typing import Any, Literal, TypeVar, cast

from azure.ai.projects import AIProjectClient
from azure.core.credentials import TokenCredential
from azure.identity import DefaultAzureCredential
from dotenv import dotenv_values
from pydantic import Field, ValidationError

from infineq.agents.investigator import Investigator, validate_investigation_result
from infineq.agents.prompt_manifest import (
    VERIFIER_PROMPT_VERSION,
    PromptManifest,
    load_prompt_manifest,
    load_verifier_prompt_manifest,
)
from infineq.agents.tool_definitions import (
    INVESTIGATOR_TOOL_SCHEMA_SHA256,
)
from infineq.agents.tool_loop import ToolCallTrace
from infineq.agents.verifier import Verifier
from infineq.config import FoundrySettings, Settings
from infineq.detection.detector import DetectionRun, Detector
from infineq.evaluation.baselines import (
    CANONICAL_QUEUE_EPISODE_ID,
    BaselineEpisode,
    BaselineEvaluation,
    BaselineMode,
    BaselineRunRecord,
    BaselineTerminalStatus,
    BaselineVersions,
    DetectorStatus,
    DevelopmentOracle,
    ModePassCount,
    OracleLoader,
    ResourceBudget,
    VisibleEvidence,
    baseline_run_directory,
    load_development_oracle,
    run_static_baseline,
    score_development_baselines,
    select_development_episode_ids,
    validate_matched_baseline_runs,
)
from infineq.evidence.store import EvidenceStore
from infineq.evidence.tool_schemas import (
    GetPolicyRequest,
    PolicyRef,
    PolicyResponse,
    RunbookQueryEnum,
)
from infineq.evidence.tools import InvestigatorTools, VerifierTools
from infineq.foundry.agent_registry import (
    INVESTIGATOR_AGENT_NAME,
    VERIFIER_AGENT_NAME,
    AgentVersionRecord,
    PromptAgentRegistry,
    VerifierAgentVersionRecord,
    build_investigation_result_json_schema,
    build_verification_result_json_schema,
)
from infineq.foundry.client import AzureFoundryResponsesClient
from infineq.foundry.deployment import (
    VerifierDeploymentManifest,
    deployment_manifest_path,
    load_verifier_deployment_manifest,
)
from infineq.foundry.verifier_tools import VERIFIER_TOOL_SCHEMA_SHA256
from infineq.schemas.common import StrictModel
from infineq.schemas.incident import IncidentPacketV1
from infineq.schemas.investigation import Disposition, InvestigationResultV1
from infineq.schemas.verification import VerificationResultV1, VerificationStatus
from infineq.security.redaction import redact_mapping, redact_text
from infineq.workflow.corrections import CorrectionPacketV1
from infineq.workflow.orchestrator import (
    InvestigatorProtocol,
    VerifierProtocol,
    WorkflowOrchestrator,
)
from infineq.workflow.policy import evaluate_action_policy
from infineq.workflow.transitions import WorkflowFailure, WorkflowState

CANONICAL_EPISODE_ID: str = CANONICAL_QUEUE_EPISODE_ID
LIVE_OPT_IN_ENV: str = "INFIN_EQ_RUN_LIVE_BASELINES"
LIVE_DIAGNOSTICS_ENV: str = "INFIN_EQ_LIVE_DIAGNOSTICS"
INVESTIGATOR_VERSION: str = "14"
VERIFIER_VERSION: str = "1"
LIVE_WORKFLOW_VERSION: str = "workflow-v1"
_DEVELOPMENT_LIVE_RUN_ID = re.compile(r"^run-phase5-development-live-v[0-9]+$")
_MODE_INDEX: dict[BaselineMode, int] = {
    BaselineMode.STATIC: 0,
    BaselineMode.INVESTIGATOR: 1,
    BaselineMode.INVESTIGATOR_VERIFIER: 2,
}

_ResultT = TypeVar("_ResultT")
_DIAGNOSTIC_URL = re.compile(r"(?i)https?://[^\s\"']+")
_DIAGNOSTIC_UUID = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
)
_DIAGNOSTIC_SENSITIVE_FIELD = re.compile(
    r"(?i)(\b(?:account|agent(?:[_-]?(?:name|version))?|call[_-]?id|client[_-]?id|"
    r"completion|credential|endpoint|episode[_-]?id|expected(?:[_-]?(?:diagnosis|label))?|"
    r"hidden[_-]?oracle|investigation[_-]?id|oracle|project[_-]?id|prompt(?:[_-]?(?:body|hash|"
    r"sha256|version))?|request[_-]?body|resource[_-]?id|response[_-]?(?:body|id)|run[_-]?id|"
    r"subscription|tenant[_-]?id|verification[_-]?id)\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s;,]+)"
)
_DIAGNOSTIC_ID = re.compile(
    r"(?i)\b(?:call|ep|investigation|resp|run|trace|verification|workflow)-"
    r"[A-Za-z0-9][A-Za-z0-9_-]*\b"
)
_DIAGNOSTIC_ORACLE_LABEL = re.compile(
    r"(?i)\b(?:backend_errors_timeouts|backend_slowdown|capacity_queueing|"
    r"queue_saturation|replica_or_deployment_regression|workload_shape_change)\b"
)
_DIAGNOSTIC_PHASE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

LIVE_BASELINE_BUDGET = ResourceBudget(
    budget_id="budget-live-v1",
    max_visible_evidence=20,
    max_successful_tool_calls=8,
    max_redundant_tool_calls=3,
    max_runbook_chunks=3,
    max_input_tokens=50_000,
    max_output_tokens=10_000,
    max_total_tokens=60_000,
    max_latency_ms=120_000.0,
)


class LivePrerequisiteError(RuntimeError):
    """A live run was not allowed to reach a cloud or evaluator boundary."""


class LiveArtifactError(ValueError):
    """A saved live artifact is missing, unsafe, or outside its run directory."""


def _sanitize_live_diagnostic_message(value: str) -> str:
    """Keep provider error wording while removing identifiers and content bodies."""

    redacted = redact_text(value)
    redacted = _DIAGNOSTIC_URL.sub("[REDACTED_URL]", redacted)
    redacted = _DIAGNOSTIC_SENSITIVE_FIELD.sub(r"\1[REDACTED]", redacted)
    redacted = _DIAGNOSTIC_ID.sub("[REDACTED_ID]", redacted)
    redacted = _DIAGNOSTIC_ORACLE_LABEL.sub("[REDACTED_LABEL]", redacted)
    redacted = _DIAGNOSTIC_UUID.sub("[REDACTED_ID]", redacted)
    return redacted


def _diagnostic_chain(error: BaseException) -> list[dict[str, str]]:
    chain: list[dict[str, str]] = []
    current: BaseException | None = error
    while current is not None and len(chain) < 8:
        chain.append(
            {
                "exception_type": type(current).__name__,
                "message": _sanitize_live_diagnostic_message(str(current)),
            }
        )
        current = current.__cause__ or current.__context__
    return chain


def _write_live_diagnostic(path: Path, *, phase: str, error: BaseException) -> None:
    """Write only safe exception metadata and frame locations to a local file."""

    frames = [
        {
            "file": Path(frame.filename).name,
            "line": frame.lineno,
            "function": frame.name,
        }
        for frame in traceback.extract_tb(error.__traceback__)
    ]
    payload = {
        "schema_version": "live-diagnostic-v1",
        "phase": phase if _DIAGNOSTIC_PHASE.fullmatch(phase) else "[REDACTED_PHASE]",
        "exception_type": type(error).__name__,
        "message": _sanitize_live_diagnostic_message(str(error)),
        "cause_chain": _diagnostic_chain(error),
        "traceback": frames,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2) + "\n")
    with suppress(OSError):
        path.chmod(0o600)


def run_live_step(  # noqa: UP047
    phase: str,
    operation: Callable[[], _ResultT],
    *,
    diagnostic_path: Path | None = None,
) -> _ResultT:
    """Run one live step and optionally record a safe local diagnostic before rethrowing."""

    try:
        return operation()
    except Exception as error:
        if (
            diagnostic_path is not None
            and os.environ.get(LIVE_OPT_IN_ENV) == "1"
            and os.environ.get(LIVE_DIAGNOSTICS_ENV) == "1"
        ):
            with suppress(OSError):
                _write_live_diagnostic(diagnostic_path, phase=phase, error=error)
        raise


class LiveModeStatus(StrEnum):
    """Safe status values written into a live mode artifact."""

    COMPLETE = "complete"
    ABSTAINED = "abstained"
    BLOCKED = "blocked"
    TIMEOUT = "timeout"
    INVALID_SCHEMA = "invalid_schema"
    ANALYSIS_INCOMPLETE = "analysis_incomplete"


class InvestigatorDeploymentManifest(StrictModel):
    """Credential-free local identity for the exact Investigator reuse version."""

    agent_name: Literal["infineq-investigator"]
    agent_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    prompt_version: Literal["investigator-v14"]
    prompt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    model_deployment_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    tool_schema_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class LiveAgentPin(StrictModel):
    """Safe identity and schema hashes for one provider agent."""

    agent_name: str = Field(min_length=1, max_length=63)
    agent_version: str = Field(min_length=1, max_length=64)
    prompt_version: str = Field(pattern=r"^(?:investigator|verifier)-v[0-9]+$")
    prompt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    tool_schema_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    response_schema_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    model_deployment_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class LiveAgentPins(StrictModel):
    """The two exact agent pins recorded in a live run."""

    investigator: LiveAgentPin
    verifier: LiveAgentPin


@dataclass(frozen=True, slots=True)
class LivePrerequisites:
    """Validated, non-cloud inputs required to retrieve exact provider versions."""

    foundry: FoundrySettings
    investigator_prompt: PromptManifest
    verifier_prompt: PromptManifest
    investigator_manifest: InvestigatorDeploymentManifest
    verifier_manifest: VerifierDeploymentManifest


@dataclass(frozen=True, slots=True)
class LiveRuntime:
    """Cloud clients and typed agent boundaries created after preflight."""

    project: object
    store: EvidenceStore
    investigator: Investigator
    verifier: Verifier
    policy: PolicyResponse
    pins: LiveAgentPins

    def close(self) -> None:
        """Close the project client; agent boundaries hold no independent resource."""

        close = getattr(self.project, "close", None)
        if callable(close):
            close()


@dataclass(frozen=True, slots=True)
class _CapturedAgentRun:
    """Safe in-memory accounting collected without retaining prompts or tool bodies."""

    result: InvestigationResultV1 | VerificationResultV1
    raw_output: str
    response_ids: tuple[str, ...]
    trace_records: tuple[dict[str, object], ...]
    returned_evidence_ids: frozenset[str]
    successful_tool_calls: int
    forbidden_tool_calls: int
    tool_retry_count: int
    repair_attempted: bool
    usage: tuple[int | None, int | None, int | None]
    latency_ms: float
    error: str | None = None


@dataclass(frozen=True, slots=True)
class LiveBaselineExecution:
    """Validated matched result and its contained artifact directory."""

    run_id: str
    output_directory: Path
    episode: BaselineEpisode
    runs: tuple[BaselineRunRecord, ...]
    workflow: object
    evaluation: BaselineEvaluation | None
    pins: LiveAgentPins | None = None


@dataclass(frozen=True, slots=True)
class LiveDevelopmentExecution:
    """Validated aggregate of one matched run for every development episode."""

    run_id: str
    output_directory: Path
    episode_ids: tuple[str, ...]
    episodes: tuple[BaselineEpisode, ...]
    runs: tuple[BaselineRunRecord, ...]
    workflows: tuple[object, ...]
    evaluation: BaselineEvaluation
    aggregate_metrics: Mapping[str, object] = field(default_factory=dict)
    pins: LiveAgentPins | None = None

    @property
    def aggregate(self) -> Mapping[str, object]:
        """Compatibility alias for the persisted aggregate metrics."""

        return self.aggregate_metrics


def _invoke_investigator(
    delegate: object,
    packet: IncidentPacketV1,
    correction_packet: CorrectionPacketV1 | None,
) -> object:
    method_name = "investigate" if correction_packet is None else "investigate_with_correction"
    method = getattr(delegate, method_name, None)
    if not callable(method):
        raise TypeError(f"Investigator adapter does not expose {method_name}")
    if correction_packet is None:
        return method(packet)
    return method(packet, correction_packet)


class _RecordingInvestigator(InvestigatorProtocol):
    """Collect safe accounting around a concrete or deterministic Investigator."""

    def __init__(self, delegate: object) -> None:
        self._delegate = delegate
        self.runs: list[_CapturedAgentRun] = []

    def investigate(
        self,
        packet: IncidentPacketV1,
        *,
        correction_packet: CorrectionPacketV1 | None = None,
    ) -> InvestigationResultV1:
        started = perf_counter()
        try:
            candidate = _invoke_investigator(self._delegate, packet, correction_packet)
            if not isinstance(candidate, InvestigationResultV1):
                raise TypeError("Investigator adapter returned an invalid result type")
            result = candidate
        except Exception as error:
            self.runs.append(
                _CapturedAgentRun(
                    result=_incomplete_investigation(packet),
                    raw_output="",
                    response_ids=(),
                    trace_records=(),
                    returned_evidence_ids=frozenset(),
                    successful_tool_calls=0,
                    forbidden_tool_calls=0,
                    tool_retry_count=0,
                    repair_attempted=False,
                    usage=(None, None, None),
                    latency_ms=_elapsed_ms(started),
                    error=redact_text(str(error)),
                )
            )
            raise
        self.runs.append(_capture_agent_run(self._delegate, result, _elapsed_ms(started)))
        return result

    @property
    def successful_tool_calls(self) -> int:
        return sum(item.successful_tool_calls for item in self.runs)

    @property
    def returned_evidence_ids(self) -> frozenset[str]:
        return frozenset(
            evidence_id for item in self.runs for evidence_id in item.returned_evidence_ids
        )

    @property
    def tool_calls_used(self) -> int:
        return self.successful_tool_calls

    @property
    def last_run(self) -> object:
        value = getattr(self._delegate, "last_run", None)
        if value is None:
            return None
        return value


class _ReusedInvestigator(InvestigatorProtocol):
    """Replay the first Investigator result into the two-agent comparison."""

    def __init__(self, recording: _RecordingInvestigator, initial: InvestigationResultV1) -> None:
        self._recording = recording
        self._initial = initial

    def investigate(
        self,
        packet: IncidentPacketV1,
        *,
        correction_packet: CorrectionPacketV1 | None = None,
    ) -> InvestigationResultV1:
        if correction_packet is None:
            if packet.incident_id != self._initial.incident_id:
                raise ValueError("reused Investigator result crosses the incident boundary")
            return self._initial
        return self._recording.investigate(packet, correction_packet=correction_packet)

    @property
    def successful_tool_calls(self) -> int:
        return self._recording.successful_tool_calls

    @property
    def returned_evidence_ids(self) -> frozenset[str]:
        return self._recording.returned_evidence_ids

    @property
    def tool_calls_used(self) -> int:
        return self.successful_tool_calls

    @property
    def last_run(self) -> object:
        return self._recording.last_run


class _RecordingVerifier(VerifierProtocol):
    """Collect safe accounting around every finite Verifier invocation."""

    def __init__(self, delegate: VerifierProtocol) -> None:
        self._delegate = delegate
        self.runs: list[_CapturedAgentRun] = []

    def verify(
        self,
        packet: IncidentPacketV1,
        investigation: InvestigationResultV1,
    ) -> VerificationResultV1:
        started = perf_counter()
        try:
            result = self._delegate.verify(packet, investigation)
            if not isinstance(result, VerificationResultV1):
                raise TypeError("Verifier adapter returned an invalid result type")
        except Exception as error:
            self.runs.append(
                _CapturedAgentRun(
                    result=_blocked_verification(packet, investigation),
                    raw_output="",
                    response_ids=(),
                    trace_records=(),
                    returned_evidence_ids=frozenset(),
                    successful_tool_calls=0,
                    forbidden_tool_calls=0,
                    tool_retry_count=0,
                    repair_attempted=False,
                    usage=(None, None, None),
                    latency_ms=_elapsed_ms(started),
                    error=redact_text(str(error)),
                )
            )
            raise
        self.runs.append(_capture_agent_run(self._delegate, result, _elapsed_ms(started)))
        return result

    @property
    def successful_tool_calls(self) -> int:
        return sum(item.successful_tool_calls for item in self.runs)

    @property
    def returned_evidence_ids(self) -> frozenset[str]:
        return frozenset(
            evidence_id for item in self.runs for evidence_id in item.returned_evidence_ids
        )

    @property
    def tool_calls_used(self) -> int:
        return self.successful_tool_calls

    @property
    def last_run(self) -> object:
        value = getattr(self._delegate, "last_run", None)
        if value is None:
            return None
        return value

    @property
    def lookup_allowance(self) -> object | None:
        value = getattr(self._delegate, "lookup_allowance", None)
        return value if value is not None else getattr(self._delegate, "permitted_lookup", None)

    @property
    def permitted_lookup(self) -> object | None:
        return self.lookup_allowance


def _elapsed_ms(started: float) -> float:
    return round((perf_counter() - started) * 1_000, 3)


def _incomplete_investigation(packet: IncidentPacketV1) -> InvestigationResultV1:
    return InvestigationResultV1(
        investigation_id=f"investigation-{packet.incident_id}",
        incident_id=packet.incident_id,
        completed_at=datetime.now(UTC),
        disposition=Disposition.ANALYSIS_INCOMPLETE,
        summary="Investigator did not return a schema-valid result.",
        hypotheses=(),
        leading_hypothesis_id=None,
        proposed_action=None,
        cited_evidence_ids=(),
        limitations=("live Investigator call failed",),
    )


def _packetless_investigation(detection: DetectionRun) -> InvestigationResultV1:
    if detection.decision == "no_incident":
        disposition = Disposition.NO_INCIDENT
        summary = "Deterministic detector returned no incident; agents were not invoked."
        limitation = "no incident packet was produced"
    elif detection.decision == "abstain":
        disposition = Disposition.INDETERMINATE
        summary = "Deterministic detector abstained because required data was not decision-ready."
        limitation = "required data quality was not decision-ready"
    else:
        raise LivePrerequisiteError(
            "cannot build a packetless result for this detector disposition"
        )
    return InvestigationResultV1(
        investigation_id=f"investigation-{detection.episode_id}",
        incident_id=detection.episode_id,
        completed_at=datetime.now(UTC),
        disposition=disposition,
        summary=summary,
        hypotheses=(),
        leading_hypothesis_id=None,
        proposed_action=None,
        cited_evidence_ids=(),
        limitations=(limitation,),
    )


def _synthetic_capture(result: InvestigationResultV1) -> _CapturedAgentRun:
    return _CapturedAgentRun(
        result=result,
        raw_output=result.model_dump_json(),
        response_ids=(),
        trace_records=(),
        returned_evidence_ids=frozenset(),
        successful_tool_calls=0,
        forbidden_tool_calls=0,
        tool_retry_count=0,
        repair_attempted=False,
        usage=(0, 0, 0),
        latency_ms=0.0,
        error=None,
    )


def _blocked_verification(
    packet: IncidentPacketV1,
    investigation: InvestigationResultV1,
) -> VerificationResultV1:
    return VerificationResultV1(
        verification_id=f"verification-{packet.incident_id}",
        incident_id=packet.incident_id,
        investigation_id=investigation.investigation_id,
        checked_at=datetime.now(UTC),
        status=VerificationStatus.BLOCKED,
        claim_checks=(),
        issues=("live Verifier call failed",),
        correction_requests=(),
        policy_ref=PolicyRef.INFINEQ_V1.value,
    )


def _safe_status(value: object) -> str | None:
    status = getattr(value, "status", None)
    if status is None:
        return None
    status_value = getattr(status, "value", status)
    return status_value if isinstance(status_value, str) else None


def _safe_usage(value: object) -> tuple[int | None, int | None, int | None]:
    if value is None:
        return (None, None, None)
    if isinstance(value, (tuple, list)) and len(value) == 3:
        return tuple(item if isinstance(item, int) and item >= 0 else None for item in value)  # type: ignore[return-value]
    return (
        getattr(value, "input_tokens", None)
        if isinstance(getattr(value, "input_tokens", None), int)
        else None,
        getattr(value, "output_tokens", None)
        if isinstance(getattr(value, "output_tokens", None), int)
        else None,
        getattr(value, "total_tokens", None)
        if isinstance(getattr(value, "total_tokens", None), int)
        else None,
    )


def _trace_record(trace: object) -> dict[str, object]:
    if isinstance(trace, ToolCallTrace):
        return {
            "response_id": trace.response_id,
            "call_id": trace.call_id,
            "tool_name": trace.name,
            "status": trace.status,
            "retry_count": trace.retry_count,
            "returned_evidence_ids": trace.returned_evidence_ids,
        }
    if isinstance(trace, Mapping):
        return {
            key: value
            for key, value in trace.items()
            if key
            in {
                "response_id",
                "call_id",
                "tool_name",
                "name",
                "status",
                "retry_count",
                "returned_evidence_ids",
            }
        }
    return {"status": "trace_unavailable"}


def _result_evidence_ids(result: InvestigationResultV1 | VerificationResultV1) -> tuple[str, ...]:
    if isinstance(result, InvestigationResultV1):
        return result.cited_evidence_ids
    return tuple(evidence_id for check in result.claim_checks for evidence_id in check.evidence_ids)


def _capture_agent_run(
    agent: object,
    result: InvestigationResultV1 | VerificationResultV1,
    latency_ms: float,
) -> _CapturedAgentRun:
    usage: tuple[int | None, int | None, int | None]
    last_run = getattr(agent, "last_run", None)
    if last_run is None:
        raw_output = result.model_dump_json()
        response_ids: tuple[str, ...] = ()
        traces: tuple[object, ...] = ()
        returned = frozenset(_result_evidence_ids(result))
        successful = 0
        forbidden = 0
        retries = 0
        repair = False
        usage = (0, 0, 0)
        error = None
    else:
        raw_value = getattr(last_run, "final_output_text", "")
        raw_output = raw_value if isinstance(raw_value, str) else result.model_dump_json()
        response_ids = tuple(
            value for value in getattr(last_run, "response_ids", ()) if isinstance(value, str)
        )
        traces = tuple(getattr(last_run, "traces", ()))
        returned = frozenset(
            value
            for value in getattr(last_run, "returned_evidence_ids", ())
            if isinstance(value, str)
        )
        if not returned:
            returned = frozenset(_result_evidence_ids(result))
        successful = getattr(last_run, "successful_tool_calls", 0)
        forbidden = getattr(last_run, "forbidden_tool_calls", 0)
        retries = getattr(last_run, "tool_retry_count", 0)
        repair = getattr(last_run, "repair_attempted", False) is True
        usage = _safe_usage(getattr(last_run, "usage", None))
        error = None
    return _CapturedAgentRun(
        result=result,
        raw_output=raw_output,
        response_ids=response_ids,
        trace_records=tuple(_trace_record(trace) for trace in traces),
        returned_evidence_ids=returned,
        successful_tool_calls=successful if isinstance(successful, int) and successful >= 0 else 0,
        forbidden_tool_calls=forbidden if isinstance(forbidden, int) and forbidden >= 0 else 0,
        tool_retry_count=retries if isinstance(retries, int) and retries >= 0 else 0,
        repair_attempted=repair,
        usage=usage,
        latency_ms=latency_ms,
        error=error,
    )


def _aggregate_runs(runs: Sequence[_CapturedAgentRun], result: Any) -> _CapturedAgentRun:
    if not runs:
        return _capture_agent_run(object(), result, 0.0)
    input_tokens = [item.usage[0] for item in runs]
    output_tokens = [item.usage[1] for item in runs]
    total_tokens = [item.usage[2] for item in runs]
    return _CapturedAgentRun(
        result=result,
        raw_output=runs[-1].raw_output,
        response_ids=tuple(value for item in runs for value in item.response_ids),
        trace_records=tuple(record for item in runs for record in item.trace_records),
        returned_evidence_ids=frozenset(
            value for item in runs for value in item.returned_evidence_ids
        ),
        successful_tool_calls=sum(item.successful_tool_calls for item in runs),
        forbidden_tool_calls=sum(item.forbidden_tool_calls for item in runs),
        tool_retry_count=sum(item.tool_retry_count for item in runs),
        repair_attempted=any(item.repair_attempted for item in runs),
        usage=(
            sum(value for value in input_tokens if value is not None)
            if all(value is not None for value in input_tokens)
            else None,
            sum(value for value in output_tokens if value is not None)
            if all(value is not None for value in output_tokens)
            else None,
            sum(value for value in total_tokens if value is not None)
            if all(value is not None for value in total_tokens)
            else None,
        ),
        latency_ms=round(sum(item.latency_ms for item in runs), 3),
        error=next((item.error for item in runs if item.error is not None), None),
    )


def _read_json(path: Path, *, description: str) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LivePrerequisiteError(f"{description} is unreadable") from error
    if not isinstance(payload, dict):
        raise LivePrerequisiteError(f"{description} must be a JSON object")
    return cast(dict[str, object], payload)


def _load_investigator_deployment_manifest(
    project_root: Path,
) -> InvestigatorDeploymentManifest:
    payload = _read_json(
        deployment_manifest_path(project_root),
        description="Investigator deployment manifest",
    )
    try:
        return InvestigatorDeploymentManifest.model_validate(payload)
    except ValidationError as error:
        raise LivePrerequisiteError("Investigator deployment manifest is invalid") from error


def _environment(project_root: Path) -> dict[str, str]:
    values: dict[str, str] = {
        key: value
        for key, value in dotenv_values(project_root / ".env").items()
        if isinstance(value, str)
    }
    values.update({key: value for key, value in os.environ.items() if isinstance(value, str)})
    return values


def load_live_prerequisites(
    project_root: Path,
    *,
    environment: Mapping[str, str] | None = None,
) -> LivePrerequisites:
    """Validate opt-in, endpoint/model, and both exact local manifests without cloud calls."""

    values = dict(environment) if environment is not None else _environment(project_root)
    if values.get(LIVE_OPT_IN_ENV) != "1":
        raise LivePrerequisiteError(f"{LIVE_OPT_IN_ENV}=1 is required for live baselines")
    try:
        settings = Settings.from_mapping(values, project_root=project_root)
        foundry = settings.require_foundry()
        investigator_prompt = load_prompt_manifest(project_root=project_root)
        verifier_prompt = load_verifier_prompt_manifest(project_root=project_root)
        investigator_manifest = _load_investigator_deployment_manifest(project_root)
        verifier_manifest = load_verifier_deployment_manifest(project_root)
    except (FileNotFoundError, OSError, ValidationError, ValueError) as error:
        raise LivePrerequisiteError(
            "exact local live manifests or prompt metadata are invalid"
        ) from error

    if investigator_prompt.version != "investigator-v14":
        raise LivePrerequisiteError("Investigator prompt must be investigator-v14")
    if verifier_prompt.version != VERIFIER_PROMPT_VERSION:
        raise LivePrerequisiteError(f"Verifier prompt must be {VERIFIER_PROMPT_VERSION}")
    if investigator_manifest.agent_version != INVESTIGATOR_VERSION:
        raise LivePrerequisiteError("Investigator local manifest must pin version 14")
    if (
        investigator_manifest.prompt_version != investigator_prompt.version
        or investigator_manifest.prompt_sha256 != investigator_prompt.prompt_sha256
    ):
        raise LivePrerequisiteError("Investigator local prompt manifest does not match source")
    if (
        verifier_manifest.prompt_version != verifier_prompt.version
        or verifier_manifest.prompt_sha256 != verifier_prompt.prompt_sha256
    ):
        raise LivePrerequisiteError("Verifier local prompt manifest does not match source")
    if investigator_manifest.model_deployment_name != foundry.model_deployment_name:
        raise LivePrerequisiteError(
            "Investigator local model deployment does not match endpoint config"
        )
    if verifier_manifest.model_deployment_name != foundry.model_deployment_name:
        raise LivePrerequisiteError(
            "Verifier local model deployment does not match endpoint config"
        )
    if investigator_manifest.tool_schema_sha256 not in (None, INVESTIGATOR_TOOL_SCHEMA_SHA256):
        raise LivePrerequisiteError("Investigator local tool schema hash does not match")
    if verifier_manifest.tool_schema_sha256 != VERIFIER_TOOL_SCHEMA_SHA256:
        raise LivePrerequisiteError("Verifier local tool schema hash does not match")
    return LivePrerequisites(
        foundry=foundry,
        investigator_prompt=investigator_prompt,
        verifier_prompt=verifier_prompt,
        investigator_manifest=investigator_manifest,
        verifier_manifest=verifier_manifest,
    )


def _schema_sha256(schema: Mapping[str, object]) -> str:
    encoded = json.dumps(
        schema,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    import hashlib

    return hashlib.sha256(encoded).hexdigest()


def validate_exact_live_pins(
    *,
    project_root: Path,
    model_deployment_name: str,
    investigator_manifest: Mapping[str, object],
    verifier_manifest: Mapping[str, object],
    investigator_record: AgentVersionRecord,
    verifier_record: VerifierAgentVersionRecord,
) -> LiveAgentPins:
    """Cross-check local manifests, retrieved provider records, and schema hashes."""

    try:
        local_investigator = InvestigatorDeploymentManifest.model_validate(investigator_manifest)
        local_verifier = VerifierDeploymentManifest.model_validate(verifier_manifest)
    except ValidationError as error:
        raise LivePrerequisiteError("a live deployment manifest is invalid") from error
    if local_investigator.agent_version != INVESTIGATOR_VERSION:
        raise LivePrerequisiteError("retrieved Investigator version is not the exact local pin")
    current_investigator_prompt = load_prompt_manifest(project_root=project_root)
    current_verifier_prompt = load_verifier_prompt_manifest(project_root=project_root)
    if local_investigator.prompt_sha256 != current_investigator_prompt.prompt_sha256:
        raise LivePrerequisiteError("Investigator prompt hash differs from committed prompt")
    if (
        local_verifier.prompt_version != current_verifier_prompt.version
        or local_verifier.prompt_sha256 != current_verifier_prompt.prompt_sha256
    ):
        raise LivePrerequisiteError("Verifier prompt hash differs from committed prompt")
    if local_investigator.tool_schema_sha256 not in (None, INVESTIGATOR_TOOL_SCHEMA_SHA256):
        raise LivePrerequisiteError("Investigator tool schema hash differs from pinned schema")
    if local_verifier.tool_schema_sha256 != VERIFIER_TOOL_SCHEMA_SHA256:
        raise LivePrerequisiteError("Verifier tool schema hash differs from pinned schema")
    if (
        investigator_record.name != INVESTIGATOR_AGENT_NAME
        or investigator_record.version != INVESTIGATOR_VERSION
        or investigator_record.prompt_version != current_investigator_prompt.version
        or investigator_record.prompt_sha256 != current_investigator_prompt.prompt_sha256
        or investigator_record.model_deployment_name != model_deployment_name
    ):
        raise LivePrerequisiteError("retrieved Investigator version does not match the safe pin")
    if (
        verifier_record.name != VERIFIER_AGENT_NAME
        or verifier_record.version != local_verifier.agent_version
        or verifier_record.prompt_version != current_verifier_prompt.version
        or verifier_record.prompt_sha256 != current_verifier_prompt.prompt_sha256
        or verifier_record.model_deployment_name != model_deployment_name
        or verifier_record.tool_schema_sha256 != VERIFIER_TOOL_SCHEMA_SHA256
    ):
        raise LivePrerequisiteError("retrieved Verifier version does not match the safe pin")
    return LiveAgentPins(
        investigator=LiveAgentPin(
            agent_name=investigator_record.name,
            agent_version=investigator_record.version,
            prompt_version=investigator_record.prompt_version,
            prompt_sha256=investigator_record.prompt_sha256,
            tool_schema_sha256=INVESTIGATOR_TOOL_SCHEMA_SHA256,
            response_schema_sha256=_schema_sha256(build_investigation_result_json_schema()),
            model_deployment_name=investigator_record.model_deployment_name,
        ),
        verifier=LiveAgentPin(
            agent_name=verifier_record.name,
            agent_version=verifier_record.version,
            prompt_version=verifier_record.prompt_version,
            prompt_sha256=verifier_record.prompt_sha256,
            tool_schema_sha256=verifier_record.tool_schema_sha256,
            response_schema_sha256=_schema_sha256(build_verification_result_json_schema()),
            model_deployment_name=verifier_record.model_deployment_name,
        ),
    )


def build_live_runtime(
    project_root: Path,
    *,
    environment: Mapping[str, str] | None = None,
    credential_factory: Callable[[], object] | None = None,
    project_factory: Callable[..., object] | None = None,
    registry_factory: Callable[..., Any] | None = None,
    responses_factory: Callable[..., object] | None = None,
    diagnostic_path: Path | None = None,
) -> LiveRuntime:
    """Preflight locally, then retrieve exact versions without creating or mutating agents."""

    prerequisites = run_live_step(
        "local_preflight",
        lambda: load_live_prerequisites(project_root, environment=environment),
        diagnostic_path=diagnostic_path,
    )
    credential_builder = credential_factory or (
        lambda: DefaultAzureCredential(exclude_interactive_browser_credential=True)
    )
    project_builder = project_factory or AIProjectClient
    registry_builder = registry_factory or PromptAgentRegistry
    response_builder = responses_factory or AzureFoundryResponsesClient
    credential = cast(
        TokenCredential,
        run_live_step("credential", credential_builder, diagnostic_path=diagnostic_path),
    )
    project = cast(
        Any,
        run_live_step(
            "project_client",
            lambda: project_builder(endpoint=prerequisites.foundry.endpoint, credential=credential),
            diagnostic_path=diagnostic_path,
        ),
    )
    try:
        registry = run_live_step(
            "registry_client",
            lambda: registry_builder(project.agents, project_root=project_root),
            diagnostic_path=diagnostic_path,
        )
        investigator_record = run_live_step(
            "retrieve_investigator_v14",
            lambda: registry.retrieve_investigator_version(
                model_deployment_name=prerequisites.foundry.model_deployment_name,
                manifest=prerequisites.investigator_prompt,
                expected_version=INVESTIGATOR_VERSION,
                verify_definition=True,
            ),
            diagnostic_path=diagnostic_path,
        )
        verifier_record = run_live_step(
            f"retrieve_verifier_{prerequisites.verifier_manifest.agent_version}",
            lambda: registry.retrieve_verifier_version(
                model_deployment_name=prerequisites.foundry.model_deployment_name,
                manifest=prerequisites.verifier_prompt,
                expected_version=prerequisites.verifier_manifest.agent_version,
                creation_timestamp=prerequisites.verifier_manifest.created_at,
            ),
            diagnostic_path=diagnostic_path,
        )
        pins = run_live_step(
            "validate_exact_pins",
            lambda: validate_exact_live_pins(
                project_root=project_root,
                model_deployment_name=prerequisites.foundry.model_deployment_name,
                investigator_manifest=prerequisites.investigator_manifest.model_dump(mode="python"),
                verifier_manifest=prerequisites.verifier_manifest.model_dump(mode="python"),
                investigator_record=investigator_record,
                verifier_record=verifier_record,
            ),
            diagnostic_path=diagnostic_path,
        )
        store = EvidenceStore(project_root=project_root)
        policy = run_live_step(
            "policy_read",
            lambda: VerifierTools(store=store).get_policy(
                GetPolicyRequest(policy_ref=PolicyRef.INFINEQ_V1)
            ),
            diagnostic_path=diagnostic_path,
        )
        investigator_client = run_live_step(
            "investigator_response_client",
            lambda: response_builder(
                project,
                agent_name=investigator_record.name,
                agent_version=investigator_record.version,
            ),
            diagnostic_path=diagnostic_path,
        )
        verifier_client = run_live_step(
            "verifier_response_client",
            lambda: response_builder(
                project,
                agent_name=verifier_record.name,
                agent_version=verifier_record.version,
            ),
            diagnostic_path=diagnostic_path,
        )
        investigator = run_live_step(
            "investigator_adapter",
            lambda: Investigator(
                client=investigator_client,
                tools=InvestigatorTools(store=store),
                project_root=project_root,
            ),
            diagnostic_path=diagnostic_path,
        )
        verifier = run_live_step(
            "verifier_adapter",
            lambda: Verifier(
                client=verifier_client,
                tools=VerifierTools(store=store),
                project_root=project_root,
            ),
            diagnostic_path=diagnostic_path,
        )
        return LiveRuntime(
            project=project,
            store=store,
            investigator=investigator,
            verifier=verifier,
            policy=policy,
            pins=pins,
        )
    except Exception:
        close = getattr(project, "close", None)
        if callable(close):
            close()
        raise


def baseline_episode_from_packet(
    packet: IncidentPacketV1,
    *,
    data_version: str = "corpus-v1",
) -> BaselineEpisode:
    """Create the same evaluator-visible evidence set for all three modes."""

    if not isinstance(packet, IncidentPacketV1):
        raise TypeError("live baseline requires IncidentPacketV1")
    signal_kinds = {signal.evidence.evidence_id: signal.signal for signal in packet.signals}
    deployment_ids = set(packet.deployment.evidence_ids)
    visible = tuple(
        VisibleEvidence(
            evidence_id=reference.evidence_id,
            kind=(
                signal_kinds.get(reference.evidence_id) or "replicas"
                if reference.evidence_id in deployment_ids
                else signal_kinds.get(reference.evidence_id, "unknown")
            ),
        )
        for reference in packet.evidence_refs
    )
    if any(item.kind == "unknown" for item in visible):
        raise LivePrerequisiteError("canonical packet contains an unmapped visible evidence kind")
    return BaselineEpisode(
        episode_id=packet.incident_id,
        data_version=data_version,
        visible_evidence=visible,
        detector_status=DetectorStatus.INCIDENT,
        runbook_queries=(RunbookQueryEnum.CAPACITY_QUEUEING,),
        runbook_chunk_ids=("runbook-capacity-queueing",),
    )


def baseline_episode_from_detection(
    detection: DetectionRun,
    *,
    data_version: str = "corpus-v1",
) -> BaselineEpisode:
    """Build public baseline metadata from detector output without opening an oracle."""

    if not isinstance(detection, DetectionRun):
        raise TypeError("live baseline requires DetectionRun")
    if detection.packet is not None:
        return baseline_episode_from_packet(detection.packet, data_version=data_version)
    if detection.decision == "no_incident":
        status = DetectorStatus.NO_INCIDENT
    elif detection.decision == "abstain":
        status = DetectorStatus.ABSTAIN
    else:
        raise LivePrerequisiteError("detector returned an unsupported packetless disposition")
    return BaselineEpisode(
        episode_id=detection.episode_id,
        data_version=data_version,
        visible_evidence=(),
        detector_status=status,
        runbook_queries=(),
        runbook_chunk_ids=(),
    )


def _diagnosis(result: InvestigationResultV1) -> str | None:
    if result.disposition is not Disposition.DIAGNOSED:
        return None
    leading = next(
        (
            hypothesis
            for hypothesis in result.hypotheses
            if hypothesis.hypothesis_id == result.leading_hypothesis_id
        ),
        None,
    )
    return None if leading is None else leading.family.value


def _mode_status(
    *,
    result: InvestigationResultV1,
    workflow_state: WorkflowState | None = None,
    failure: WorkflowFailure | None = None,
) -> BaselineTerminalStatus:
    if failure is WorkflowFailure.TIMEOUT:
        return BaselineTerminalStatus.TIMEOUT
    if failure is WorkflowFailure.INVALID_SCHEMA:
        return BaselineTerminalStatus.INVALID_SCHEMA
    if failure is not None:
        return BaselineTerminalStatus.BLOCKED
    if workflow_state is not None and workflow_state is WorkflowState.AWAITING_HUMAN:
        return BaselineTerminalStatus.COMPLETE
    if result.disposition is Disposition.ANALYSIS_INCOMPLETE:
        return BaselineTerminalStatus.INVALID_SCHEMA
    if result.disposition is not Disposition.DIAGNOSED:
        return BaselineTerminalStatus.ABSTAINED
    return BaselineTerminalStatus.COMPLETE


def _usage_total(usage: tuple[int | None, int | None, int | None]) -> int | None:
    return usage[2]


def _unsupported_claim_count(verification: object) -> int:
    """Count explicitly failed grounded claim checks from a typed Verifier result."""

    if not isinstance(verification, VerificationResultV1):
        return 0
    return sum(not check.supported for check in verification.claim_checks)


def _action_policy_error_count(
    investigation: InvestigationResultV1,
    *,
    policy: PolicyResponse,
    packet: IncidentPacketV1,
) -> int:
    """Count an invalid proposed action identically for either agent mode."""

    if investigation.proposed_action is None:
        return 0
    try:
        decision = evaluate_action_policy(
            investigation=investigation,
            policy=policy,
            packet=packet,
        )
    except Exception:
        return 1
    return int(not decision.allowed)


def _safe_trace_refs(
    mode: BaselineMode,
    episode_id: str,
    response_ids: Sequence[str],
) -> tuple[str, ...]:
    references = tuple(
        re.sub(r"^(trace|resp|call)_", r"\1-", value)
        for value in response_ids
        if re.fullmatch(r"^(?:trace|resp|call)[-_][A-Za-z0-9_-]{1,96}$", value)
    )
    return references or (f"trace-live-{mode.value.replace('_', '-')}-{episode_id}",)


def _record_for_mode(
    *,
    mode: BaselineMode,
    episode: BaselineEpisode,
    budget: ResourceBudget,
    captured: _CapturedAgentRun,
    investigation: InvestigationResultV1,
    run_id: str,
    workflow_state: WorkflowState | None = None,
    failure: WorkflowFailure | None = None,
    action_card_eligible: bool = False,
    unsupported_claims: int = 0,
    policy_errors: int = 0,
    agent_version: str,
    prompt_version: str,
    tool_schema_version: str,
) -> BaselineRunRecord:
    result = investigation
    cited = tuple(result.cited_evidence_ids)
    unseen = len(set(cited) - set(episode.visible_evidence_ids))
    terminal = _mode_status(result=result, workflow_state=workflow_state, failure=failure)
    abstained = result.disposition is not Disposition.DIAGNOSED
    input_tokens, output_tokens, total_tokens = captured.usage
    return BaselineRunRecord(
        mode=mode,
        episode_id=episode.episode_id,
        versions=BaselineVersions(
            data_version=episode.data_version,
            agent_version=agent_version,
            prompt_version=prompt_version,
            tool_schema_version=tool_schema_version,
            workflow_version=LIVE_WORKFLOW_VERSION,
        ),
        budget=budget,
        visible_evidence_ids=episode.visible_evidence_ids,
        terminal_status=terminal,
        diagnosis=_diagnosis(result),
        abstained=abstained,
        action_executed=False,
        action_card_eligible=action_card_eligible,
        cited_evidence_ids=cited,
        unsupported_claims=unsupported_claims,
        policy_errors=policy_errors,
        unseen_evidence=unseen,
        forbidden_tool_calls=captured.forbidden_tool_calls,
        successful_tool_calls=captured.successful_tool_calls,
        redundant_tool_calls=0,
        latency_ms=captured.latency_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens if total_tokens is not None else _usage_total(captured.usage),
        run_id=run_id,
        trace_refs=_safe_trace_refs(mode, episode.episode_id, captured.response_ids),
    )


def _safe_json_text(text: str) -> str:
    try:
        value = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return redact_text(text)

    def clean(item: object) -> object:
        if isinstance(item, Mapping):
            result: dict[str, object] = {}
            for key, child in item.items():
                lowered = str(key).casefold()
                if lowered in {
                    "prompt",
                    "completion",
                    "endpoint",
                    "credential",
                    "api_key",
                    "authorization",
                    "client_secret",
                    "connection_string",
                    "password",
                    "token",
                    "secret",
                }:
                    continue
                result[str(key)] = clean(child)
            return redact_mapping(result)
        if isinstance(item, list):
            return [clean(child) for child in item]
        if isinstance(item, str):
            return redact_text(item)
        return item

    return json.dumps(clean(value), ensure_ascii=True, sort_keys=True, indent=2) + "\n"


def _safe_trace_payload(records: Sequence[Mapping[str, object]]) -> str:
    return (
        json.dumps(
            [redact_mapping(dict(record)) for record in records],
            ensure_ascii=True,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )


def _safe_json_value(text: str) -> object:
    """Return a JSON-safe redacted value even when a call failed before output."""

    safe_text = _safe_json_text(text)
    if not safe_text:
        return {"output": ""}
    try:
        return json.loads(safe_text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"output": safe_text}


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _mode_metadata(
    *,
    mode: BaselineMode,
    captured: _CapturedAgentRun,
    result: BaselineRunRecord,
    raw_path: str,
    redacted_path: str,
    trace_path: str,
    agent_invoked: bool | None = None,
) -> dict[str, object]:
    return {
        "mode": mode.value,
        "raw_output_path": raw_path,
        "redacted_output_path": redacted_path,
        "trace_path": trace_path,
        "response_ids": captured.response_ids,
        "agent_invoked": (
            bool(captured.response_ids or captured.error is not None)
            if agent_invoked is None
            else agent_invoked
        ),
        "successful_tool_calls": captured.successful_tool_calls,
        "forbidden_tool_calls": captured.forbidden_tool_calls,
        "tool_retry_count": captured.tool_retry_count,
        "repair_attempted": captured.repair_attempted,
        "latency_ms": captured.latency_ms,
        "input_tokens": captured.usage[0],
        "output_tokens": captured.usage[1],
        "total_tokens": captured.usage[2],
        "returned_evidence_count": len(captured.returned_evidence_ids),
        "visible_evidence_count": len(result.visible_evidence_ids),
        "terminal_status": result.terminal_status.value,
        "action_executed": False,
        "error": (
            None if captured.error is None else _sanitize_live_diagnostic_message(captured.error)
        ),
    }


def _persist_live_artifacts(
    *,
    project_root: Path,
    run_id: str,
    episode: BaselineEpisode,
    runs: Sequence[BaselineRunRecord],
    static_brief: Mapping[str, object],
    initial_investigator_capture: _CapturedAgentRun,
    investigator_capture: _CapturedAgentRun,
    verifier_capture: _CapturedAgentRun | None,
    investigator_invocation_count: int,
    verifier_invocation_count: int,
    verifier_not_invoked_reason: str,
    workflow: object,
    pins: LiveAgentPins | None,
    scored: BaselineEvaluation | None,
    output_directory: Path | None = None,
    nested: bool = False,
    scope: str = "single_episode",
    failure_artifact: Mapping[str, object] | None = None,
    revision_capture: _CapturedAgentRun | None = None,
    first_verifier_capture: _CapturedAgentRun | None = None,
) -> Path:
    output = (
        output_directory.resolve()
        if output_directory is not None
        else baseline_run_directory(project_root, run_id)
    )
    if output.exists():
        raise LivePrerequisiteError("live run ID already exists; choose a unique safe run ID")
    if nested and (output.name != episode.episode_id or output.parent.name != "episodes"):
        raise LivePrerequisiteError("nested live artifacts are not episode-contained")
    correction_invocation_count = max(0, investigator_invocation_count - 1)
    if (correction_invocation_count > 0) != (revision_capture is not None):
        raise LivePrerequisiteError("live revision capture does not match invocation count")
    correction_packet = getattr(getattr(workflow, "context", None), "correction_packet", None)
    if correction_invocation_count and (
        first_verifier_capture is None
        or not isinstance(first_verifier_capture.result, VerificationResultV1)
        or not isinstance(correction_packet, CorrectionPacketV1)
    ):
        raise LivePrerequisiteError("live correction decision or typed packet is missing")
    output.mkdir(parents=True, exist_ok=False)
    _write_json(output / "static" / "brief.json", static_brief)
    _write_json(
        output / "investigator" / "raw_output.json",
        _safe_json_value(initial_investigator_capture.raw_output),
    )
    (output / "investigator" / "redacted_output.json").write_text(
        _safe_json_text(initial_investigator_capture.result.model_dump_json()),
        encoding="utf-8",
    )
    (output / "investigator" / "trace.json").write_text(
        _safe_trace_payload(initial_investigator_capture.trace_records),
        encoding="utf-8",
    )
    if verifier_capture is None:
        _write_json(
            output / "verifier" / "not_invoked.json",
            {"invoked": False, "reason": verifier_not_invoked_reason},
        )
    else:
        _write_json(
            output / "verifier" / "raw_output.json",
            _safe_json_value(verifier_capture.raw_output),
        )
        (output / "verifier" / "redacted_output.json").write_text(
            _safe_json_text(verifier_capture.result.model_dump_json()),
            encoding="utf-8",
        )
        (output / "verifier" / "trace.json").write_text(
            _safe_trace_payload(verifier_capture.trace_records),
            encoding="utf-8",
        )
    _write_json(
        output / "investigator_verifier" / "reused_investigator.json",
        {
            "source_mode": BaselineMode.INVESTIGATOR.value,
            "same_investigator_result": True,
            "same_visible_evidence": True,
            "same_initial_response_ids": initial_investigator_capture.response_ids,
        },
    )
    if revision_capture is not None:
        (output / "investigator_verifier" / "revision_result.json").write_text(
            _safe_json_text(revision_capture.result.model_dump_json()),
            encoding="utf-8",
        )
        assert first_verifier_capture is not None
        assert isinstance(correction_packet, CorrectionPacketV1)
        (output / "investigator_verifier" / "initial_verification.json").write_text(
            _safe_json_text(first_verifier_capture.result.model_dump_json()),
            encoding="utf-8",
        )
        (output / "investigator_verifier" / "correction_packet.json").write_text(
            _safe_json_text(correction_packet.model_dump_json()),
            encoding="utf-8",
        )
    _write_json(
        output / "investigator_verifier" / "accounting.json",
        {
            "investigator_invocation_count": investigator_invocation_count,
            "agent_invoked": investigator_invocation_count > 0,
            "correction_invocation_count": correction_invocation_count,
            "verifier_invocation_count": verifier_invocation_count,
            "initial_investigator_response_ids": initial_investigator_capture.response_ids,
            "all_investigator_response_ids": investigator_capture.response_ids,
            "correction_response_ids": investigator_capture.response_ids[
                len(initial_investigator_capture.response_ids) :
            ],
            "initial_investigator_successful_tool_calls": (
                initial_investigator_capture.successful_tool_calls
            ),
            "correction_successful_tool_calls": max(
                0,
                investigator_capture.successful_tool_calls
                - initial_investigator_capture.successful_tool_calls,
            ),
            "verifier_response_ids": (
                () if verifier_capture is None else verifier_capture.response_ids
            ),
            "shared_initial_investigator_result": investigator_invocation_count > 0,
            "shared_initial_visible_evidence": True,
            "action_execution_count": 0,
            "oracle_loaded_after_agent_outputs": False,
        },
    )
    workflow_audit = getattr(workflow, "audit_events", ())
    _write_json(
        output / "workflow_audit.json",
        [
            redact_mapping(
                {
                    "event_id": event.event_id,
                    "workflow_id": event.workflow_id,
                    "from_state": event.from_state.value,
                    "to_state": event.to_state.value,
                    "actor": event.actor.value,
                    "reason": event.reason,
                    "occurred_at": event.occurred_at.isoformat(),
                    "revision_count": event.revision_count,
                }
            )
            for event in workflow_audit
        ],
    )
    if failure_artifact is not None:
        _write_json(output / "failure.json", redact_mapping(dict(failure_artifact)))
    _write_json(
        output / "baseline_results.json",
        {
            "run_id": run_id,
            "runs": [
                redact_mapping(run.model_dump(mode="json"))
                for run in sorted(
                    runs,
                    key=lambda item: (
                        item.episode_id,
                        {
                            BaselineMode.STATIC: 0,
                            BaselineMode.INVESTIGATOR: 1,
                            BaselineMode.INVESTIGATOR_VERIFIER: 2,
                        }[item.mode],
                    ),
                )
            ],
        },
    )
    verifier_mode_result = next(
        item for item in runs if item.mode is BaselineMode.INVESTIGATOR_VERIFIER
    )
    if verifier_capture is None:
        verifier_mode: dict[str, object] = {
            "mode": BaselineMode.INVESTIGATOR_VERIFIER.value,
            "verifier_invoked": False,
            "not_invoked_path": "verifier/not_invoked.json",
            "response_ids": (),
            "successful_tool_calls": 0,
            "forbidden_tool_calls": 0,
            "tool_retry_count": 0,
            "repair_attempted": False,
            "latency_ms": 0.0,
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "returned_evidence_count": 0,
            "visible_evidence_count": len(verifier_mode_result.visible_evidence_ids),
            "terminal_status": verifier_mode_result.terminal_status.value,
            "action_executed": False,
            "error": None,
            "investigator_result_reused": True,
            "reused_investigator_path": "investigator_verifier/reused_investigator.json",
        }
    else:
        verifier_mode = {
            **_mode_metadata(
                mode=BaselineMode.INVESTIGATOR_VERIFIER,
                captured=verifier_capture,
                result=verifier_mode_result,
                raw_path="verifier/raw_output.json",
                redacted_path="verifier/redacted_output.json",
                trace_path="verifier/trace.json",
                agent_invoked=verifier_invocation_count > 0,
            ),
            "verifier_invoked": True,
            "investigator_result_reused": True,
            "reused_investigator_path": "investigator_verifier/reused_investigator.json",
        }
    if revision_capture is not None:
        verifier_mode["revision_result_path"] = "investigator_verifier/revision_result.json"
        verifier_mode["initial_verification_path"] = (
            "investigator_verifier/initial_verification.json"
        )
        verifier_mode["correction_packet_path"] = "investigator_verifier/correction_packet.json"
    manifest: dict[str, object] = {
        "schema_version": "1.1",
        "scope": scope,
        "run_id": run_id,
        "episode_id": episode.episode_id,
        "data_version": episode.data_version,
        "workflow_version": LIVE_WORKFLOW_VERSION,
        "status": "scored" if scored is not None else "awaiting_score",
        "baseline_results_path": "baseline_results.json",
        "workflow_audit_path": "workflow_audit.json",
        "modes": {
            "static": {
                "brief_path": "static/brief.json",
                "action_executed": False,
            },
            "investigator": _mode_metadata(
                mode=BaselineMode.INVESTIGATOR,
                captured=initial_investigator_capture,
                result=next(item for item in runs if item.mode is BaselineMode.INVESTIGATOR),
                raw_path="investigator/raw_output.json",
                redacted_path="investigator/redacted_output.json",
                trace_path="investigator/trace.json",
                agent_invoked=investigator_invocation_count > 0,
            ),
            "investigator_verifier": verifier_mode,
        },
    }
    if pins is not None:
        manifest["agent_pins"] = pins.model_dump(mode="json")
    if scored is not None:
        _write_json(output / "evaluation.json", scored.model_dump(mode="json"))
        manifest["evaluation_path"] = "evaluation.json"
    _write_json(output / "live_manifest.json", redact_mapping(manifest))
    return output


def _assert_relative_artifact(output: Path, relative: object) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise LiveArtifactError("artifact path must be relative")
    candidate = (output / relative).resolve()
    if not candidate.is_relative_to(output.resolve()):
        raise LiveArtifactError("artifact path escaped the live run directory")
    if not candidate.is_file():
        raise LiveArtifactError("declared live artifact is missing")
    return candidate


def _validate_saved_pins(
    payload: object,
    *,
    project_root: Path | None,
) -> LiveAgentPins:
    try:
        pins = LiveAgentPins.model_validate(payload)
    except ValidationError as error:
        raise LiveArtifactError("saved live agent pins are invalid") from error
    investigator = pins.investigator
    verifier = pins.verifier
    if (
        investigator.agent_name != INVESTIGATOR_AGENT_NAME
        or investigator.agent_version != INVESTIGATOR_VERSION
        or investigator.prompt_version != "investigator-v14"
        or investigator.tool_schema_sha256 != INVESTIGATOR_TOOL_SCHEMA_SHA256
        or investigator.response_schema_sha256
        != _schema_sha256(build_investigation_result_json_schema())
        or verifier.agent_name != VERIFIER_AGENT_NAME
        or verifier.prompt_version not in {"verifier-v1", VERIFIER_PROMPT_VERSION}
        or verifier.tool_schema_sha256 != VERIFIER_TOOL_SCHEMA_SHA256
        or verifier.response_schema_sha256
        != _schema_sha256(build_verification_result_json_schema())
        or investigator.model_deployment_name != verifier.model_deployment_name
    ):
        raise LiveArtifactError("saved live agent pins do not match the exact Task 7b1 pins")
    if project_root is not None:
        try:
            investigator_prompt = load_prompt_manifest(project_root=project_root)
            verifier_prompt = load_verifier_prompt_manifest(project_root=project_root)
            investigator_manifest = _load_investigator_deployment_manifest(project_root)
            verifier_manifest = load_verifier_deployment_manifest(project_root)
        except (FileNotFoundError, OSError, ValidationError, ValueError) as error:
            raise LiveArtifactError("local live pin sources are unreadable") from error
        if (
            investigator.prompt_sha256 != investigator_prompt.prompt_sha256
            or verifier.prompt_version != verifier_prompt.version
            or verifier.prompt_sha256 != verifier_prompt.prompt_sha256
            or verifier.agent_version != verifier_manifest.agent_version
            or investigator.model_deployment_name != investigator_manifest.model_deployment_name
            or verifier.model_deployment_name != verifier_manifest.model_deployment_name
        ):
            raise LiveArtifactError("saved live prompt or model pins differ from local manifests")
    return pins


def _read_artifact_json(path: Path, *, description: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LiveArtifactError(f"{description} is unreadable") from error


def _as_list(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []


def _validate_trace_linkage(
    output: Path,
    metadata: Mapping[str, object],
    *,
    record: BaselineRunRecord,
) -> None:
    response_ids = metadata.get("response_ids")
    if not isinstance(response_ids, list):
        response_ids = list(response_ids) if isinstance(response_ids, tuple) else []
    if not all(
        isinstance(value, str)
        and re.fullmatch(r"^(?:trace|resp|call)[-_][A-Za-z0-9_-]{1,96}$", value)
        for value in response_ids
    ):
        raise LiveArtifactError("live response references are malformed")
    trace_path = _assert_relative_artifact(output, metadata.get("trace_path"))
    traces = _read_artifact_json(trace_path, description="live trace artifact")
    if not isinstance(traces, list):
        raise LiveArtifactError("live trace artifact must be a list")
    response_set = set(response_ids)
    for trace in traces:
        if not isinstance(trace, dict):
            raise LiveArtifactError("live trace entry is invalid")
        response_id = trace.get("response_id")
        call_id = trace.get("call_id")
        if (
            not isinstance(response_id, str)
            or response_id not in response_set
            or not isinstance(call_id, str)
            or re.fullmatch(r"^(?:trace|resp|call)[-_][A-Za-z0-9_-]{1,96}$", call_id) is None
        ):
            raise LiveArtifactError("live response and call references are not linked")
    successful = metadata.get("successful_tool_calls")
    if not isinstance(successful, int) or successful < 0 or successful != len(traces):
        raise LiveArtifactError("live tool accounting does not match its trace artifact")
    if metadata.get("forbidden_tool_calls") != 0 or record.forbidden_tool_calls != 0:
        raise LiveArtifactError("live artifact records a forbidden tool call")
    if record.unseen_evidence != 0:
        raise LiveArtifactError("live artifact records unseen evidence")


def _validate_saved_accounting(
    output: Path,
    investigator_mode: Mapping[str, object],
    verifier_mode: Mapping[str, object],
    *,
    require_evaluation: bool,
    artifact_schema_version: str = "1.0",
    episode_id: str | None = None,
) -> None:
    accounting_path = _assert_relative_artifact(
        output,
        "investigator_verifier/accounting.json",
    )
    accounting = _read_artifact_json(accounting_path, description="live accounting artifact")
    if not isinstance(accounting, dict):
        raise LiveArtifactError("live accounting artifact must be an object")
    integer_fields = (
        "investigator_invocation_count",
        "correction_invocation_count",
        "verifier_invocation_count",
        "initial_investigator_successful_tool_calls",
        "correction_successful_tool_calls",
        "action_execution_count",
    )
    counts: dict[str, int] = {}
    for field_name in integer_fields:
        value = accounting.get(field_name)
        if not isinstance(value, int) or value < 0:
            raise LiveArtifactError("live accounting counts are invalid")
        counts[field_name] = value
    investigator_count = counts["investigator_invocation_count"]
    if investigator_count == 0:
        if (
            counts["correction_invocation_count"] != 0
            or counts["verifier_invocation_count"] != 0
            or accounting.get("agent_invoked") is not False
        ):
            raise LiveArtifactError("packetless live accounting is inconsistent")
        if accounting.get("shared_initial_investigator_result") is not False:
            raise LiveArtifactError("packetless artifact claims an Investigator result")
        if accounting.get("shared_initial_visible_evidence") is not True:
            raise LiveArtifactError("packetless visible evidence marker is missing")
    else:
        if (
            accounting.get("agent_invoked") is not True
            or counts["correction_invocation_count"] != investigator_count - 1
        ):
            raise LiveArtifactError("correction accounting is inconsistent")
        if accounting.get("shared_initial_investigator_result") is not True:
            raise LiveArtifactError("the initial Investigator result was not marked as shared")
        if accounting.get("shared_initial_visible_evidence") is not True:
            raise LiveArtifactError("the initial visible evidence set was not marked as shared")
    if counts["action_execution_count"] != 0:
        raise LiveArtifactError("live artifact records action execution")
    if require_evaluation and accounting.get("oracle_loaded_after_agent_outputs") is not True:
        raise LiveArtifactError("oracle ordering marker is missing")
    all_response_ids = accounting.get("all_investigator_response_ids")
    initial_response_ids = accounting.get("initial_investigator_response_ids")
    correction_response_ids = accounting.get("correction_response_ids")
    if not (
        isinstance(all_response_ids, list)
        and isinstance(initial_response_ids, list)
        and isinstance(correction_response_ids, list)
    ):
        raise LiveArtifactError("live Investigator response accounting is invalid")
    if all_response_ids[: len(initial_response_ids)] != initial_response_ids:
        raise LiveArtifactError("live Investigator response accounting lost the initial result")
    if all_response_ids[len(initial_response_ids) :] != correction_response_ids:
        raise LiveArtifactError("live correction response accounting is inconsistent")
    # The one-agent mode contains only the shared initial invocation; correction
    # responses belong to the two-agent workflow and are linked above via accounting.
    if _as_list(investigator_mode.get("response_ids", ())) != initial_response_ids:
        raise LiveArtifactError(
            "Investigator response accounting is not linked to the mode artifact"
        )
    verifier_response_ids = accounting.get("verifier_response_ids")
    if not isinstance(verifier_response_ids, list):
        raise LiveArtifactError("live Verifier response accounting is invalid")
    if _as_list(verifier_mode.get("response_ids", ())) != verifier_response_ids:
        raise LiveArtifactError("Verifier response accounting is not linked to the mode artifact")
    if artifact_schema_version == "1.1":
        revision_path = verifier_mode.get("revision_result_path")
        if counts["correction_invocation_count"] > 0:
            if revision_path is None:
                raise LiveArtifactError("corrected investigation artifact is missing")
            initial_path = verifier_mode.get("initial_verification_path")
            packet_path = verifier_mode.get("correction_packet_path")
            if initial_path is None or packet_path is None:
                raise LiveArtifactError("initial Verifier or correction packet artifact is missing")
            try:
                first_verification = VerificationResultV1.model_validate(
                    _read_artifact_json(
                        _assert_relative_artifact(output, initial_path),
                        description="initial Verifier artifact",
                    )
                )
                correction = CorrectionPacketV1.model_validate(
                    _read_artifact_json(
                        _assert_relative_artifact(output, packet_path),
                        description="correction packet artifact",
                    )
                )
            except ValidationError as error:
                raise LiveArtifactError(
                    "initial Verifier or correction packet is invalid"
                ) from error
            if (
                first_verification.status is not VerificationStatus.REVISION_REQUIRED
                or first_verification.incident_id != episode_id
                or correction.incident_id != episode_id
                or correction.investigation_id != first_verification.investigation_id
            ):
                raise LiveArtifactError("initial Verifier and correction packet are not linked")
            payload = _read_artifact_json(
                _assert_relative_artifact(output, revision_path),
                description="corrected investigation artifact",
            )
            try:
                revision = InvestigationResultV1.model_validate(payload)
            except ValidationError as error:
                raise LiveArtifactError("corrected investigation artifact is invalid") from error
            if revision.incident_id != episode_id:
                raise LiveArtifactError("corrected investigation episode does not match")
        elif any(
            verifier_mode.get(key) is not None
            for key in (
                "revision_result_path",
                "initial_verification_path",
                "correction_packet_path",
            )
        ):
            raise LiveArtifactError("unrequested corrected investigation artifact is present")


def _assert_relative_directory(output: Path, relative: object) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise LiveArtifactError("artifact directory path must be relative")
    candidate = (output / relative).resolve()
    if not candidate.is_relative_to(output.resolve()) or not candidate.is_dir():
        raise LiveArtifactError("declared live artifact directory is invalid")
    return candidate


def _validate_all_development_artifacts(
    output: Path,
    run_id: str,
    manifest: Mapping[str, object],
    *,
    require_evaluation: bool,
    require_pins: bool,
    project_root: Path | None,
) -> Path:
    if _DEVELOPMENT_LIVE_RUN_ID.fullmatch(run_id) is None:
        raise LiveArtifactError("all-development live run ID is not strict")
    expected_ids = tuple(select_development_episode_ids())
    raw_ids = manifest.get("episode_ids")
    if not isinstance(raw_ids, list) or tuple(raw_ids) != expected_ids:
        raise LiveArtifactError("all-development manifest episode selection is not exact")
    if manifest.get("status") not in {"awaiting_score", "scored"}:
        raise LiveArtifactError("all-development manifest status is invalid")
    if require_pins:
        _validate_saved_pins(manifest.get("agent_pins"), project_root=project_root)
    episodes_root = _assert_relative_directory(output, manifest.get("episodes_path"))
    _assert_relative_artifact(output, manifest.get("evaluation_audit_path"))
    if require_evaluation:
        _assert_relative_artifact(output, manifest.get("evaluation_path"))
        _assert_relative_artifact(output, manifest.get("aggregate_metrics_path"))
        if manifest.get("status") != "scored":
            raise LiveArtifactError("scored all-development manifest is not marked scored")
    child_directories = tuple(
        sorted(path.name for path in episodes_root.iterdir() if path.is_dir())
    )
    if child_directories != tuple(sorted(expected_ids)):
        raise LiveArtifactError("all-development episode directory set is not exact")
    if any(path.is_file() for path in episodes_root.iterdir()):
        raise LiveArtifactError("all-development episode root contains an unexpected file")
    records: list[BaselineRunRecord] = []
    for episode_id in expected_ids:
        episode_output = episodes_root / episode_id
        validate_live_artifacts(
            episode_output,
            run_id,
            require_evaluation=require_evaluation,
            require_pins=require_pins,
            project_root=project_root,
            _nested=True,
            expected_episode_id=episode_id,
        )
        baseline_payload = _read_artifact_json(
            episode_output / "baseline_results.json",
            description="episode baseline results",
        )
        if not isinstance(baseline_payload, dict):
            raise LiveArtifactError("episode baseline results are not an object")
        raw_runs = baseline_payload.get("runs")
        if not isinstance(raw_runs, list) or len(raw_runs) != len(_MODE_INDEX):
            raise LiveArtifactError("episode baseline results do not contain three records")
        try:
            records.extend(BaselineRunRecord.model_validate(item) for item in raw_runs)
        except ValidationError as error:
            raise LiveArtifactError("episode baseline results contain an invalid record") from error
    if len(records) != 24:
        raise LiveArtifactError("all-development artifacts do not contain exactly 24 records")
    keys = {(record.episode_id, record.mode) for record in records}
    expected_keys = {(episode_id, mode) for episode_id in expected_ids for mode in _MODE_INDEX}
    if keys != expected_keys:
        raise LiveArtifactError("all-development mode records are not an exact matrix")
    audit = _read_artifact_json(
        _assert_relative_artifact(output, manifest.get("evaluation_audit_path")),
        description="development evaluation audit",
    )
    if not isinstance(audit, dict):
        raise LiveArtifactError("development evaluation audit is invalid")
    if (
        audit.get("all_episode_artifacts_validated") is not True
        or audit.get("validated_episode_count") != 8
        or audit.get("validated_mode_record_count") != 24
        or audit.get("held_out_access") is not False
    ):
        raise LiveArtifactError("development evaluation audit does not prove the safety order")
    if require_evaluation and audit.get("oracle_loaded_after_all_episode_outputs") is not True:
        raise LiveArtifactError("development oracle ordering marker is missing")
    if not require_evaluation and audit.get("oracle_loaded_after_all_episode_outputs") not in {
        False,
        True,
    }:
        raise LiveArtifactError("development oracle ordering marker is invalid")
    if require_evaluation:
        evaluation_payload = _read_artifact_json(
            _assert_relative_artifact(output, manifest.get("evaluation_path")),
            description="development evaluation",
        )
        try:
            evaluation = BaselineEvaluation.model_validate(evaluation_payload)
        except ValidationError as error:
            raise LiveArtifactError("development evaluation artifact is invalid") from error
        if (
            tuple(evaluation.episode_ids) != expected_ids
            or len(evaluation.scores) != 24
            or {(score.episode_id, score.mode) for score in evaluation.scores} != expected_keys
        ):
            raise LiveArtifactError("development evaluation does not cover the exact matrix")
        for mode in _MODE_INDEX:
            mode_records = tuple(record for record in records if record.mode is mode)
            if (
                evaluation.pass_count(mode)
                != sum(score.full_pass for score in evaluation.scores if score.mode is mode)
                or evaluation.unsupported_claims_by_mode.get(mode.value)
                != sum(record.unsupported_claims for record in mode_records)
                or evaluation.policy_errors_by_mode.get(mode.value)
                != sum(record.policy_errors for record in mode_records)
            ):
                raise LiveArtifactError("development evaluation does not match saved mode records")
        one = BaselineMode.INVESTIGATOR.value
        two = BaselineMode.INVESTIGATOR_VERIFIER.value
        improvement = (
            evaluation.unsupported_claims_by_mode[two] < evaluation.unsupported_claims_by_mode[one]
            or evaluation.policy_errors_by_mode[two] < evaluation.policy_errors_by_mode[one]
        )
        canonical_passed = any(
            score.episode_id == CANONICAL_QUEUE_EPISODE_ID
            and score.mode is BaselineMode.INVESTIGATOR_VERIFIER
            and score.full_pass
            for score in evaluation.scores
        ) and any(
            record.episode_id == CANONICAL_QUEUE_EPISODE_ID
            and record.mode is BaselineMode.INVESTIGATOR_VERIFIER
            and record.action_card_eligible
            for record in records
        )
        expected_justification = (
            improvement
            and evaluation.pass_count(BaselineMode.INVESTIGATOR_VERIFIER)
            >= evaluation.pass_count(BaselineMode.INVESTIGATOR)
            and canonical_passed
        )
        if evaluation.two_agent_justification_passed is not expected_justification:
            raise LiveArtifactError("saved two-agent justification gate is stale or inconsistent")
        aggregate = _read_artifact_json(
            _assert_relative_artifact(output, manifest.get("aggregate_metrics_path")),
            description="development aggregate metrics",
        )
        if not isinstance(aggregate, dict):
            raise LiveArtifactError("development aggregate metrics are invalid")
        if (
            aggregate.get("episode_count") != 8
            or aggregate.get("mode_record_count") != 24
            or tuple(aggregate.get("episode_ids", ())) != expected_ids
            or aggregate.get("two_agent_justification_passed")
            is not evaluation.two_agent_justification_passed
        ):
            raise LiveArtifactError("development aggregate metrics are not matched to evaluation")
    for path in output.rglob("*"):
        relative = path.relative_to(output).as_posix().casefold()
        if any(
            marker in relative
            for marker in ("held_out", "held-out", "hidden_oracle", "expected_label")
        ):
            raise LiveArtifactError("all-development artifacts contain a held-out path")
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as error:
                raise LiveArtifactError("all-development artifact is not safe text") from error
            if any(
                fragment in text.casefold()
                for fragment in ('"prompt":', '"completion":', '"endpoint":', "https://", "file://")
            ):
                raise LiveArtifactError(
                    "all-development artifact contains a forbidden body or endpoint"
                )
    return output


def validate_live_artifacts(
    output_directory: Path,
    run_id: str,
    *,
    require_evaluation: bool = True,
    require_pins: bool = False,
    project_root: Path | None = None,
    _nested: bool = False,
    expected_episode_id: str | None = None,
) -> Path:
    """Validate contained paths and redactable content without reading evaluator truth."""

    output = output_directory.resolve()
    if not _nested and (output.name != run_id or output.parent.name != "runs"):
        raise LiveArtifactError("live artifacts are not under the designated runs directory")
    if _nested and output.name != expected_episode_id:
        raise LiveArtifactError("nested live artifacts have the wrong episode directory")
    manifest_path = output / "live_manifest.json"
    if not manifest_path.is_file():
        raise LiveArtifactError("live artifact manifest is missing")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LiveArtifactError("live artifact manifest is unreadable") from error
    if not isinstance(manifest, dict) or manifest.get("run_id") != run_id:
        raise LiveArtifactError("live artifact manifest run ID does not match")
    permitted_schema_versions = (
        {"development-live-v1"}
        if not _nested and manifest.get("scope") == "all_development"
        else {"1.0", "1.1"}
    )
    if manifest.get("schema_version") not in permitted_schema_versions:
        raise LiveArtifactError("live artifact manifest schema version is invalid")
    if expected_episode_id is not None and manifest.get("episode_id") != expected_episode_id:
        raise LiveArtifactError("nested live manifest episode ID does not match its directory")
    if not _nested and manifest.get("scope") == "all_development":
        return _validate_all_development_artifacts(
            output,
            run_id,
            manifest,
            require_evaluation=require_evaluation,
            require_pins=require_pins,
            project_root=project_root,
        )
    saved_pins = manifest.get("agent_pins")
    pins = (
        _validate_saved_pins(saved_pins, project_root=project_root)
        if saved_pins is not None or require_pins
        else None
    )
    for required in (
        "baseline_results_path",
        "workflow_audit_path",
    ):
        _assert_relative_artifact(output, manifest.get(required))
    modes = manifest.get("modes")
    if not isinstance(modes, dict) or set(modes) != {
        "static",
        "investigator",
        "investigator_verifier",
    }:
        raise LiveArtifactError("live artifact manifest does not contain all three modes")
    static_mode = modes["static"]
    if not isinstance(static_mode, dict):
        raise LiveArtifactError("static mode artifact metadata is invalid")
    _assert_relative_artifact(output, static_mode.get("brief_path"))
    investigator_mode = modes["investigator"]
    verifier_mode = modes["investigator_verifier"]
    if not isinstance(investigator_mode, dict) or not isinstance(verifier_mode, dict):
        raise LiveArtifactError("live mode artifact metadata is invalid")
    for key in ("raw_output_path", "redacted_output_path", "trace_path"):
        _assert_relative_artifact(output, investigator_mode.get(key))
    if verifier_mode.get("verifier_invoked", True):
        for key in ("raw_output_path", "redacted_output_path", "trace_path"):
            _assert_relative_artifact(output, verifier_mode.get(key))
    else:
        _assert_relative_artifact(output, verifier_mode.get("not_invoked_path"))
    if require_evaluation:
        _assert_relative_artifact(output, manifest.get("evaluation_path"))
    if any(
        mode.get("action_executed") is not False
        for mode in (static_mode, investigator_mode, verifier_mode)
    ):
        raise LiveArtifactError("live mode metadata records action execution")
    workflow_audit = _read_artifact_json(
        _assert_relative_artifact(output, manifest.get("workflow_audit_path")),
        description="workflow audit artifact",
    )
    if not isinstance(workflow_audit, list):
        raise LiveArtifactError("workflow audit artifact must be a list")
    for event in workflow_audit:
        if not isinstance(event, dict):
            raise LiveArtifactError("workflow audit entry is invalid")
        actor = event.get("actor")
        to_state = event.get("to_state")
        if (isinstance(actor, str) and actor.casefold() == "executor") or (
            isinstance(to_state, str) and to_state.casefold() == "action_applied"
        ):
            raise LiveArtifactError("live workflow audit records an executor or action")
    files = tuple(path for path in output.rglob("*") if path.is_file())
    forbidden_fragments = (
        '"prompt":',
        '"completion":',
        '"api_key":',
        '"authorization":',
        '"client_secret":',
        '"connection_string":',
        '"password":',
        '"endpoint":',
        '"credential":',
        '"hidden_oracle"',
        "https://",
        "file://",
    )
    for path in files:
        if not path.resolve().is_relative_to(output):
            raise LiveArtifactError("live artifact escaped the run directory")
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise LiveArtifactError("live artifact is not safe text") from error
        lowered = text.casefold()
        if any(fragment in lowered for fragment in forbidden_fragments):
            raise LiveArtifactError("live artifact contains a forbidden body or endpoint")
    try:
        baseline = json.loads((output / "baseline_results.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LiveArtifactError("baseline results artifact is unreadable") from error
    if not isinstance(baseline, dict) or baseline.get("run_id") != run_id:
        raise LiveArtifactError("baseline results run ID does not match")
    raw_runs = baseline.get("runs")
    if not isinstance(raw_runs, list):
        raise LiveArtifactError("baseline results must contain a run list")
    try:
        records = tuple(BaselineRunRecord.model_validate(item) for item in raw_runs)
    except ValidationError as error:
        raise LiveArtifactError("baseline results contain an invalid strict record") from error
    if {record.mode for record in records} != {
        BaselineMode.STATIC,
        BaselineMode.INVESTIGATOR,
        BaselineMode.INVESTIGATOR_VERIFIER,
    }:
        raise LiveArtifactError("baseline results do not contain exactly the three required modes")
    episode_id = manifest.get("episode_id")
    data_version = manifest.get("data_version")
    if not isinstance(episode_id, str) or not isinstance(data_version, str):
        raise LiveArtifactError("live manifest episode identity is invalid")
    if any(
        record.episode_id != episode_id
        or record.data_version != data_version
        or record.action_executed is not False
        or record.forbidden_tool_calls != 0
        or record.unseen_evidence != 0
        for record in records
    ):
        raise LiveArtifactError("baseline results violate matched evidence or action safety")
    if len({record.visible_evidence_ids for record in records}) != 1:
        raise LiveArtifactError("baseline modes do not share one visible evidence set")
    expected_versions = {
        BaselineMode.STATIC: (
            "static-baseline-v1",
            "not_applicable",
            "not_applicable",
        ),
        BaselineMode.INVESTIGATOR: (
            "14",
            "investigator-v14",
            "investigator-tools-v1",
        ),
        BaselineMode.INVESTIGATOR_VERIFIER: (
            f"investigator-{INVESTIGATOR_VERSION}+verifier-"
            f"{pins.verifier.agent_version if pins is not None else VERIFIER_VERSION}",
            f"investigator-v14+"
            f"{pins.verifier.prompt_version if pins is not None else 'verifier-v1'}",
            "investigator-tools-v1+verifier-tools-v1",
        ),
    }
    for record in records:
        expected = expected_versions[record.mode]
        actual = (
            record.agent_version,
            record.prompt_version,
            record.tool_schema_version,
        )
        if actual != expected:
            raise LiveArtifactError("baseline result version pins are not exact")
    investigator_record = next(
        record for record in records if record.mode is BaselineMode.INVESTIGATOR
    )
    verifier_record = next(
        record for record in records if record.mode is BaselineMode.INVESTIGATOR_VERIFIER
    )
    _validate_trace_linkage(output, investigator_mode, record=investigator_record)
    if verifier_mode.get("verifier_invoked", True):
        _validate_trace_linkage(output, verifier_mode, record=verifier_record)
    _validate_saved_accounting(
        output,
        investigator_mode,
        verifier_mode,
        require_evaluation=require_evaluation,
        artifact_schema_version=manifest["schema_version"],
        episode_id=episode_id,
    )
    if require_evaluation:
        try:
            BaselineEvaluation.model_validate(
                json.loads((output / "evaluation.json").read_text(encoding="utf-8"))
            )
        except (OSError, json.JSONDecodeError, ValidationError) as error:
            raise LiveArtifactError("evaluation artifact is invalid") from error
    return output


def _static_brief(episode: BaselineEpisode) -> dict[str, object]:
    return {
        "mode": BaselineMode.STATIC.value,
        "episode_id": episode.episode_id,
        "data_version": episode.data_version,
        "detector_status": episode.detector_status.value,
        "visible_evidence_ids": episode.visible_evidence_ids,
        "runbook_queries": tuple(query.value for query in episode.runbook_queries),
        "runbook_chunk_ids": episode.runbook_chunk_ids,
        "disposition": "abstained",
        "action_executed": False,
    }


def _verifier_not_invoked_reason(workflow: object) -> str:
    """Return a fixed safe reason when the finite workflow never called Verifier."""

    failure = getattr(workflow, "failure", None)
    reasons: dict[WorkflowFailure, str] = {
        WorkflowFailure.INVALID_SCHEMA: "investigator_failed",
        WorkflowFailure.TIMEOUT: "investigator_timeout",
        WorkflowFailure.TOOL_TRANSPORT: "investigator_tool_failure",
        WorkflowFailure.STALE_REQUIRED_DATA: "stale_before_verifier",
        WorkflowFailure.BLOCKED: "investigator_blocked",
    }
    return (
        reasons.get(failure, "pre_verifier_gate")
        if isinstance(failure, WorkflowFailure)
        else "pre_verifier_gate"
    )


def _failure_artifact_for_workflow(
    *,
    investigation: InvestigationResultV1,
    workflow: object,
    verifier_invoked: bool,
) -> Mapping[str, object] | None:
    failure = getattr(workflow, "failure", None)
    if not isinstance(failure, WorkflowFailure):
        return None
    if investigation.disposition is Disposition.ANALYSIS_INCOMPLETE:
        component = "investigator"
    elif verifier_invoked:
        component = "verifier"
    else:
        component = "workflow"
    return {
        "schema_version": "live-failure-v1",
        "component": component,
        "failure": failure.value,
        "agent_invoked": investigation.disposition is Disposition.ANALYSIS_INCOMPLETE
        or verifier_invoked,
        "action_executed": False,
    }


def run_matched_live_modes(
    *,
    project_root: Path,
    run_id: str,
    packet: IncidentPacketV1,
    investigator: InvestigatorProtocol,
    verifier: VerifierProtocol,
    policy: PolicyResponse,
    budget: ResourceBudget = LIVE_BASELINE_BUDGET,
    investigator_version: str = INVESTIGATOR_VERSION,
    verifier_version: str = VERIFIER_VERSION,
    investigator_prompt_version: str = "investigator-v14",
    verifier_prompt_version: str = "verifier-v1",
    investigator_tool_schema_version: str = "investigator-tools-v1",
    verifier_tool_schema_version: str = "verifier-tools-v1",
    oracle_loader: Callable[[str], DevelopmentOracle] | None = None,
    pins: LiveAgentPins | None = None,
    score_results: bool = True,
    allow_noncanonical: bool = False,
    output_directory: Path | None = None,
) -> LiveBaselineExecution:
    """Run one incident through the matched modes and optionally score it."""

    if not isinstance(packet, IncidentPacketV1):
        raise TypeError("live matched modes require IncidentPacketV1")
    if not allow_noncanonical and packet.incident_id != CANONICAL_EPISODE_ID:
        raise LivePrerequisiteError("this slice permits only the canonical development episode")
    output = (
        output_directory.resolve()
        if output_directory is not None
        else baseline_run_directory(project_root, run_id)
    )
    if output.exists():
        raise LivePrerequisiteError("live run ID already exists; choose a unique safe run ID")
    if pins is not None:
        verifier_version = pins.verifier.agent_version
        verifier_prompt_version = pins.verifier.prompt_version
    episode = baseline_episode_from_packet(packet)
    static = run_static_baseline(episode, budget=budget, workflow_version=LIVE_WORKFLOW_VERSION)

    recording_investigator = _RecordingInvestigator(investigator)
    initial_error: Exception | None = None
    try:
        initial_result = recording_investigator.investigate(packet)
    except Exception as error:
        initial_error = error
        initial_result = _incomplete_investigation(packet)
    if not isinstance(initial_result, InvestigationResultV1):
        raise LivePrerequisiteError("Investigator returned an invalid result type")
    initial_capture = recording_investigator.runs[-1]
    if initial_result.disposition is not Disposition.ANALYSIS_INCOMPLETE:
        try:
            validate_investigation_result(
                initial_result,
                packet=packet,
                returned_evidence_ids=initial_capture.returned_evidence_ids,
            )
        except Exception as error:
            initial_error = error
            initial_result = _incomplete_investigation(packet)
            initial_capture = replace(
                initial_capture,
                result=initial_result,
                error=_sanitize_live_diagnostic_message(str(error)),
            )
    elif initial_error is not None and initial_capture.error is None:
        initial_capture = replace(
            initial_capture,
            result=initial_result,
            error=_sanitize_live_diagnostic_message(str(initial_error)),
        )
    reused_investigator = _ReusedInvestigator(recording_investigator, initial_result)
    recording_verifier = _RecordingVerifier(verifier)
    orchestrator = WorkflowOrchestrator(
        investigator=reused_investigator,
        verifier=recording_verifier,
        policy=policy,
    )
    workflow = orchestrator.run(packet, workflow_id=f"workflow-{run_id}-{packet.incident_id}")
    current_investigation = workflow.context.current_investigation or initial_result
    investigator_capture = _aggregate_runs(recording_investigator.runs, current_investigation)
    if recording_verifier.runs:
        verifier_result = recording_verifier.runs[-1].result
        verifier_capture: _CapturedAgentRun | None = _aggregate_runs(
            recording_verifier.runs,
            verifier_result,
        )
        if not isinstance(verifier_result, VerificationResultV1):
            raise LivePrerequisiteError("Verifier returned an invalid result type")
    else:
        verifier_capture = None

    if initial_result.proposed_action is None:
        initial_action_policy = None
    else:
        try:
            initial_action_policy = evaluate_action_policy(
                investigation=initial_result,
                policy=policy,
                packet=packet,
            )
        except Exception:
            initial_action_policy = None
    one = _record_for_mode(
        mode=BaselineMode.INVESTIGATOR,
        episode=episode,
        budget=budget,
        captured=initial_capture,
        investigation=initial_result,
        run_id=f"{run_id}-investigator",
        action_card_eligible=(initial_action_policy is not None and initial_action_policy.allowed),
        unsupported_claims=_unsupported_claim_count(
            recording_verifier.runs[0].result if recording_verifier.runs else None
        ),
        policy_errors=_action_policy_error_count(
            initial_result,
            policy=policy,
            packet=packet,
        ),
        failure=(
            workflow.failure
            if initial_result.disposition is Disposition.ANALYSIS_INCOMPLETE
            else None
        ),
        agent_version=investigator_version,
        prompt_version=investigator_prompt_version,
        tool_schema_version=investigator_tool_schema_version,
    )
    policy_errors = _action_policy_error_count(
        current_investigation,
        policy=policy,
        packet=packet,
    )
    two_capture = _aggregate_runs(
        (investigator_capture,)
        if verifier_capture is None
        else (investigator_capture, verifier_capture),
        current_investigation,
    )
    two = _record_for_mode(
        mode=BaselineMode.INVESTIGATOR_VERIFIER,
        episode=episode,
        budget=budget,
        captured=two_capture,
        investigation=current_investigation,
        run_id=f"{run_id}-investigator-verifier",
        workflow_state=workflow.final_state,
        failure=workflow.failure,
        action_card_eligible=workflow.action_card is not None,
        unsupported_claims=_unsupported_claim_count(workflow.context.verification),
        policy_errors=policy_errors,
        agent_version=f"investigator-{investigator_version}+verifier-{verifier_version}",
        prompt_version=f"{investigator_prompt_version}+{verifier_prompt_version}",
        tool_schema_version=f"{investigator_tool_schema_version}+{verifier_tool_schema_version}",
    )
    runs = (static, one, two)
    validate_matched_baseline_runs(
        (episode,),
        runs,
        allowed_episode_ids={packet.incident_id},
        allow_incomplete_metrics=True,
    )
    persisted_directory = _persist_live_artifacts(
        project_root=project_root,
        run_id=run_id,
        episode=episode,
        runs=runs,
        static_brief=_static_brief(episode),
        initial_investigator_capture=initial_capture,
        investigator_capture=investigator_capture,
        verifier_capture=verifier_capture,
        investigator_invocation_count=len(recording_investigator.runs),
        verifier_invocation_count=len(recording_verifier.runs),
        verifier_not_invoked_reason=_verifier_not_invoked_reason(workflow),
        workflow=workflow,
        pins=pins,
        scored=None,
        output_directory=output,
        nested=output_directory is not None,
        scope="development_episode" if output_directory is not None else "single_episode",
        failure_artifact=_failure_artifact_for_workflow(
            investigation=initial_result,
            workflow=workflow,
            verifier_invoked=bool(recording_verifier.runs),
        ),
        revision_capture=(
            recording_investigator.runs[-1] if len(recording_investigator.runs) > 1 else None
        ),
        first_verifier_capture=(recording_verifier.runs[0] if recording_verifier.runs else None),
    )
    validate_live_artifacts(
        persisted_directory,
        run_id,
        require_evaluation=False,
        _nested=output_directory is not None,
        expected_episode_id=episode.episode_id,
    )
    evaluation: BaselineEvaluation | None = None
    if score_results:
        loader = oracle_loader or (
            lambda episode_id: load_development_oracle(project_root, episode_id)
        )
        evaluation = score_development_baselines(
            (episode,),
            runs,
            oracle_loader=cast(OracleLoader, loader),
            allowed_episode_ids={packet.incident_id},
            allow_incomplete_metrics=True,
        )
        # Update only the already-contained output after evaluator access; no agent is called here.
        scored_manifest = json.loads(
            (persisted_directory / "live_manifest.json").read_text(encoding="utf-8")
        )
        scored_manifest["status"] = "scored"
        _write_json(persisted_directory / "evaluation.json", evaluation.model_dump(mode="json"))
        scored_manifest["evaluation_path"] = "evaluation.json"
        accounting_path = persisted_directory / "investigator_verifier" / "accounting.json"
        accounting = json.loads(accounting_path.read_text(encoding="utf-8"))
        if not isinstance(accounting, dict):
            raise LivePrerequisiteError("live accounting artifact is invalid")
        accounting["oracle_loaded_after_agent_outputs"] = True
        _write_json(accounting_path, accounting)
        _write_json(persisted_directory / "live_manifest.json", redact_mapping(scored_manifest))
        validate_live_artifacts(
            persisted_directory,
            run_id,
            _nested=output_directory is not None,
            expected_episode_id=episode.episode_id,
        )
    return LiveBaselineExecution(
        run_id=run_id,
        output_directory=persisted_directory,
        episode=episode,
        runs=runs,
        workflow=workflow,
        evaluation=evaluation,
        pins=pins,
    )


@dataclass(frozen=True, slots=True)
class _SafeWorkflow:
    """Typed local failure outcome used when detector setup itself fails."""

    final_state: WorkflowState
    action_card: object | None
    failure: WorkflowFailure | None
    audit_events: tuple[object, ...] = ()


def _development_output_directory(
    project_root: Path,
    run_id: str,
    artifact_root: Path | None,
) -> Path:
    if _DEVELOPMENT_LIVE_RUN_ID.fullmatch(run_id) is None:
        raise LivePrerequisiteError(
            "all-development live runs require run-phase5-development-live-v<digits>"
        )
    # Reuse the existing protected-name and containment checks without creating anything.
    canonical_directory = baseline_run_directory(project_root, run_id)
    if artifact_root is None:
        return canonical_directory
    root = artifact_root.resolve()
    runs_root = root if root.name == "runs" else root / "data" / "infineq" / "v1" / "runs"
    candidate = (runs_root / run_id).resolve()
    if not candidate.is_relative_to(runs_root):
        raise LivePrerequisiteError("development live artifacts escaped the artifact root")
    return candidate


def _incomplete_investigation_for_episode(episode_id: str) -> InvestigationResultV1:
    return InvestigationResultV1(
        investigation_id=f"investigation-{episode_id}",
        incident_id=episode_id,
        completed_at=datetime.now(UTC),
        disposition=Disposition.ANALYSIS_INCOMPLETE,
        summary="The deterministic detector setup did not produce an agent-visible packet.",
        hypotheses=(),
        leading_hypothesis_id=None,
        proposed_action=None,
        cited_evidence_ids=(),
        limitations=("detector output was unavailable",),
    )


def _run_packetless_development_episode(
    *,
    project_root: Path,
    run_id: str,
    detection: DetectionRun,
    investigator: InvestigatorProtocol,
    verifier: VerifierProtocol,
    policy: PolicyResponse,
    budget: ResourceBudget,
    pins: LiveAgentPins | None,
    output_directory: Path,
) -> LiveBaselineExecution:
    del investigator, verifier
    episode = baseline_episode_from_detection(detection)
    result = _packetless_investigation(detection)
    capture = _synthetic_capture(result)
    static = run_static_baseline(episode, budget=budget, workflow_version=LIVE_WORKFLOW_VERSION)
    workflow = WorkflowOrchestrator(
        investigator=cast(InvestigatorProtocol, object()),
        verifier=cast(VerifierProtocol, object()),
        policy=policy,
    ).run_detection(detection, workflow_id=f"workflow-{run_id}-{episode.episode_id}")
    one = _record_for_mode(
        mode=BaselineMode.INVESTIGATOR,
        episode=episode,
        budget=budget,
        captured=capture,
        investigation=result,
        run_id=f"{run_id}-{episode.episode_id}-investigator",
        action_card_eligible=False,
        agent_version=INVESTIGATOR_VERSION,
        prompt_version="investigator-v14",
        tool_schema_version="investigator-tools-v1",
    )
    two = _record_for_mode(
        mode=BaselineMode.INVESTIGATOR_VERIFIER,
        episode=episode,
        budget=budget,
        captured=capture,
        investigation=result,
        run_id=f"{run_id}-{episode.episode_id}-investigator-verifier",
        workflow_state=workflow.final_state,
        failure=workflow.failure,
        action_card_eligible=False,
        policy_errors=0,
        agent_version=(
            f"investigator-{INVESTIGATOR_VERSION}+verifier-"
            f"{pins.verifier.agent_version if pins is not None else VERIFIER_VERSION}"
        ),
        prompt_version=(
            "investigator-v14+"
            f"{pins.verifier.prompt_version if pins is not None else 'verifier-v1'}"
        ),
        tool_schema_version="investigator-tools-v1+verifier-tools-v1",
    )
    runs = (static, one, two)
    validate_matched_baseline_runs(
        (episode,),
        runs,
        allowed_episode_ids={episode.episode_id},
        allow_incomplete_metrics=True,
    )
    persisted = _persist_live_artifacts(
        project_root=project_root,
        run_id=run_id,
        episode=episode,
        runs=runs,
        static_brief=_static_brief(episode),
        initial_investigator_capture=capture,
        investigator_capture=capture,
        verifier_capture=None,
        investigator_invocation_count=0,
        verifier_invocation_count=0,
        verifier_not_invoked_reason="packetless_detection",
        workflow=workflow,
        pins=pins,
        scored=None,
        output_directory=output_directory,
        nested=True,
        scope="development_episode",
    )
    validate_live_artifacts(
        persisted,
        run_id,
        require_evaluation=False,
        _nested=True,
        expected_episode_id=episode.episode_id,
    )
    return LiveBaselineExecution(
        run_id=run_id,
        output_directory=persisted,
        episode=episode,
        runs=runs,
        workflow=workflow,
        evaluation=None,
        pins=pins,
    )


def _run_detector_failure_episode(
    *,
    project_root: Path,
    run_id: str,
    episode_id: str,
    policy: PolicyResponse,
    budget: ResourceBudget,
    pins: LiveAgentPins | None,
    output_directory: Path,
) -> LiveBaselineExecution:
    episode = BaselineEpisode(
        episode_id=episode_id,
        data_version="corpus-v1",
        visible_evidence=(),
        detector_status=DetectorStatus.ABSTAIN,
        runbook_queries=(),
        runbook_chunk_ids=(),
    )
    result = _incomplete_investigation_for_episode(episode_id)
    capture = _synthetic_capture(result)
    workflow = _SafeWorkflow(
        final_state=WorkflowState.ANALYSIS_INCOMPLETE,
        action_card=None,
        failure=WorkflowFailure.INVALID_SCHEMA,
    )
    static = run_static_baseline(episode, budget=budget, workflow_version=LIVE_WORKFLOW_VERSION)
    one = _record_for_mode(
        mode=BaselineMode.INVESTIGATOR,
        episode=episode,
        budget=budget,
        captured=capture,
        investigation=result,
        run_id=f"{run_id}-{episode_id}-investigator",
        failure=WorkflowFailure.INVALID_SCHEMA,
        agent_version=INVESTIGATOR_VERSION,
        prompt_version="investigator-v14",
        tool_schema_version="investigator-tools-v1",
    )
    two = _record_for_mode(
        mode=BaselineMode.INVESTIGATOR_VERIFIER,
        episode=episode,
        budget=budget,
        captured=capture,
        investigation=result,
        run_id=f"{run_id}-{episode_id}-investigator-verifier",
        workflow_state=workflow.final_state,
        failure=workflow.failure,
        agent_version=(
            f"investigator-{INVESTIGATOR_VERSION}+verifier-"
            f"{pins.verifier.agent_version if pins is not None else VERIFIER_VERSION}"
        ),
        prompt_version=(
            "investigator-v14+"
            f"{pins.verifier.prompt_version if pins is not None else 'verifier-v1'}"
        ),
        tool_schema_version="investigator-tools-v1+verifier-tools-v1",
    )
    runs = (static, one, two)
    validate_matched_baseline_runs(
        (episode,),
        runs,
        allowed_episode_ids={episode_id},
        allow_incomplete_metrics=True,
    )
    persisted = _persist_live_artifacts(
        project_root=project_root,
        run_id=run_id,
        episode=episode,
        runs=runs,
        static_brief=_static_brief(episode),
        initial_investigator_capture=capture,
        investigator_capture=capture,
        verifier_capture=None,
        investigator_invocation_count=0,
        verifier_invocation_count=0,
        verifier_not_invoked_reason="detector_failure",
        workflow=workflow,
        pins=pins,
        scored=None,
        output_directory=output_directory,
        nested=True,
        scope="development_episode",
        failure_artifact={
            "schema_version": "live-failure-v1",
            "component": "detector",
            "failure": "detector_unavailable",
            "agent_invoked": False,
            "action_executed": False,
        },
    )
    validate_live_artifacts(
        persisted,
        run_id,
        require_evaluation=False,
        _nested=True,
        expected_episode_id=episode_id,
    )
    return LiveBaselineExecution(
        run_id=run_id,
        output_directory=persisted,
        episode=episode,
        runs=runs,
        workflow=workflow,
        evaluation=None,
        pins=pins,
    )


def _development_runs_directory(
    project_root: Path,
    run_id: str,
    artifact_root: Path | None,
) -> Path:
    return _development_output_directory(project_root, run_id, artifact_root)


def _episode_evaluation(
    evaluation: BaselineEvaluation,
    episode_id: str,
    runs: Sequence[BaselineRunRecord],
) -> BaselineEvaluation:
    scores = tuple(score for score in evaluation.scores if score.episode_id == episode_id)
    if len(scores) != len(_MODE_INDEX):
        raise LivePrerequisiteError("aggregate evaluator did not return three episode scores")
    pass_counts = tuple(
        ModePassCount(
            mode=mode,
            pass_count=sum(1 for score in scores if score.mode is mode and score.full_pass),
            episode_count=1,
        )
        for mode in _MODE_INDEX
    )
    unsupported_claims_by_mode = {
        mode.value: sum(
            run.unsupported_claims
            for run in runs
            if run.episode_id == episode_id and run.mode is mode
        )
        for mode in _MODE_INDEX
    }
    policy_errors_by_mode = {
        mode.value: sum(
            run.policy_errors for run in runs if run.episode_id == episode_id and run.mode is mode
        )
        for mode in _MODE_INDEX
    }
    one_mode = BaselineMode.INVESTIGATOR.value
    two_mode = BaselineMode.INVESTIGATOR_VERIFIER.value
    improvement = (
        unsupported_claims_by_mode[two_mode] < unsupported_claims_by_mode[one_mode]
        or policy_errors_by_mode[two_mode] < policy_errors_by_mode[one_mode]
    )
    canonical_action_passed = episode_id == CANONICAL_QUEUE_EPISODE_ID and any(
        score.mode is BaselineMode.INVESTIGATOR_VERIFIER and score.full_pass for score in scores
    )
    return BaselineEvaluation(
        episode_ids=(episode_id,),
        scores=scores,
        pass_counts=pass_counts,
        unsupported_claims_by_mode=unsupported_claims_by_mode,
        policy_errors_by_mode=policy_errors_by_mode,
        two_agent_justification_passed=(
            improvement
            and pass_counts[2].pass_count >= pass_counts[1].pass_count
            and canonical_action_passed
        ),
    )


def _mark_episode_scored(
    execution: LiveBaselineExecution,
    evaluation: BaselineEvaluation,
) -> None:
    episode_evaluation = _episode_evaluation(
        evaluation, execution.episode.episode_id, execution.runs
    )
    manifest_path = execution.output_directory / "live_manifest.json"
    manifest = _read_json(manifest_path, description="episode live manifest")
    manifest["status"] = "scored"
    manifest["evaluation_path"] = "evaluation.json"
    _write_json(
        execution.output_directory / "evaluation.json", episode_evaluation.model_dump(mode="json")
    )
    accounting_path = execution.output_directory / "investigator_verifier" / "accounting.json"
    accounting = _read_json(accounting_path, description="episode accounting")
    accounting["oracle_loaded_after_agent_outputs"] = True
    _write_json(accounting_path, accounting)
    _write_json(manifest_path, redact_mapping(manifest))


def _aggregate_development_metrics(
    *,
    run_id: str,
    episode_ids: Sequence[str],
    runs: Sequence[BaselineRunRecord],
    evaluation: BaselineEvaluation,
) -> dict[str, object]:
    by_mode: dict[str, object] = {}
    for mode in _MODE_INDEX:
        mode_runs = tuple(run for run in runs if run.mode is mode)
        status_counts: dict[str, int] = {}
        for run in mode_runs:
            status_counts[run.terminal_status.value] = (
                status_counts.get(run.terminal_status.value, 0) + 1
            )
        token_totals: dict[str, object] = {}
        for field_name in ("input_tokens", "output_tokens", "total_tokens"):
            values = [getattr(run, field_name) for run in mode_runs]
            known = [value for value in values if value is not None]
            token_totals[f"{field_name}_total"] = sum(known) if len(known) == len(values) else None
            token_totals[f"{field_name}_known_count"] = len(known)
        by_mode[mode.value] = {
            "record_count": len(mode_runs),
            "pass_count": evaluation.pass_count(mode),
            "unsupported_claims": sum(run.unsupported_claims for run in mode_runs),
            "policy_errors": sum(run.policy_errors for run in mode_runs),
            "unseen_evidence": sum(run.unseen_evidence for run in mode_runs),
            "forbidden_tool_calls": sum(run.forbidden_tool_calls for run in mode_runs),
            "successful_tool_calls": sum(run.successful_tool_calls for run in mode_runs),
            "redundant_tool_calls": sum(run.redundant_tool_calls for run in mode_runs),
            "latency_ms_total": round(sum(run.latency_ms or 0.0 for run in mode_runs), 3),
            "terminal_status_counts": dict(sorted(status_counts.items())),
            **token_totals,
        }
    return {
        "schema_version": "development-aggregate-v1",
        "run_id": run_id,
        "episode_count": len(episode_ids),
        "mode_record_count": len(runs),
        "episode_ids": tuple(episode_ids),
        "modes": by_mode,
        "pass_counts": {mode.value: evaluation.pass_count(mode) for mode in _MODE_INDEX},
        "unsupported_claims_by_mode": evaluation.unsupported_claims_by_mode,
        "policy_errors_by_mode": evaluation.policy_errors_by_mode,
        "two_agent_justification_passed": evaluation.two_agent_justification_passed,
        "action_execution_count": sum(run.action_executed for run in runs),
    }


def run_all_development_live_modes(
    *,
    project_root: Path,
    run_id: str,
    detector: Detector,
    investigator: InvestigatorProtocol,
    verifier: VerifierProtocol,
    policy: PolicyResponse,
    budget: ResourceBudget = LIVE_BASELINE_BUDGET,
    oracle_loader: Callable[[str], DevelopmentOracle] | None = None,
    pins: LiveAgentPins | None = None,
    artifact_root: Path | None = None,
) -> LiveDevelopmentExecution:
    """Run exactly the selected eight development episodes before one oracle pass."""

    episode_ids = tuple(select_development_episode_ids())
    if len(episode_ids) != 8 or len(set(episode_ids)) != 8:
        raise LivePrerequisiteError("the development selection must contain exactly eight IDs")
    if pins is not None:
        _validate_saved_pins(pins.model_dump(mode="json"), project_root=None)
    output = _development_runs_directory(project_root, run_id, artifact_root)
    if output.exists():
        raise LivePrerequisiteError("live run ID already exists; choose a unique safe run ID")
    episodes_root = output / "episodes"
    output.mkdir(parents=True, exist_ok=False)
    episodes_root.mkdir(parents=True, exist_ok=False)
    executions: list[LiveBaselineExecution] = []
    for episode_id in episode_ids:
        episode_output = episodes_root / episode_id
        try:
            detection = detector.run(episode_id)
        except Exception:
            execution = _run_detector_failure_episode(
                project_root=project_root,
                run_id=run_id,
                episode_id=episode_id,
                policy=policy,
                budget=budget,
                pins=pins,
                output_directory=episode_output,
            )
        else:
            if not isinstance(detection, DetectionRun):
                raise LivePrerequisiteError("detector returned an invalid typed result")
            if detection.packet is None:
                execution = _run_packetless_development_episode(
                    project_root=project_root,
                    run_id=run_id,
                    detection=detection,
                    investigator=investigator,
                    verifier=verifier,
                    policy=policy,
                    budget=budget,
                    pins=pins,
                    output_directory=episode_output,
                )
            else:
                execution = run_matched_live_modes(
                    project_root=project_root,
                    run_id=run_id,
                    packet=detection.packet,
                    investigator=investigator,
                    verifier=verifier,
                    policy=policy,
                    budget=budget,
                    oracle_loader=None,
                    pins=pins,
                    score_results=False,
                    allow_noncanonical=True,
                    output_directory=episode_output,
                )
        executions.append(execution)
    episodes = tuple(execution.episode for execution in executions)
    runs = tuple(run for execution in executions for run in execution.runs)
    workflows = tuple(execution.workflow for execution in executions)
    if tuple(episode.episode_id for episode in episodes) != episode_ids:
        raise LivePrerequisiteError("development episodes were not persisted in selection order")
    if len(runs) != 24:
        raise LivePrerequisiteError("all-development live run must contain exactly 24 mode records")
    validate_matched_baseline_runs(
        episodes,
        runs,
        allowed_episode_ids=episode_ids,
        allow_incomplete_metrics=True,
    )
    for execution in executions:
        validate_live_artifacts(
            execution.output_directory,
            run_id,
            require_evaluation=False,
            require_pins=pins is not None,
            project_root=project_root,
            _nested=True,
            expected_episode_id=execution.episode.episode_id,
        )
    _write_json(
        output / "live_manifest.json",
        redact_mapping(
            {
                "schema_version": "development-live-v1",
                "scope": "all_development",
                "run_id": run_id,
                "status": "awaiting_score",
                "episode_ids": episode_ids,
                "episodes_path": "episodes",
                "evaluation_audit_path": "evaluation_audit.json",
                "workflow_version": LIVE_WORKFLOW_VERSION,
                **({"agent_pins": pins.model_dump(mode="json")} if pins is not None else {}),
            }
        ),
    )
    _write_json(
        output / "evaluation_audit.json",
        {
            "schema_version": "development-evaluation-order-v1",
            "run_id": run_id,
            "all_episode_artifacts_validated": True,
            "validated_episode_count": len(episodes),
            "validated_mode_record_count": len(runs),
            "oracle_loaded_after_all_episode_outputs": False,
            "held_out_access": False,
        },
    )
    validate_live_artifacts(
        output,
        run_id,
        require_evaluation=False,
        require_pins=pins is not None,
        project_root=project_root,
    )
    loader = oracle_loader or (lambda episode_id: load_development_oracle(project_root, episode_id))
    evaluation = score_development_baselines(
        episodes,
        runs,
        oracle_loader=cast(OracleLoader, loader),
        allowed_episode_ids=episode_ids,
        allow_incomplete_metrics=True,
    )
    for execution in executions:
        _mark_episode_scored(execution, evaluation)
    aggregate = _aggregate_development_metrics(
        run_id=run_id,
        episode_ids=episode_ids,
        runs=runs,
        evaluation=evaluation,
    )
    _write_json(output / "evaluation.json", evaluation.model_dump(mode="json"))
    _write_json(output / "aggregate_metrics.json", aggregate)
    _write_json(
        output / "evaluation_audit.json",
        {
            "schema_version": "development-evaluation-order-v1",
            "run_id": run_id,
            "all_episode_artifacts_validated": True,
            "validated_episode_count": len(episodes),
            "validated_mode_record_count": len(runs),
            "oracle_loaded_after_all_episode_outputs": True,
            "held_out_access": False,
        },
    )
    final_manifest = _read_json(output / "live_manifest.json", description="development manifest")
    final_manifest.update(
        {
            "status": "scored",
            "evaluation_path": "evaluation.json",
            "aggregate_metrics_path": "aggregate_metrics.json",
            "evaluation_audit_path": "evaluation_audit.json",
        }
    )
    _write_json(output / "live_manifest.json", redact_mapping(final_manifest))
    validate_live_artifacts(
        output,
        run_id,
        require_evaluation=True,
        require_pins=pins is not None,
        project_root=project_root,
    )
    return LiveDevelopmentExecution(
        run_id=run_id,
        output_directory=output,
        episode_ids=episode_ids,
        episodes=episodes,
        runs=runs,
        workflows=workflows,
        evaluation=evaluation,
        aggregate_metrics=aggregate,
        pins=pins,
    )


__all__ = [
    "CANONICAL_EPISODE_ID",
    "INVESTIGATOR_VERSION",
    "LIVE_BASELINE_BUDGET",
    "LIVE_OPT_IN_ENV",
    "VERIFIER_VERSION",
    "InvestigatorDeploymentManifest",
    "LiveAgentPin",
    "LiveAgentPins",
    "LiveArtifactError",
    "LiveBaselineExecution",
    "LivePrerequisiteError",
    "LivePrerequisites",
    "LiveRuntime",
    "baseline_episode_from_packet",
    "build_live_runtime",
    "load_live_prerequisites",
    "run_matched_live_modes",
    "validate_exact_live_pins",
    "validate_live_artifacts",
]
