import json

from infineq.simulator.emitters import emit_episode_artifacts
from infineq.simulator.engine import canonical_simulation_config, simulate

_REQUIRED_EVIDENCE_FIELDS = {
    "value",
    "unit",
    "source",
    "aggregation",
    "window",
    "freshness_s",
    "data_origin",
}


def test_every_observed_evidence_record_has_frozen_provenance_fields(tmp_path) -> None:
    result = simulate(canonical_simulation_config(seed=1001))
    emit_episode_artifacts(result, episode_id="ep-opaque1", output_dir=tmp_path)

    for filename in (
        "request_events.jsonl",
        "service_metrics.jsonl",
        "infrastructure_events.jsonl",
        "change_events.jsonl",
        "evidence_index.jsonl",
    ):
        records = [json.loads(line) for line in (tmp_path / filename).read_text().splitlines()]
        assert records
        assert all(record.keys() >= _REQUIRED_EVIDENCE_FIELDS for record in records), filename
        assert all(record["data_origin"] == "synthetic" for record in records)
