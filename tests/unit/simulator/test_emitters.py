import json

from infineq.simulator.emitters import TelemetryProfile, emit_episode_artifacts
from infineq.simulator.engine import canonical_simulation_config, simulate


def test_emitters_write_all_frozen_observed_artifacts_with_evidence_index(tmp_path) -> None:
    result = simulate(canonical_simulation_config(seed=1001))

    emitted = emit_episode_artifacts(
        result,
        episode_id="ep-opaque1",
        output_dir=tmp_path,
    )

    assert set(emitted.checksums) == {
        "request_events.jsonl",
        "service_metrics.jsonl",
        "infrastructure_events.jsonl",
        "change_events.jsonl",
        "evidence_index.jsonl",
    }
    index_lines = (tmp_path / "evidence_index.jsonl").read_text().splitlines()
    service_lines = (tmp_path / "service_metrics.jsonl").read_text().splitlines()
    assert len(index_lines) >= len(service_lines)
    assert all(json.loads(line)["evidence_id"].startswith("ev:ep-opaque1:") for line in index_lines)


def test_emitters_make_absent_changes_an_explicit_empty_result(tmp_path) -> None:
    result = simulate(canonical_simulation_config(seed=1001))

    emit_episode_artifacts(result, episode_id="ep-opaque1", output_dir=tmp_path)
    records = [
        json.loads(line) for line in (tmp_path / "change_events.jsonl").read_text().splitlines()
    ]

    assert records[0]["data_origin"] == "synthetic"
    assert records[0]["events"] == []
    assert records[0]["query_status"] == "empty"
    assert records[0]["source"] == "change_events"
    assert records[0]["window_start_s"] == 0.0
    assert records[0]["window_end_s"] == 180.0


def test_emitters_can_mark_required_signal_stale_without_fabricating_zero(tmp_path) -> None:
    result = simulate(canonical_simulation_config(seed=1001))

    emit_episode_artifacts(
        result,
        episode_id="ep-opaque1",
        output_dir=tmp_path,
        telemetry=TelemetryProfile(stale_signals=frozenset({"itl"})),
    )
    records = [
        json.loads(line)
        for line in (tmp_path / "service_metrics.jsonl").read_text().splitlines()
        if json.loads(line)["signal"] == "itl"
    ]

    assert records
    assert all(record["freshness_s"] > 10.0 for record in records)
    assert all(record["value"] is not None for record in records)
