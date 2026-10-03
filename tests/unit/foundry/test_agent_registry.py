from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from azure.ai.projects.models import PromptAgentDefinitionTextOptions, TextResponseFormatJsonSchema

from infineq.agents.prompt_manifest import load_prompt_manifest, load_verifier_prompt_manifest
from infineq.agents.tool_definitions import INVESTIGATOR_TOOL_NAMES
from infineq.foundry import agent_registry
from infineq.foundry.agent_registry import (
    INVESTIGATOR_AGENT_NAME,
    VERIFIER_AGENT_NAME,
    AgentVersionMismatchError,
    PromptAgentRegistry,
    VerifierAgentVersionRecord,
    VersionNotFoundError,
    build_prompt_agent_definition,
)
from infineq.schemas.investigation import InvestigationResultV1
from infineq.schemas.verification import VerificationResultV1

PROJECT_ROOT = Path(__file__).parents[3]


@dataclass
class FakeAgentOperations:
    records: dict[tuple[str, str], Any]

    def __post_init__(self) -> None:
        self.created: list[tuple[str, Any, dict[str, str]]] = []
        self.retrieved: list[tuple[str, str]] = []

    def get_version(self, agent_name: str, agent_version: str) -> Any:
        self.retrieved.append((agent_name, agent_version))
        try:
            return self.records[(agent_name, agent_version)]
        except KeyError as error:
            raise VersionNotFoundError("not found") from error

    def create_version(
        self,
        agent_name: str,
        *,
        definition: Any,
        metadata: dict[str, str],
        description: str | None = None,
    ) -> Any:
        version = str(len(self.records) + 1)
        record = SimpleNamespace(
            name=agent_name,
            version=version,
            metadata=dict(metadata),
            definition=definition,
        )
        self.created.append((agent_name, definition, metadata))
        self.records[(agent_name, version)] = record
        return record


def _assert_strict_objects(
    node: object, root: dict[str, Any], seen_refs: set[str] | None = None
) -> None:
    if not isinstance(node, dict):
        return
    refs = seen_refs if seen_refs is not None else set()
    reference = node.get("$ref")
    if isinstance(reference, str):
        prefix = "#/$defs/"
        assert reference.startswith(prefix)
        ref_name = reference.removeprefix(prefix)
        if ref_name in refs:
            return
        target = root.get("$defs", {}).get(ref_name)
        assert isinstance(target, dict)
        _assert_strict_objects(target, root, refs | {ref_name})
        return
    for alternative in node.get("anyOf", []):
        _assert_strict_objects(alternative, root, refs)
    if node.get("type") == "array":
        _assert_strict_objects(node.get("items"), root, refs)
    if node.get("type") != "object":
        return
    properties = node.get("properties")
    required = node.get("required")
    assert isinstance(properties, dict)
    assert isinstance(required, list)
    assert node.get("additionalProperties") is False
    assert set(required) == set(properties)
    assert "schema_version" not in properties
    for property_schema in properties.values():
        _assert_strict_objects(property_schema, root, refs)


def test_prompt_definition_uses_a_strict_investigation_result_json_schema() -> None:
    manifest = load_prompt_manifest(project_root=PROJECT_ROOT)

    definition = build_prompt_agent_definition(
        model_deployment_name="model-under-test",
        manifest=manifest,
    )

    assert isinstance(definition.text, PromptAgentDefinitionTextOptions)
    response_format = definition.text.format
    assert isinstance(response_format, TextResponseFormatJsonSchema)
    assert response_format.type == "json_schema"
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", response_format.name)
    assert response_format.strict is True

    schema = response_format.schema
    assert schema["title"] == InvestigationResultV1.__name__
    assert set(schema["properties"]) == (
        set(InvestigationResultV1.model_json_schema()["properties"]) - {"schema_version"}
    )
    _assert_strict_objects(schema, schema)


def test_prompt_definition_pins_model_prompt_hash_and_exactly_seven_strict_tools() -> None:
    manifest = load_prompt_manifest(project_root=PROJECT_ROOT)

    definition = build_prompt_agent_definition(
        model_deployment_name="model-under-test",
        manifest=manifest,
    )

    assert definition.model == "model-under-test"
    assert manifest.prompt_sha256 in (definition.instructions or "")
    assert tuple(tool.name for tool in (definition.tools or [])) == INVESTIGATOR_TOOL_NAMES
    assert all(tool.strict is True for tool in (definition.tools or []))
    assert all(
        tool.parameters["additionalProperties"] is False for tool in (definition.tools or [])
    )


def test_registry_retrieves_exact_existing_version_without_creating() -> None:
    manifest = load_prompt_manifest(project_root=PROJECT_ROOT)
    definition = build_prompt_agent_definition(
        model_deployment_name="model-under-test", manifest=manifest
    )
    existing = SimpleNamespace(
        name=INVESTIGATOR_AGENT_NAME,
        version="7",
        metadata={
            "prompt_version": manifest.version,
            "prompt_sha256": manifest.prompt_sha256,
            "model_deployment_name": "model-under-test",
        },
        definition=definition,
    )
    operations = FakeAgentOperations({(INVESTIGATOR_AGENT_NAME, "7"): existing})

    result = PromptAgentRegistry(operations).ensure_version(
        model_deployment_name="model-under-test",
        manifest=manifest,
        expected_version="7",
    )

    assert result.version == "7"
    assert operations.created == []
    assert operations.retrieved == [(INVESTIGATOR_AGENT_NAME, "7")]


def test_retrieval_only_typed_methods_never_create_versions() -> None:
    investigator_manifest = load_prompt_manifest(project_root=PROJECT_ROOT)
    verifier_manifest = load_verifier_prompt_manifest(project_root=PROJECT_ROOT)
    model = "model-under-test"
    investigator_definition = build_prompt_agent_definition(
        model_deployment_name=model,
        manifest=investigator_manifest,
        project_root=PROJECT_ROOT,
    )
    verifier_definition = agent_registry.build_verifier_prompt_agent_definition(
        model_deployment_name=model,
        manifest=verifier_manifest,
        project_root=PROJECT_ROOT,
    )
    operations = FakeAgentOperations(
        {
            (INVESTIGATOR_AGENT_NAME, "14"): SimpleNamespace(
                name=INVESTIGATOR_AGENT_NAME,
                version="14",
                metadata={
                    "prompt_version": investigator_manifest.version,
                    "prompt_sha256": investigator_manifest.prompt_sha256,
                    "model_deployment_name": model,
                },
                definition=investigator_definition,
            ),
            (VERIFIER_AGENT_NAME, "1"): SimpleNamespace(
                name=VERIFIER_AGENT_NAME,
                version="1",
                metadata={
                    "prompt_version": verifier_manifest.version,
                    "prompt_sha256": verifier_manifest.prompt_sha256,
                    "model_deployment_name": model,
                    "tool_schema_sha256": agent_registry.VERIFIER_TOOL_SCHEMA_SHA256,
                },
                definition=verifier_definition,
                created_at=datetime(2026, 9, 14, tzinfo=UTC),
            ),
        }
    )
    registry = PromptAgentRegistry(operations, project_root=PROJECT_ROOT)

    investigator = registry.retrieve_investigator_version(
        model_deployment_name=model,
        manifest=investigator_manifest,
        expected_version="14",
        verify_definition=True,
    )
    verifier = registry.retrieve_verifier_version(
        model_deployment_name=model,
        manifest=verifier_manifest,
        expected_version="1",
    )

    assert investigator.version == "14"
    assert verifier.version == "1"
    assert operations.created == []
    assert operations.retrieved == [
        (INVESTIGATOR_AGENT_NAME, "14"),
        (VERIFIER_AGENT_NAME, "1"),
    ]

    manifest = load_prompt_manifest(project_root=PROJECT_ROOT)
    operations = FakeAgentOperations({})

    result = PromptAgentRegistry(operations).ensure_version(
        model_deployment_name="model-under-test",
        manifest=manifest,
    )

    assert result.version == "1"
    assert len(operations.created) == 1
    assert operations.retrieved[-1] == (INVESTIGATOR_AGENT_NAME, "1")
    assert operations.created[0][2]["prompt_sha256"] == manifest.prompt_sha256


def test_verifier_retrieval_uses_explicit_local_timestamp_when_provider_omits_it() -> None:
    manifest = load_verifier_prompt_manifest(project_root=PROJECT_ROOT)
    model = "model-under-test"
    definition = agent_registry.build_verifier_prompt_agent_definition(
        model_deployment_name=model,
        manifest=manifest,
        project_root=PROJECT_ROOT,
    )
    operations = FakeAgentOperations(
        {
            (VERIFIER_AGENT_NAME, "1"): SimpleNamespace(
                name=VERIFIER_AGENT_NAME,
                version="1",
                metadata={
                    "prompt_version": manifest.version,
                    "prompt_sha256": manifest.prompt_sha256,
                    "model_deployment_name": model,
                    "tool_schema_sha256": agent_registry.VERIFIER_TOOL_SCHEMA_SHA256,
                },
                definition=definition,
            )
        }
    )
    timestamp = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)

    result = PromptAgentRegistry(operations, project_root=PROJECT_ROOT).retrieve_verifier_version(
        model_deployment_name=model,
        manifest=manifest,
        expected_version="1",
        creation_timestamp=timestamp,
    )

    assert result.created_at == timestamp
    assert operations.created == []


def test_registry_rejects_a_version_with_a_different_pinned_hash() -> None:
    manifest = load_prompt_manifest(project_root=PROJECT_ROOT)
    bad = SimpleNamespace(
        name=INVESTIGATOR_AGENT_NAME,
        version="7",
        metadata={
            "prompt_version": manifest.version,
            "prompt_sha256": "0" * 64,
            "model_deployment_name": "model-under-test",
        },
    )
    operations = FakeAgentOperations({(INVESTIGATOR_AGENT_NAME, "7"): bad})

    with pytest.raises(AgentVersionMismatchError):
        PromptAgentRegistry(operations).ensure_version(
            model_deployment_name="model-under-test",
            manifest=manifest,
            expected_version="7",
        )


def test_registry_requires_an_exact_version_when_retrieving() -> None:
    operations = FakeAgentOperations({})

    with pytest.raises(VersionNotFoundError):
        PromptAgentRegistry(operations).retrieve_exact("missing")


def test_verifier_definition_uses_two_strict_read_tools_and_verification_schema() -> None:
    manifest = load_verifier_prompt_manifest(project_root=PROJECT_ROOT)
    definition = agent_registry.build_verifier_prompt_agent_definition(
        model_deployment_name="model-under-test",
        manifest=manifest,
        project_root=PROJECT_ROOT,
    )

    assert definition.model == "model-under-test"
    assert manifest.prompt_sha256 in (definition.instructions or "")
    assert tuple(tool.name for tool in (definition.tools or [])) == (
        "get_evidence",
        "get_policy",
    )
    assert all(tool.strict is True for tool in (definition.tools or []))
    assert all(
        tool.parameters["additionalProperties"] is False for tool in (definition.tools or [])
    )
    evidence_parameters = (definition.tools or [])[0].parameters
    assert evidence_parameters["properties"]["evidence_ids"]["maxItems"] == 20
    policy_parameters = (definition.tools or [])[1].parameters
    assert policy_parameters["properties"]["policy_ref"]["enum"] == ["policy-infineq-v1"]
    response_format = definition.text.format  # type: ignore[union-attr]
    assert isinstance(response_format, TextResponseFormatJsonSchema)
    assert response_format.name == "verification_result_v1"
    assert response_format.strict is True
    assert response_format.schema["title"] == VerificationResultV1.__name__
    assert "schema_version" not in response_format.schema["properties"]


def test_registry_selects_investigator_and_verifier_versions_independently() -> None:
    investigator_manifest = load_prompt_manifest(project_root=PROJECT_ROOT)
    verifier_manifest = load_verifier_prompt_manifest(project_root=PROJECT_ROOT)
    verifier_definition = agent_registry.build_verifier_prompt_agent_definition(
        model_deployment_name="model-under-test",
        manifest=verifier_manifest,
        project_root=PROJECT_ROOT,
    )
    timestamp = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    operations = FakeAgentOperations(
        {
            (INVESTIGATOR_AGENT_NAME, "14"): SimpleNamespace(
                name=INVESTIGATOR_AGENT_NAME,
                version="14",
                metadata={
                    "prompt_version": investigator_manifest.version,
                    "prompt_sha256": investigator_manifest.prompt_sha256,
                    "model_deployment_name": "model-under-test",
                },
            ),
            (VERIFIER_AGENT_NAME, "3"): SimpleNamespace(
                name=VERIFIER_AGENT_NAME,
                version="3",
                metadata={
                    "prompt_version": verifier_manifest.version,
                    "prompt_sha256": verifier_manifest.prompt_sha256,
                    "model_deployment_name": "model-under-test",
                    "tool_schema_sha256": agent_registry.VERIFIER_TOOL_SCHEMA_SHA256,
                },
                definition=verifier_definition,
                created_at=timestamp,
            ),
        }
    )
    registry = PromptAgentRegistry(operations)

    investigator = registry.retrieve_exact("14")
    verifier = registry.retrieve_exact("3", agent_name=VERIFIER_AGENT_NAME)

    assert investigator is operations.records[(INVESTIGATOR_AGENT_NAME, "14")]
    assert verifier is operations.records[(VERIFIER_AGENT_NAME, "3")]
    assert operations.retrieved == [
        (INVESTIGATOR_AGENT_NAME, "14"),
        (VERIFIER_AGENT_NAME, "3"),
    ]


def test_registry_creates_and_reads_back_one_strict_verifier_version() -> None:
    manifest = load_verifier_prompt_manifest(project_root=PROJECT_ROOT)
    timestamp = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)

    class VerifierOperations(FakeAgentOperations):
        def create_version(
            self,
            agent_name: str,
            *,
            definition: Any,
            metadata: dict[str, str],
            description: str | None = None,
        ) -> Any:
            return super().create_version(
                agent_name,
                definition=definition,
                metadata=metadata,
                description=description,
            )

    operations = VerifierOperations({})
    result = PromptAgentRegistry(operations, project_root=PROJECT_ROOT).ensure_verifier_version(
        model_deployment_name="model-under-test",
        manifest=manifest,
        clock=lambda: timestamp,
    )

    assert isinstance(result, VerifierAgentVersionRecord)
    assert result.name == VERIFIER_AGENT_NAME
    assert result.version == "1"
    assert result.prompt_version == manifest.version
    assert result.prompt_sha256 == manifest.prompt_sha256
    assert result.model_deployment_name == "model-under-test"
    assert result.tool_schema_sha256 == agent_registry.VERIFIER_TOOL_SCHEMA_SHA256
    assert result.created_at == timestamp
    assert operations.created[0][0] == VERIFIER_AGENT_NAME
    assert operations.created[0][2]["tool_schema_sha256"] == result.tool_schema_sha256
    assert operations.retrieved == [(VERIFIER_AGENT_NAME, "1")]
