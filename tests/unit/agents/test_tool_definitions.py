from __future__ import annotations

import json

from infineq.agents.tool_definitions import (
    INVESTIGATOR_TOOL_NAMES,
    get_investigator_tool_definitions,
)

EXPECTED_NAMES = (
    "get_incident_packet",
    "get_signal_window",
    "get_request_samples",
    "get_deployment_snapshot",
    "search_runbook",
    "get_evidence",
    "prepare_action_plan",
)


def test_exactly_seven_strict_function_schemas_are_defined() -> None:
    definitions = get_investigator_tool_definitions()

    assert tuple(definition.name for definition in definitions) == EXPECTED_NAMES
    assert INVESTIGATOR_TOOL_NAMES == EXPECTED_NAMES
    assert len({definition.name for definition in definitions}) == 7
    assert all(definition.strict is True for definition in definitions)


def test_every_function_schema_forbids_extra_properties_and_requires_all_fields() -> None:
    for definition in get_investigator_tool_definitions():
        parameters = definition.parameters
        assert parameters["type"] == "object"
        assert parameters["additionalProperties"] is False
        assert set(parameters["required"]) == set(parameters["properties"])
        assert json.loads(json.dumps(parameters)) == parameters


def test_tool_arguments_are_typed_and_bounded() -> None:
    definitions = {item.name: item for item in get_investigator_tool_definitions()}

    assert definitions["get_signal_window"].parameters["properties"]["signal_enum"]["enum"]
    assert definitions["get_signal_window"].parameters["properties"]["window_enum"]["enum"] == [
        "baseline",
        "observation",
    ]
    assert definitions["get_request_samples"].parameters["properties"]["limit"]["maximum"] == 20
    assert definitions["get_evidence"].parameters["properties"]["evidence_ids"]["maxItems"] == 20
    assert definitions["search_runbook"].parameters["properties"]["top_k"]["maximum"] == 3
    assert definitions["prepare_action_plan"].parameters["properties"]["action_type"]["enum"] == [
        "simulated_scale_out"
    ]


def test_prepare_action_plan_is_described_as_a_dry_run_only() -> None:
    definition = next(
        item for item in get_investigator_tool_definitions() if item.name == "prepare_action_plan"
    )

    description = f"{definition.description} {definition.parameters}".casefold()
    assert "dry" in description
    assert "execute" in description
    assert "simulator" in description
