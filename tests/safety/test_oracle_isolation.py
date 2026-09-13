import json
from dataclasses import replace

import pytest

from infineq.simulator.oracle_writer import (
    build_hidden_oracle,
    resolve_agent_path,
    write_hidden_oracle,
)
from infineq.simulator.scenarios import ScenarioFamily, scenario_catalog


def test_hidden_oracle_contains_expected_truth_but_is_not_observed_manifest() -> None:
    case = next(
        case for case in scenario_catalog() if case.family is ScenarioFamily.QUEUE_SATURATION
    )
    oracle = build_hidden_oracle(case)

    assert oracle.episode_id == case.episode_id
    assert oracle.scenario_family == "queue_saturation"
    assert oracle.expected_leading_family == "capacity_queueing"
    assert oracle.allowed_actions == ("SIMULATED_SCALE_OUT",)
    assert oracle.recovery_truth.expected_ready_replicas == 2


def test_oracle_writer_uses_a_separate_opaque_filename(tmp_path) -> None:
    case = scenario_catalog()[0]

    path = write_hidden_oracle(case, tmp_path)

    assert path == tmp_path / f"{case.episode_id}.oracle.json"
    payload = json.loads(path.read_text())
    assert payload["scenario_family"] == case.family.value
    assert "mechanism" in payload
    assert not (tmp_path / case.episode_id / "manifest.json").exists()


def test_oracle_writer_rejects_path_traversal_in_episode_identity(tmp_path) -> None:
    case = replace(scenario_catalog()[0], episode_id="../escape")

    with pytest.raises(ValueError):
        write_hidden_oracle(case, tmp_path)


def test_agent_path_cannot_escape_observed_root_to_hidden_oracles(tmp_path) -> None:
    data_root = tmp_path / "data" / "infineq" / "v1"
    data_root.joinpath("hidden_oracles").mkdir(parents=True)

    with pytest.raises(PermissionError):
        resolve_agent_path(data_root, "../hidden_oracles/ep-opaque1.oracle.json")

    with pytest.raises(PermissionError):
        resolve_agent_path(data_root, "/etc/passwd")


def test_runtime_settings_do_not_expose_an_oracle_root(tmp_path) -> None:
    from infineq.config import Settings

    settings = Settings.from_mapping({"INFINEQ_ORACLE_ROOT": "/tmp/hidden"}, project_root=tmp_path)

    assert not hasattr(settings, "oracle_root")
