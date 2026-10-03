"""Versioned Microsoft Foundry prompt-agent registration and verification."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from azure.ai.projects.models import (
    PromptAgentDefinition,
    PromptAgentDefinitionTextOptions,
    TextResponseFormatJsonSchema,
)
from azure.core.exceptions import ResourceNotFoundError
from pydantic import BaseModel

from infineq.agents.prompt_manifest import (
    VERIFIER_PROMPT_VERSION,
    PromptManifest,
    load_investigator_prompt,
    load_verifier_prompt,
    prompt_sha256,
)
from infineq.agents.tool_definitions import (
    INVESTIGATOR_TOOL_NAMES,
    INVESTIGATOR_TOOL_SCHEMA_SHA256,
    get_investigator_tool_definitions,
)
from infineq.foundry.protocols import PromptAgentOperations
from infineq.foundry.verifier_tools import (
    VERIFIER_TOOL_NAMES,
    VERIFIER_TOOL_SCHEMA_SHA256,
    canonical_tool_schema_entries,
    get_verifier_tool_definitions,
    verifier_tool_schema_sha256,
)
from infineq.schemas.investigation import InvestigationResultV1
from infineq.schemas.verification import VerificationResultV1

INVESTIGATOR_AGENT_NAME: Final[str] = "infineq-investigator"
INVESTIGATION_RESULT_FORMAT_NAME: Final[str] = "investigation_result_v1"
VERIFIER_AGENT_NAME: Final[str] = "infineq-evidence-verifier"
VERIFICATION_RESULT_FORMAT_NAME: Final[str] = "verification_result_v1"
_SCHEMA_VERSION_FIELD: Final[str] = "schema_version"


class VersionNotFoundError(LookupError):
    """The exact requested agent version does not exist."""


class AgentVersionMismatchError(ValueError):
    """A retrieved agent version is not the pinned definition."""


class MalformedProviderDefinitionError(AgentVersionMismatchError):
    """A provider returned an incomplete or unsafe prompt-agent definition."""


@dataclass(frozen=True, slots=True)
class AgentVersionRecord:
    """Safe version identity and pinned metadata."""

    name: str
    version: str
    prompt_version: str
    prompt_sha256: str
    model_deployment_name: str

    def to_manifest(self) -> dict[str, str]:
        """Return a local-safe deployment manifest without endpoint or credentials."""

        return {
            "agent_name": self.name,
            "agent_version": self.version,
            "prompt_version": self.prompt_version,
            "prompt_sha256": self.prompt_sha256,
            "model_deployment_name": self.model_deployment_name,
        }


def _utc_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("creation timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp_text(value: datetime) -> str:
    return _utc_timestamp(value).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class VerifierAgentVersionRecord:
    """Safe identity and hashes for one immutable Verifier version."""

    name: str
    version: str
    prompt_version: str
    prompt_sha256: str
    model_deployment_name: str
    tool_schema_sha256: str
    created_at: datetime

    def to_manifest(self) -> dict[str, str]:
        """Return the credential-free fields persisted for the Verifier."""

        return {
            "agent_name": self.name,
            "agent_version": self.version,
            "prompt_version": self.prompt_version,
            "prompt_sha256": self.prompt_sha256,
            "model_deployment_name": self.model_deployment_name,
            "tool_schema_sha256": self.tool_schema_sha256,
            "created_at": _timestamp_text(self.created_at),
        }


def _field(record: object, name: str) -> object:
    if isinstance(record, Mapping):
        return record.get(name)
    return getattr(record, name, None)


def _build_strict_json_schema(contract: type[BaseModel]) -> dict[str, Any]:
    schema = deepcopy(contract.model_json_schema())

    def normalize(node: object) -> None:
        if not isinstance(node, dict):
            return
        properties = node.get("properties")
        if isinstance(properties, dict):
            properties.pop(_SCHEMA_VERSION_FIELD, None)
            node["required"] = list(properties)
            node["additionalProperties"] = False
            for property_schema in properties.values():
                normalize(property_schema)
        items = node.get("items")
        if isinstance(items, dict):
            normalize(items)
        for key in ("anyOf", "oneOf", "allOf"):
            alternatives = node.get(key)
            if isinstance(alternatives, list):
                for alternative in alternatives:
                    normalize(alternative)
        definitions = node.get("$defs")
        if isinstance(definitions, dict):
            for definition in definitions.values():
                normalize(definition)

    normalize(schema)
    return schema


def build_investigation_result_json_schema() -> dict[str, Any]:
    """Return the strict provider schema derived from ``InvestigationResultV1``."""

    return _build_strict_json_schema(InvestigationResultV1)


def build_verification_result_json_schema() -> dict[str, Any]:
    """Return the strict provider schema derived from ``VerificationResultV1``."""

    return _build_strict_json_schema(VerificationResultV1)


def build_prompt_agent_definition(
    *,
    model_deployment_name: str,
    manifest: PromptManifest,
    project_root: Path | None = None,
) -> PromptAgentDefinition:
    """Build the SDK definition from the committed prompt and strict tool schemas."""

    if not model_deployment_name:
        raise ValueError("model deployment name is required")
    resolved_project_root = project_root or Path(__file__).resolve().parents[3]
    instructions = (
        load_investigator_prompt(project_root=resolved_project_root)
        + "\n\nPinned prompt manifest metadata: "
        + f"version={manifest.version}; sha256={manifest.prompt_sha256}."
    )
    tools = [definition.to_foundry_tool() for definition in get_investigator_tool_definitions()]
    response_format = TextResponseFormatJsonSchema(
        name=INVESTIGATION_RESULT_FORMAT_NAME,
        schema=build_investigation_result_json_schema(),
        strict=True,
    )
    return PromptAgentDefinition(
        model=model_deployment_name,
        instructions=instructions,
        tools=tools,
        temperature=0.0,
        text=PromptAgentDefinitionTextOptions(format=response_format),
    )


def build_verifier_prompt_agent_definition(
    *,
    model_deployment_name: str,
    manifest: PromptManifest,
    project_root: Path | None = None,
) -> PromptAgentDefinition:
    """Build the independent Verifier definition with its strict two-tool boundary."""

    if not model_deployment_name:
        raise ValueError("model deployment name is required")
    if manifest.version != VERIFIER_PROMPT_VERSION:
        raise ValueError(f"verifier definition requires {VERIFIER_PROMPT_VERSION}")
    resolved_project_root = project_root or Path(__file__).resolve().parents[3]
    prompt = load_verifier_prompt(project_root=resolved_project_root)
    if prompt_sha256(prompt) != manifest.prompt_sha256:
        raise ValueError("verifier prompt hash does not match the committed prompt")
    instructions = (
        prompt
        + "\n\nPinned prompt manifest metadata: "
        + f"version={manifest.version}; sha256={manifest.prompt_sha256}."
    )
    tools = [definition.to_foundry_tool() for definition in get_verifier_tool_definitions()]
    response_format = TextResponseFormatJsonSchema(
        name=VERIFICATION_RESULT_FORMAT_NAME,
        schema=build_verification_result_json_schema(),
        strict=True,
    )
    return PromptAgentDefinition(
        model=model_deployment_name,
        instructions=instructions,
        tools=tools,
        temperature=0.0,
        text=PromptAgentDefinitionTextOptions(format=response_format),
    )


def _record_from_sdk(
    record: object,
    *,
    expected_version: str,
    manifest: PromptManifest,
    model_deployment_name: str,
    verify_definition: bool = False,
    project_root: Path | None = None,
) -> AgentVersionRecord:
    name = _field(record, "name")
    version = _field(record, "version")
    metadata = _field(record, "metadata")
    if not isinstance(name, str) or name != INVESTIGATOR_AGENT_NAME:
        raise AgentVersionMismatchError("retrieved agent name does not match")
    if not isinstance(version, str) or version != expected_version:
        raise AgentVersionMismatchError("retrieved agent version does not match")
    if not isinstance(metadata, Mapping):
        raise AgentVersionMismatchError("retrieved agent metadata is unavailable")
    expected = {
        "prompt_version": manifest.version,
        "prompt_sha256": manifest.prompt_sha256,
        "model_deployment_name": model_deployment_name,
    }
    for key, value in expected.items():
        if metadata.get(key) != value:
            raise AgentVersionMismatchError(f"retrieved agent metadata mismatch: {key}")
    if verify_definition:
        expected_definition = build_prompt_agent_definition(
            model_deployment_name=model_deployment_name,
            manifest=manifest,
            project_root=project_root,
        )
        _verify_investigator_definition(record, expected_definition=expected_definition)
    return AgentVersionRecord(
        name=name,
        version=version,
        prompt_version=manifest.version,
        prompt_sha256=manifest.prompt_sha256,
        model_deployment_name=model_deployment_name,
    )


def _verify_investigator_definition(
    record: object,
    *,
    expected_definition: PromptAgentDefinition,
) -> None:
    """Require the retrieved Investigator definition to match the pinned contract."""

    definition = _field(record, "definition")
    if definition is None:
        raise MalformedProviderDefinitionError("provider prompt-agent definition is unavailable")
    if _field(definition, "kind") != "prompt":
        raise MalformedProviderDefinitionError("provider definition is not a prompt agent")
    if _field(definition, "model") != expected_definition.model:
        raise AgentVersionMismatchError("retrieved Investigator model does not match")
    if _field(definition, "instructions") != expected_definition.instructions:
        raise AgentVersionMismatchError("retrieved Investigator prompt does not match")
    if _field(definition, "temperature") != 0.0:
        raise MalformedProviderDefinitionError("retrieved Investigator temperature is not pinned")

    actual_tools = _field(definition, "tools")
    expected_tools = _field(expected_definition, "tools")
    if (
        not isinstance(actual_tools, Sequence)
        or isinstance(actual_tools, (str, bytes))
        or not isinstance(expected_tools, Sequence)
        or isinstance(expected_tools, (str, bytes))
    ):
        raise MalformedProviderDefinitionError("provider Investigator tools are unavailable")
    if tuple(_field(tool, "name") for tool in actual_tools) != INVESTIGATOR_TOOL_NAMES:
        raise AgentVersionMismatchError(
            "retrieved Investigator tools do not match the seven-tool boundary"
        )
    try:
        actual_entries = canonical_tool_schema_entries(actual_tools)
        expected_entries = canonical_tool_schema_entries(expected_tools)
    except (TypeError, ValueError) as error:
        raise MalformedProviderDefinitionError(
            "provider Investigator tool schema is malformed"
        ) from error
    if actual_entries != expected_entries:
        raise AgentVersionMismatchError("retrieved Investigator tool schema does not match")
    if verifier_tool_schema_sha256(actual_tools) != INVESTIGATOR_TOOL_SCHEMA_SHA256:
        raise AgentVersionMismatchError("retrieved Investigator tool hash does not match")

    _verify_response_schema(
        definition,
        expected_definition,
        format_name=INVESTIGATION_RESULT_FORMAT_NAME,
        role="Investigator",
    )


def _verify_response_schema(
    definition: object,
    expected_definition: PromptAgentDefinition,
    *,
    format_name: str,
    role: str,
) -> None:
    """Require one strict JSON response format to match its local contract."""

    actual_text = _field(definition, "text")
    expected_text = _field(expected_definition, "text")
    actual_format = _field(actual_text, "format") if actual_text is not None else None
    expected_format = _field(expected_text, "format") if expected_text is not None else None
    if actual_format is None or expected_format is None:
        raise MalformedProviderDefinitionError(f"provider {role} response format is unavailable")
    if (
        _field(actual_format, "type") != "json_schema"
        or _field(actual_format, "name") != format_name
        or _field(actual_format, "strict") is not True
        or _field(actual_format, "schema") != _field(expected_format, "schema")
    ):
        raise AgentVersionMismatchError(f"retrieved {role} response schema does not match")


def _provider_created_at(record: object) -> datetime:
    value = _field(record, "created_at")
    if isinstance(value, datetime):
        return _utc_timestamp(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise MalformedProviderDefinitionError(
                "provider creation timestamp is malformed"
            ) from error
        return _utc_timestamp(parsed)
    raise MalformedProviderDefinitionError("provider creation timestamp is unavailable")


def _verify_verifier_definition(
    record: object,
    *,
    expected_definition: PromptAgentDefinition,
) -> None:
    definition = _field(record, "definition")
    if definition is None:
        raise MalformedProviderDefinitionError("provider prompt-agent definition is unavailable")
    if _field(definition, "kind") != "prompt":
        raise MalformedProviderDefinitionError("provider definition is not a prompt agent")
    if _field(definition, "model") != expected_definition.model:
        raise AgentVersionMismatchError("retrieved verifier model does not match")
    if _field(definition, "instructions") != expected_definition.instructions:
        raise AgentVersionMismatchError("retrieved verifier prompt does not match")
    if _field(definition, "temperature") != 0.0:
        raise MalformedProviderDefinitionError("retrieved verifier temperature is not pinned")

    actual_tools = _field(definition, "tools")
    expected_tools = _field(expected_definition, "tools")
    if (
        not isinstance(actual_tools, Sequence)
        or isinstance(actual_tools, (str, bytes))
        or not isinstance(expected_tools, Sequence)
        or isinstance(expected_tools, (str, bytes))
    ):
        raise MalformedProviderDefinitionError("provider verifier tools are unavailable")
    if tuple(_field(tool, "name") for tool in actual_tools) != VERIFIER_TOOL_NAMES:
        raise AgentVersionMismatchError(
            "retrieved verifier tools do not match the two-tool boundary"
        )
    try:
        actual_entries = canonical_tool_schema_entries(actual_tools)
        expected_entries = canonical_tool_schema_entries(expected_tools)
    except (TypeError, ValueError) as error:
        raise MalformedProviderDefinitionError(
            "provider verifier tool schema is malformed"
        ) from error
    if actual_entries != expected_entries:
        raise AgentVersionMismatchError("retrieved verifier tool schema does not match")
    if verifier_tool_schema_sha256(actual_tools) != VERIFIER_TOOL_SCHEMA_SHA256:
        raise AgentVersionMismatchError("retrieved verifier tool hash does not match")

    actual_text = _field(definition, "text")
    expected_text = _field(expected_definition, "text")
    actual_format = _field(actual_text, "format") if actual_text is not None else None
    expected_format = _field(expected_text, "format") if expected_text is not None else None
    if actual_format is None or expected_format is None:
        raise MalformedProviderDefinitionError("provider verifier response format is unavailable")
    if (
        _field(actual_format, "type") != "json_schema"
        or _field(actual_format, "name") != VERIFICATION_RESULT_FORMAT_NAME
        or _field(actual_format, "strict") is not True
        or _field(actual_format, "schema") != _field(expected_format, "schema")
    ):
        raise AgentVersionMismatchError("retrieved verifier response schema does not match")


def _verifier_record_from_sdk(
    record: object,
    *,
    expected_version: str,
    manifest: PromptManifest,
    model_deployment_name: str,
    creation_timestamp: datetime | None = None,
    project_root: Path | None = None,
) -> VerifierAgentVersionRecord:
    if manifest.version != VERIFIER_PROMPT_VERSION:
        raise AgentVersionMismatchError("verifier prompt version does not match")
    name = _field(record, "name")
    version = _field(record, "version")
    metadata = _field(record, "metadata")
    if not isinstance(name, str) or name != VERIFIER_AGENT_NAME:
        raise AgentVersionMismatchError("retrieved verifier name does not match")
    if not isinstance(version, str) or version != expected_version:
        raise AgentVersionMismatchError("retrieved verifier version does not match")
    if not isinstance(metadata, Mapping):
        raise AgentVersionMismatchError("retrieved verifier metadata is unavailable")
    expected_metadata = {
        "prompt_version": manifest.version,
        "prompt_sha256": manifest.prompt_sha256,
        "model_deployment_name": model_deployment_name,
        "tool_schema_sha256": VERIFIER_TOOL_SCHEMA_SHA256,
    }
    for key, value in expected_metadata.items():
        if metadata.get(key) != value:
            raise AgentVersionMismatchError(f"retrieved verifier metadata mismatch: {key}")

    expected_definition = build_verifier_prompt_agent_definition(
        model_deployment_name=model_deployment_name,
        manifest=manifest,
        project_root=project_root,
    )
    _verify_verifier_definition(record, expected_definition=expected_definition)
    if creation_timestamp is not None:
        resolved_created_at = _utc_timestamp(creation_timestamp)
    else:
        resolved_created_at = _provider_created_at(record)
    return VerifierAgentVersionRecord(
        name=name,
        version=version,
        prompt_version=manifest.version,
        prompt_sha256=manifest.prompt_sha256,
        model_deployment_name=model_deployment_name,
        tool_schema_sha256=VERIFIER_TOOL_SCHEMA_SHA256,
        created_at=resolved_created_at,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


class PromptAgentRegistry:
    """Create once or retrieve one exact prompt-agent version; never delete."""

    def __init__(
        self, operations: PromptAgentOperations | object, *, project_root: Path | None = None
    ) -> None:
        self._operations: Any = operations
        self._project_root = project_root

    def retrieve_exact(
        self,
        agent_version: str,
        *,
        agent_name: str = INVESTIGATOR_AGENT_NAME,
    ) -> object:
        """Retrieve one exact version and preserve a typed not-found boundary."""

        if not agent_name:
            raise ValueError("agent name is required")
        if not agent_version:
            raise ValueError("agent version is required")
        try:
            return self._operations.get_version(
                agent_name=agent_name,
                agent_version=agent_version,
            )
        except VersionNotFoundError:
            raise
        except ResourceNotFoundError as error:
            raise VersionNotFoundError("agent version was not found") from error

    def retrieve_investigator_version(
        self,
        *,
        model_deployment_name: str,
        manifest: PromptManifest,
        expected_version: str,
        verify_definition: bool = False,
    ) -> AgentVersionRecord:
        """Retrieve and validate one exact Investigator version without writes."""

        record = self.retrieve_exact(expected_version, agent_name=INVESTIGATOR_AGENT_NAME)
        return _record_from_sdk(
            record,
            expected_version=expected_version,
            manifest=manifest,
            model_deployment_name=model_deployment_name,
            verify_definition=verify_definition,
            project_root=self._project_root,
        )

    def retrieve_verifier_version(
        self,
        *,
        model_deployment_name: str,
        manifest: PromptManifest,
        expected_version: str,
        creation_timestamp: datetime | None = None,
    ) -> VerifierAgentVersionRecord:
        """Retrieve and validate one exact Verifier version without writes."""

        record = self.retrieve_exact(expected_version, agent_name=VERIFIER_AGENT_NAME)
        return _verifier_record_from_sdk(
            record,
            expected_version=expected_version,
            manifest=manifest,
            model_deployment_name=model_deployment_name,
            creation_timestamp=creation_timestamp,
            project_root=self._project_root,
        )

    def ensure_version(
        self,
        *,
        model_deployment_name: str,
        manifest: PromptManifest,
        expected_version: str | None = None,
        verify_definition: bool = False,
    ) -> AgentVersionRecord:
        """Retrieve an exact pinned version or create and immediately verify one."""

        if expected_version is not None:
            record = self.retrieve_exact(expected_version)
            return _record_from_sdk(
                record,
                expected_version=expected_version,
                manifest=manifest,
                model_deployment_name=model_deployment_name,
                verify_definition=verify_definition,
                project_root=self._project_root,
            )

        definition = build_prompt_agent_definition(
            model_deployment_name=model_deployment_name,
            manifest=manifest,
            project_root=self._project_root,
        )
        metadata = {
            "prompt_version": manifest.version,
            "prompt_sha256": manifest.prompt_sha256,
            "model_deployment_name": model_deployment_name,
        }
        created = self._operations.create_version(
            agent_name=INVESTIGATOR_AGENT_NAME,
            definition=definition,
            metadata=metadata,
            description="Infineq read-only reliability investigator",
        )
        created_version = _field(created, "version")
        if not isinstance(created_version, str) or not created_version:
            raise AgentVersionMismatchError("created agent version was not returned")
        verified = self.retrieve_exact(created_version)
        return _record_from_sdk(
            verified,
            expected_version=created_version,
            manifest=manifest,
            model_deployment_name=model_deployment_name,
            verify_definition=verify_definition,
            project_root=self._project_root,
        )

    def ensure_verifier_version(
        self,
        *,
        model_deployment_name: str,
        manifest: PromptManifest,
        expected_version: str | None = None,
        creation_timestamp: datetime | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> VerifierAgentVersionRecord:
        """Create or retrieve one exact Verifier version without deleting history."""

        if expected_version is not None:
            record = self.retrieve_exact(expected_version, agent_name=VERIFIER_AGENT_NAME)
            return _verifier_record_from_sdk(
                record,
                expected_version=expected_version,
                manifest=manifest,
                model_deployment_name=model_deployment_name,
                creation_timestamp=creation_timestamp,
                project_root=self._project_root,
            )

        definition = build_verifier_prompt_agent_definition(
            model_deployment_name=model_deployment_name,
            manifest=manifest,
            project_root=self._project_root,
        )
        resolved_creation_timestamp = _utc_timestamp(
            creation_timestamp if creation_timestamp is not None else clock()
        )
        metadata = {
            "prompt_version": manifest.version,
            "prompt_sha256": manifest.prompt_sha256,
            "model_deployment_name": model_deployment_name,
            "tool_schema_sha256": VERIFIER_TOOL_SCHEMA_SHA256,
        }
        created = self._operations.create_version(
            agent_name=VERIFIER_AGENT_NAME,
            definition=definition,
            metadata=metadata,
            description="Infineq evidence and safety verifier; read-only",
        )
        created_version = _field(created, "version")
        if not isinstance(created_version, str) or not created_version:
            raise AgentVersionMismatchError("created verifier version was not returned")
        verified = self.retrieve_exact(created_version, agent_name=VERIFIER_AGENT_NAME)
        return _verifier_record_from_sdk(
            verified,
            expected_version=created_version,
            manifest=manifest,
            model_deployment_name=model_deployment_name,
            creation_timestamp=resolved_creation_timestamp,
            project_root=self._project_root,
        )


__all__ = [
    "INVESTIGATION_RESULT_FORMAT_NAME",
    "INVESTIGATOR_AGENT_NAME",
    "VERIFICATION_RESULT_FORMAT_NAME",
    "VERIFIER_AGENT_NAME",
    "VERIFIER_PROMPT_VERSION",
    "AgentVersionMismatchError",
    "AgentVersionRecord",
    "MalformedProviderDefinitionError",
    "PromptAgentRegistry",
    "VerifierAgentVersionRecord",
    "VersionNotFoundError",
    "build_investigation_result_json_schema",
    "build_prompt_agent_definition",
    "build_verification_result_json_schema",
    "build_verifier_prompt_agent_definition",
]
