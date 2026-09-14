"""Small protocols shared by the fake and Microsoft Foundry adapters."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class FunctionCall:
    """One model-requested application function call."""

    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class FunctionToolOutput:
    """One output linked to the exact model function call ID."""

    call_id: str
    output: str


@dataclass(frozen=True, slots=True)
class ResponseUsage:
    """Safe token counters from a provider response, when available."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class FoundryResponse:
    """Provider-neutral subset of a Responses API result."""

    response_id: str
    function_calls: tuple[FunctionCall, ...] = ()
    output_text: str = ""
    usage: ResponseUsage | None = None


class FoundryResponsesClient(Protocol):
    """Responses API surface used by the application loop."""

    def create_response(
        self,
        *,
        input: str | Sequence[FunctionToolOutput],
        previous_response_id: str | None,
        conversation_id: str | None,
        allow_tools: bool,
        timeout_seconds: float,
    ) -> FoundryResponse:
        """Create one response while preserving provider linkage."""

        ...


class ToolExecutor(Protocol):
    """Validated local tool boundary; implementations own dispatch policy."""

    def validate_arguments(self, name: str, arguments: dict[str, Any]) -> Any:
        """Validate untrusted JSON before dispatch."""

        ...

    def dispatch(self, name: str, arguments: Any) -> Any:
        """Execute one already-validated local function."""

        ...


class PromptAgentOperations(Protocol):
    """Minimal SDK surface needed to create and retrieve an agent version."""

    def create_version(
        self,
        *,
        agent_name: str,
        definition: Any,
        metadata: Mapping[str, str] | None = None,
        description: str | None = None,
    ) -> Any:
        """Create one immutable prompt-agent version."""

        ...

    def get_version(self, *, agent_name: str, agent_version: str) -> Any:
        """Retrieve one exact prompt-agent version."""

        ...


class FoundryProjectClient(Protocol):
    """Minimal project client used by registry/deployment adapters."""

    agents: PromptAgentOperations


__all__ = [
    "FoundryProjectClient",
    "FoundryResponse",
    "FoundryResponsesClient",
    "FunctionCall",
    "FunctionToolOutput",
    "PromptAgentOperations",
    "ResponseUsage",
    "ToolExecutor",
]
