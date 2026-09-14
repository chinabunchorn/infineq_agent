from pathlib import Path

import pytest

from infineq.errors import DataMissingError
from infineq.evidence.store import EvidenceArtifact, EvidenceStore

PROJECT_ROOT = Path(__file__).parents[3]
EPISODE_ID = "ep-a91e7c"


def test_store_loads_only_typed_observed_episode_artifacts() -> None:
    store = EvidenceStore(project_root=PROJECT_ROOT)

    episode = store.load_episode(EPISODE_ID)

    assert episode.manifest.episode_id == EPISODE_ID
    assert episode.manifest.data_origin == "synthetic"
    assert episode.requests
    assert episode.service_metrics
    assert episode.infrastructure_events
    assert episode.change_events
    assert episode.evidence_index
    assert all("path" not in record for record in episode.service_metrics)


def test_store_missing_artifact_is_typed_data_missing() -> None:
    store = EvidenceStore(project_root=PROJECT_ROOT)

    with pytest.raises(DataMissingError) as error:
        store.read_artifact("ep-does-not-exist", EvidenceArtifact.MANIFEST)

    assert error.value.code.value == "data_missing"


def test_store_resolves_evidence_only_from_the_same_episode() -> None:
    store = EvidenceStore(project_root=PROJECT_ROOT)
    evidence_id = store.load_episode(EPISODE_ID).evidence_index[0].evidence_id

    record = store.get_evidence(EPISODE_ID, evidence_id)

    assert record.evidence_id == evidence_id
    assert record.episode_id == EPISODE_ID

    with pytest.raises(DataMissingError):
        store.get_evidence("ep-0e7ab3", evidence_id)


def test_store_rejects_raw_artifact_names_and_url_like_inputs() -> None:
    store = EvidenceStore(project_root=PROJECT_ROOT)

    for artifact in ("manifest.json", "../../hidden_oracles/secret.json", "https://example.test"):
        with pytest.raises(PermissionError):
            store.read_artifact(EPISODE_ID, artifact)
