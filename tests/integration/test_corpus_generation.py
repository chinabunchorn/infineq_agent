import hashlib
import json

from infineq.simulator.generator import generate_corpus


def test_generator_writes_exact_split_counts_and_separate_stores(tmp_path) -> None:
    summary = generate_corpus(tmp_path / "v1", verify_reproducible=True)

    assert summary.episode_count == 24
    assert summary.development_count == 8
    assert summary.held_out_count == 16
    assert summary.reproducible is True
    assert len(list((tmp_path / "v1" / "observed").iterdir())) == 24
    assert len(list((tmp_path / "v1" / "hidden_oracles").glob("*.oracle.json"))) == 24

    corpus = json.loads((tmp_path / "v1" / "corpus_manifest.json").read_text())
    assert corpus["development_count"] == 8
    assert corpus["held_out_count"] == 16
    assert all("scenario_family" not in entry for entry in corpus["episodes"])


def test_generator_observed_tree_contains_no_oracle_labels_or_keys(tmp_path) -> None:
    generate_corpus(tmp_path / "v1")
    observed = tmp_path / "v1" / "observed"
    forbidden = (
        "queue_saturation",
        "backend_slowdown",
        "injected_family",
        "mechanism",
        "oracle",
        "expected_leading_family",
    )

    for path in observed.rglob("*"):
        if path.is_file():
            text = path.read_text()
            assert not any(word in text for word in forbidden), path


def test_corpus_manifest_hashes_match_every_observed_artifact(tmp_path) -> None:
    root = tmp_path / "v1"
    generate_corpus(root)
    corpus = json.loads((root / "corpus_manifest.json").read_text())

    for entry in corpus["episodes"]:
        episode_dir = root / "observed" / entry["episode_id"]
        manifest_bytes = (episode_dir / "manifest.json").read_bytes()
        assert hashlib.sha256(manifest_bytes).hexdigest() == entry["manifest_sha256"]
        for artifact in entry["artifact_checksums"]:
            payload = (episode_dir / artifact["path"]).read_bytes()
            assert hashlib.sha256(payload).hexdigest() == artifact["sha256"]


def test_corpus_manifest_persists_the_observed_tree_hash(tmp_path) -> None:
    root = tmp_path / "v1"
    generate_corpus(root)
    corpus = json.loads((root / "corpus_manifest.json").read_text())
    digest = hashlib.sha256()
    for path in sorted(path for path in (root / "observed").rglob("*") if path.is_file()):
        digest.update(path.relative_to(root / "observed").as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())

    assert corpus["observed_tree_sha256"] == digest.hexdigest()


def test_generator_removes_stale_generated_knowledge(tmp_path) -> None:
    root = tmp_path / "v1"
    stale = root / "knowledge" / "stale.txt"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale")

    generate_corpus(root)

    assert not stale.exists()
