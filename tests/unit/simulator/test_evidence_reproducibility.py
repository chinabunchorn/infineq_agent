from infineq.simulator.emitters import emit_episode_artifacts
from infineq.simulator.engine import canonical_simulation_config, simulate


def test_evidence_ids_and_artifact_bytes_ignore_output_directory(tmp_path) -> None:
    result = simulate(canonical_simulation_config(seed=1001))
    left = emit_episode_artifacts(
        result,
        episode_id="ep-opaque1",
        output_dir=tmp_path / "left",
    )
    right = emit_episode_artifacts(
        result,
        episode_id="ep-opaque1",
        output_dir=tmp_path / "right",
    )

    assert left.checksums == right.checksums
    assert (tmp_path / "left" / "evidence_index.jsonl").read_bytes() == (
        tmp_path / "right" / "evidence_index.jsonl"
    ).read_bytes()
