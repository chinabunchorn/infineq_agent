"""Bounded, typed Responses API function-call loop."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic
from typing import TypeVar

from pydantic import BaseModel

from infineq.errors import ToolPolicyDeniedError, ToolTransportError
from infineq.foundry.protocols import (
    FoundryResponse,
    FoundryResponsesClient,
    FunctionToolOutput,
    ToolExecutor,
)
from infineq.security.redaction import redact_text

_EVIDENCE_ID = re.compile(
    r"^ev:[a-z0-9-]+:[a-z0-9_-]+:[a-z0-9_-]+:[a-z0-9_-]+:[a-z0-9_-]+:[a-f0-9]{8}$"
)
_INSTRUCTION_TEXT = re.compile(
    r"(?i)(ignore\s+(?:all\s+)?previous|system\s+message|developer\s+message|assistant\s+instruction|"
    r"reveal\s+(?:the\s+)?(?:hidden|private)\s+reasoning)"
)


class ToolLoopStatus(StrEnum):
    """Terminal states for one bounded investigation run."""

    COMPLETE = "complete"
    TOOL_BUDGET_EXCEEDED = "tool_budget_exceeded"
    INVALID_ARGUMENTS = "invalid_arguments"
    FORBIDDEN_TOOL = "forbidden_tool"
    TOOL_TRANSPORT_ERROR = "tool_transport_error"
    TIMEOUT = "timeout"
    ANALYSIS_INCOMPLETE = "analysis_incomplete"


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ToolLoopConfig:
    """Safety and timing limits applied by the local application."""

    max_successful_tool_calls: int = 6
    timeout_seconds: float = 60.0
    conversation_id: str | None = None
    max_tool_output_chars: int = 20_000

    def __post_init__(self) -> None:
        if self.max_successful_tool_calls < 1:
            raise ValueError("tool-call budget must be positive")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout must be positive")
        if self.max_tool_output_chars < 256:
            raise ValueError("tool output limit is too small")


@dataclass(frozen=True, slots=True)
class ToolCallTrace:
    """Redacted metadata for one attempted function call."""

    response_id: str
    call_id: str
    name: str
    status: str
    retry_count: int
    returned_evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolLoopResult:
    """Safe loop result; no raw tool arguments or bodies are retained."""

    status: ToolLoopStatus
    output_text: str
    response_ids: tuple[str, ...]
    traces: tuple[ToolCallTrace, ...]
    successful_tool_calls: int
    tool_retry_count: int
    returned_evidence_ids: frozenset[str]
    forbidden_tool_calls: int = 0
    parsed_output: object | None = None
    repair_attempted: bool = False
    usage: tuple[int | None, int | None, int | None] | None = None


def _safe_json_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return _safe_json_value(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): _safe_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_json_value(item) for item in value]
    if isinstance(value, str):
        return _INSTRUCTION_TEXT.sub("[untrusted instruction text removed]", redact_text(value))
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return redact_text(str(value))


def _serialize_tool_output(value: object, *, max_chars: int) -> tuple[str, frozenset[str]]:
    safe_value = _safe_json_value(value)
    try:
        serialized = json.dumps(
            {"untrusted_data": safe_value},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        serialized = json.dumps(
            {"untrusted_data": "[tool output rejected]"},
            ensure_ascii=True,
            separators=(",", ":"),
        )
    if len(serialized) > max_chars:
        serialized = json.dumps(
            {"untrusted_data": "[tool output truncated]"},
            ensure_ascii=True,
            separators=(",", ":"),
        )
    evidence: set[str] = set()

    def visit(item: object) -> None:
        if isinstance(item, str) and _EVIDENCE_ID.fullmatch(item):
            evidence.add(item)
        elif isinstance(item, Mapping):
            for child in item.values():
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(safe_value)
    return serialized, frozenset(evidence)


def _response_usage(
    responses: Sequence[FoundryResponse],
) -> tuple[int | None, int | None, int | None] | None:
    usage = [item.usage for item in responses if item.usage is not None]
    if not usage:
        return None
    return (
        sum(item.input_tokens or 0 for item in usage),
        sum(item.output_tokens or 0 for item in usage),
        sum(item.total_tokens or 0 for item in usage),
    )


def _incomplete(
    status: ToolLoopStatus,
    *,
    responses: Sequence[FoundryResponse],
    traces: Sequence[ToolCallTrace],
    successful: int,
    retries: int,
    evidence_ids: set[str],
    forbidden_tool_calls: int = 0,
    repair_attempted: bool = False,
) -> ToolLoopResult:
    output = responses[-1].output_text if responses else ""
    return ToolLoopResult(
        status=status,
        output_text=output,
        response_ids=tuple(item.response_id for item in responses),
        traces=tuple(traces),
        successful_tool_calls=successful,
        tool_retry_count=retries,
        returned_evidence_ids=frozenset(evidence_ids),
        forbidden_tool_calls=forbidden_tool_calls,
        repair_attempted=repair_attempted,
        usage=_response_usage(responses),
    )


def _request_response(
    client: FoundryResponsesClient,
    *,
    input: str | Sequence[FunctionToolOutput],
    previous_response_id: str | None,
    config: ToolLoopConfig,
    remaining_seconds: float,
    allow_tools: bool,
) -> tuple[FoundryResponse, int]:
    retries = 0
    while True:
        try:
            return (
                client.create_response(
                    input=input,
                    previous_response_id=previous_response_id,
                    conversation_id=config.conversation_id,
                    allow_tools=allow_tools,
                    timeout_seconds=max(remaining_seconds, 0.001),
                ),
                retries,
            )
        except ToolTransportError:
            if retries >= 1:
                raise
            retries += 1


def run_bounded_tool_loop(  # noqa: UP047
    client: FoundryResponsesClient,
    *,
    initial_input: str,
    executor: ToolExecutor,
    config: ToolLoopConfig | None = None,
    parse_final: Callable[[str], T] | None = None,
    repair_input: str | None = None,
    clock: Callable[[], float] = monotonic,
) -> ToolLoopResult:
    """Run Responses function calls with local validation and a hard budget."""

    resolved_config = config or ToolLoopConfig()
    started = clock()
    deadline = started + resolved_config.timeout_seconds
    responses: list[FoundryResponse] = []
    traces: list[ToolCallTrace] = []
    evidence_ids: set[str] = set()
    successful = 0
    retries = 0
    previous_response_id: str | None = None
    input_payload: str | Sequence[FunctionToolOutput] = initial_input

    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            return _incomplete(
                ToolLoopStatus.TIMEOUT,
                responses=responses,
                traces=traces,
                successful=successful,
                retries=retries,
                evidence_ids=evidence_ids,
            )
        try:
            current, request_retries = _request_response(
                client,
                input=input_payload,
                previous_response_id=previous_response_id,
                config=resolved_config,
                remaining_seconds=remaining,
                allow_tools=successful < resolved_config.max_successful_tool_calls,
            )
        except ToolTransportError:
            return _incomplete(
                ToolLoopStatus.TOOL_TRANSPORT_ERROR,
                responses=responses,
                traces=traces,
                successful=successful,
                retries=retries + 1,
                evidence_ids=evidence_ids,
            )
        retries += request_retries
        responses.append(current)
        if deadline - clock() <= 0:
            return _incomplete(
                ToolLoopStatus.TIMEOUT,
                responses=responses,
                traces=traces,
                successful=successful,
                retries=retries,
                evidence_ids=evidence_ids,
            )

        calls = current.function_calls
        if calls:
            if successful + len(calls) > resolved_config.max_successful_tool_calls:
                return _incomplete(
                    ToolLoopStatus.TOOL_BUDGET_EXCEEDED,
                    responses=responses,
                    traces=traces,
                    successful=successful,
                    retries=retries,
                    evidence_ids=evidence_ids,
                )
            outputs: list[FunctionToolOutput] = []
            allowed_tools = {
                "get_incident_packet",
                "get_signal_window",
                "get_request_samples",
                "get_deployment_snapshot",
                "search_runbook",
                "get_evidence",
                "prepare_action_plan",
            }
            forbidden_calls = sum(call.name not in allowed_tools for call in calls)
            if forbidden_calls:
                return _incomplete(
                    ToolLoopStatus.FORBIDDEN_TOOL,
                    responses=responses,
                    traces=traces,
                    successful=successful,
                    retries=retries,
                    evidence_ids=evidence_ids,
                    forbidden_tool_calls=forbidden_calls,
                )
            for call in calls:
                try:
                    arguments = json.loads(call.arguments)
                    if not isinstance(arguments, dict):
                        raise ValueError("function arguments must be a JSON object")
                    validated_arguments = executor.validate_arguments(call.name, arguments)
                except (TypeError, ValueError, json.JSONDecodeError, ToolPolicyDeniedError):
                    return _incomplete(
                        ToolLoopStatus.INVALID_ARGUMENTS,
                        responses=responses,
                        traces=traces,
                        successful=successful,
                        retries=retries,
                        evidence_ids=evidence_ids,
                    )
                call_retries = 0
                while True:
                    try:
                        output = executor.dispatch(call.name, validated_arguments)
                        break
                    except ToolTransportError:
                        if call_retries >= 1:
                            return _incomplete(
                                ToolLoopStatus.TOOL_TRANSPORT_ERROR,
                                responses=responses,
                                traces=traces,
                                successful=successful,
                                retries=retries + call_retries + 1,
                                evidence_ids=evidence_ids,
                            )
                        call_retries += 1
                    except Exception:
                        return _incomplete(
                            ToolLoopStatus.ANALYSIS_INCOMPLETE,
                            responses=responses,
                            traces=traces,
                            successful=successful,
                            retries=retries,
                            evidence_ids=evidence_ids,
                        )
                serialized, returned = _serialize_tool_output(
                    output,
                    max_chars=resolved_config.max_tool_output_chars,
                )
                evidence_ids.update(returned)
                traces.append(
                    ToolCallTrace(
                        response_id=current.response_id,
                        call_id=call.call_id,
                        name=call.name,
                        status="success",
                        retry_count=call_retries,
                        returned_evidence_ids=tuple(sorted(returned)),
                    )
                )
                outputs.append(FunctionToolOutput(call_id=call.call_id, output=serialized))
                successful += 1
                retries += call_retries
            previous_response_id = current.response_id
            input_payload = tuple(outputs)
            continue

        if parse_final is None:
            return ToolLoopResult(
                status=ToolLoopStatus.COMPLETE,
                output_text=current.output_text,
                response_ids=tuple(item.response_id for item in responses),
                traces=tuple(traces),
                successful_tool_calls=successful,
                tool_retry_count=retries,
                returned_evidence_ids=frozenset(evidence_ids),
                usage=_response_usage(responses),
            )
        try:
            parsed = parse_final(current.output_text)
        except Exception:
            if repair_input is None:
                return _incomplete(
                    ToolLoopStatus.ANALYSIS_INCOMPLETE,
                    responses=responses,
                    traces=traces,
                    successful=successful,
                    retries=retries,
                    evidence_ids=evidence_ids,
                )
            remaining = deadline - clock()
            if remaining <= 0:
                return _incomplete(
                    ToolLoopStatus.TIMEOUT,
                    responses=responses,
                    traces=traces,
                    successful=successful,
                    retries=retries,
                    evidence_ids=evidence_ids,
                )
            try:
                repaired, repair_retries = _request_response(
                    client,
                    input=repair_input,
                    previous_response_id=current.response_id,
                    config=resolved_config,
                    remaining_seconds=remaining,
                    allow_tools=False,
                )
            except ToolTransportError:
                return _incomplete(
                    ToolLoopStatus.ANALYSIS_INCOMPLETE,
                    responses=responses,
                    traces=traces,
                    successful=successful,
                    retries=retries + 1,
                    evidence_ids=evidence_ids,
                    repair_attempted=True,
                )
            retries += repair_retries
            responses.append(repaired)
            if repaired.function_calls:
                return _incomplete(
                    ToolLoopStatus.ANALYSIS_INCOMPLETE,
                    responses=responses,
                    traces=traces,
                    successful=successful,
                    retries=retries,
                    evidence_ids=evidence_ids,
                    repair_attempted=True,
                )
            try:
                parsed = parse_final(repaired.output_text)
            except Exception:
                return _incomplete(
                    ToolLoopStatus.ANALYSIS_INCOMPLETE,
                    responses=responses,
                    traces=traces,
                    successful=successful,
                    retries=retries,
                    evidence_ids=evidence_ids,
                    repair_attempted=True,
                )
            return ToolLoopResult(
                status=ToolLoopStatus.COMPLETE,
                output_text=repaired.output_text,
                response_ids=tuple(item.response_id for item in responses),
                traces=tuple(traces),
                successful_tool_calls=successful,
                tool_retry_count=retries,
                returned_evidence_ids=frozenset(evidence_ids),
                parsed_output=parsed,
                repair_attempted=True,
                usage=_response_usage(responses),
            )
        else:
            return ToolLoopResult(
                status=ToolLoopStatus.COMPLETE,
                output_text=current.output_text,
                response_ids=tuple(item.response_id for item in responses),
                traces=tuple(traces),
                successful_tool_calls=successful,
                tool_retry_count=retries,
                returned_evidence_ids=frozenset(evidence_ids),
                parsed_output=parsed,
                usage=_response_usage(responses),
            )


__all__ = [
    "ToolCallTrace",
    "ToolLoopConfig",
    "ToolLoopResult",
    "ToolLoopStatus",
    "run_bounded_tool_loop",
]
