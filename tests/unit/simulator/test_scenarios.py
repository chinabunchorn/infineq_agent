import json

from infineq.simulator.emitters import emit_episode_artifacts
from infineq.simulator.engine import simulate
from infineq.simulator.manifests import ArtifactChecksum
from infineq.simulator.scenarios import ScenarioFamily, ScenarioVariant, scenario_catalog


def test_catalog_contains_three_controlled_variants_for_each_of_eight_templates() -> None:
    cases = scenario_catalog()

    assert len(cases) == 24
    assert {case.variant for case in cases} == set(ScenarioVariant)
    assert {case.family for case in cases} == set(ScenarioFamily)
    assert sum(case.split.value == "development" for case in cases) == 8
    assert sum(case.split.value == "held_out" for case in cases) == 16
    assert len({case.episode_id for case in cases}) == 24


def test_variant_b_and_c_change_numeric_parameters_without_changing_template_family() -> None:
    cases = scenario_catalog()

    for family in ScenarioFamily:
        variants = [case for case in cases if case.family is family]
        assert {case.variant for case in variants} == set(ScenarioVariant)
        assert len({case.seed for case in variants}) == 3
        assert len({case.onset_s for case in variants}) >= 2


def test_observed_case_metadata_contains_no_hidden_family_or_oracle_fields() -> None:
    for case in scenario_catalog():
        observed = case.observed_manifest(
            artifact_checksums=(ArtifactChecksum(path="request_events.jsonl", sha256="a" * 64),)
        )
        serialized = json.dumps(observed.model_dump(mode="json"), sort_keys=True)

        assert "queue_saturation" not in serialized
        assert "backend_slowdown" not in serialized
        assert "injected_family" not in serialized
        assert "mechanism" not in serialized
        assert "oracle" not in serialized


def test_scenario_evidence_patterns_are_distinct(tmp_path) -> None:
    outputs: dict[ScenarioFamily, set[str]] = {}
    for case in scenario_catalog():
        episode_dir = tmp_path / case.episode_id
        emitted = emit_episode_artifacts(
            simulate(case.simulation_config),
            episode_id=case.episode_id,
            output_dir=episode_dir,
            telemetry=case.telemetry,
        )
        assert emitted.checksums
        signals = {
            json.loads(line)["signal"]
            for line in (episode_dir / "service_metrics.jsonl").read_text().splitlines()
        }
        outputs.setdefault(case.family, set()).update(signals)

    assert "queue_depth" in outputs[ScenarioFamily.QUEUE_SATURATION]
    assert "itl" in outputs[ScenarioFamily.BACKEND_SLOWDOWN]
    assert "input_tokens" in outputs[ScenarioFamily.PROMPT_LENGTH_SHIFT]
    assert "replicas" not in outputs[ScenarioFamily.HEALTHY]


def test_each_template_has_its_declared_independent_observation_pattern(tmp_path) -> None:
    for case in scenario_catalog():
        episode_dir = tmp_path / case.episode_id
        emit_episode_artifacts(
            simulate(case.simulation_config),
            episode_id=case.episode_id,
            output_dir=episode_dir,
            telemetry=case.telemetry,
        )
        service = [
            json.loads(line)
            for line in (episode_dir / "service_metrics.jsonl").read_text().splitlines()
        ]
        requests = [
            json.loads(line)
            for line in (episode_dir / "request_events.jsonl").read_text().splitlines()
        ]
        infra = [
            json.loads(line)
            for line in (episode_dir / "infrastructure_events.jsonl").read_text().splitlines()
        ]
        if case.family is ScenarioFamily.BACKEND_SLOWDOWN:
            assert max(record["value"] for record in service if record["signal"] == "itl") > 50.0
        elif case.family is ScenarioFamily.BACKEND_ERRORS:
            assert (
                max(record["value"] for record in service if record["signal"] == "error_rate")
                > 0.05
            )
            assert any(record["status"] == "error" for record in requests)
        elif case.family is ScenarioFamily.PROMPT_LENGTH_SHIFT:
            assert (
                max(record["value"] for record in service if record["signal"] == "input_tokens")
                > 300.0
            )
            assert (
                max(record["value"] for record in service if record["signal"] == "prefill") > 300.0
            )
        elif case.family is ScenarioFamily.REPLICA_RESTART:
            assert any(record["ready_replicas"] == 0 for record in infra)
        elif case.family is ScenarioFamily.MISSING_OR_CONTRADICTORY:
            assert (
                "itl" not in {record["signal"] for record in service}
                or any(record["freshness_s"] > 10 for record in service)
                or any(record["ready_replicas"] > record["desired_replicas"] for record in infra)
            )
        elif case.family in {ScenarioFamily.HEALTHY, ScenarioFamily.BENIGN_BURST}:
            queue_wait = [record["value"] for record in service if record["signal"] == "queue_wait"]
            assert max(queue_wait) == 0.0
        elif case.family is ScenarioFamily.QUEUE_SATURATION:
            queue_wait = [record["value"] for record in service if record["signal"] == "queue_wait"]
            assert max(queue_wait) > 0.0
            assert max(record["value"] for record in service if record["signal"] == "ttft") > 300.0
            assert {record["value"] for record in service if record["signal"] == "itl"} == {50.0}
            assert {record["revision"] for record in infra} == {"sim-v1"}
            assert all(record["ready_replicas"] == 1 for record in infra)
