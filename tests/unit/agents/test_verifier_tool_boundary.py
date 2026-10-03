from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from infineq.foundry.verifier_tools import (
    canonical_tool_schema_entries,
    get_verifier_tool_definitions,
    verifier_tool_schema_sha256,
)


def _provider_mapping(definition: Any) -> dict[str, Any]:
    return {
        "type": "function",
        "name": definition.name,
        "description": definition.description,
        "parameters": definition.parameters,
        "strict": definition.strict,
    }


def _provider_object(definition: Any) -> SimpleNamespace:
    return SimpleNamespace(
        type="function",
        name=definition.name,
        description=definition.description,
        parameters=definition.parameters,
        strict=definition.strict,
    )


def test_provider_tool_mapping_and_object_have_the_same_safe_canonical_schema() -> None:
    definitions = get_verifier_tool_definitions()
    mappings = [_provider_mapping(definition) for definition in definitions]
    objects = [_provider_object(definition) for definition in definitions]

    assert canonical_tool_schema_entries(mappings) == canonical_tool_schema_entries(objects)
    assert verifier_tool_schema_sha256(mappings) == verifier_tool_schema_sha256(objects)


@pytest.mark.parametrize(
    ("provider_tool", "message"),
    [
        (
            {
                "type": "not_function",
                "name": "get_evidence",
                "description": "read",
                "parameters": {},
                "strict": True,
            },
            "non-function",
        ),
        (
            {
                "type": "function",
                "name": 42,
                "description": "read",
                "parameters": {},
                "strict": True,
            },
            "identity",
        ),
        (
            {
                "type": "function",
                "name": "get_evidence",
                "description": "read",
                "parameters": {},
                "strict": "true",
            },
            "schema",
        ),
    ],
)
def test_provider_tool_schema_rejects_malformed_boundary_records(
    provider_tool: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        canonical_tool_schema_entries([provider_tool])
