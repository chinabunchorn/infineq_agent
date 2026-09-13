from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from infineq.simulator.manifests import (
    ArtifactChecksum,
    CorpusManifest,
    EpisodeSplit,
    ObservedManifest,
    PhaseManifest,
    ServiceManifest,
    WorkloadManifest,
    validate_episode_splits,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def make_observed(episode_id: str = "ep-opaque1", split: EpisodeSplit = EpisodeSplit.DEVELOPMENT):
    return ObservedManifest(
        episode_id=episode_id,
        simulator_version="sim-v1",
        design_version="1.0",
        split=split,
        seed=11,
        generated_at=NOW,
        duration_s=180,
        phases=(
            PhaseManifest(name="baseline", start_s=0, end_s=60),
            PhaseManifest(name="degradation", start_s=60, end_s=120),
            PhaseManifest(name="recovery", start_s=120, end_s=180),
        ),
        workload=WorkloadManifest(
            baseline_arrival_rate=1.2,
            degradation_arrival_rate=2.6,
            recovery_arrival_rate=1.2,
            input_tokens=256,
            output_tokens=64,
            jitter_fraction=0.05,
        ),
        service=ServiceManifest(
            initial_replicas=1,
            slots_per_replica=8,
            base_itl_s=0.05,
            prefill_base_s=0.10,
            prefill_per_input_s=0.00078,
            ttft_slo_ms=2000.0,
        ),
        artifact_checksums=(ArtifactChecksum(path="request_events.jsonl", sha256="a" * 64),),
    )


def test_observed_manifest_contains_frozen_replay_contract() -> None:
    manifest = make_observed()

    assert manifest.schema_version == "1.0"
    assert manifest.data_origin == "synthetic"
    assert manifest.execution_mode == "replay"
    assert manifest.split is EpisodeSplit.DEVELOPMENT
    assert manifest.phases[-1].end_s == 180
    assert manifest.service.slots_per_replica == 8


def test_observed_manifest_rejects_oracle_and_causal_fields() -> None:
    payload = make_observed().model_dump()
    payload["injected_family"] = "capacity_queueing"

    with pytest.raises(ValidationError):
        ObservedManifest.model_validate(payload)


def test_observed_manifest_rejects_noncanonical_phase_boundaries() -> None:
    payload = make_observed().model_dump()
    payload["phases"][1]["start_s"] = 59

    with pytest.raises(ValidationError):
        ObservedManifest.model_validate(payload)


def test_observed_manifest_requires_artifact_checksums() -> None:
    payload = make_observed().model_dump()
    payload["artifact_checksums"] = ()

    with pytest.raises(ValidationError):
        ObservedManifest.model_validate(payload)


def test_corpus_manifest_requires_exactly_one_split_per_episode() -> None:
    development = make_observed()
    held_out_same_id = make_observed(split=EpisodeSplit.HELD_OUT)

    with pytest.raises(ValueError, match="exactly one split"):
        validate_episode_splits((development, held_out_same_id))


def test_corpus_manifest_serializes_episode_entries_deterministically() -> None:
    first = make_observed("ep-opaque1")
    second = make_observed("ep-opaque2")
    corpus = CorpusManifest.from_observed((second, first))

    assert [episode.episode_id for episode in corpus.episodes] == ["ep-opaque1", "ep-opaque2"]
    assert corpus.development_count == 2
    assert corpus.held_out_count == 0
    assert corpus.model_dump_json() == corpus.model_dump_json()
