"""Generate the frozen Infineq v1 synthetic replay corpus."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from infineq.simulator.emitters import emit_episode_artifacts
from infineq.simulator.engine import simulate
from infineq.simulator.manifests import ArtifactChecksum, CorpusEpisode, CorpusManifest
from infineq.simulator.oracle_writer import write_hidden_oracle
from infineq.simulator.scenarios import scenario_catalog
from infineq.simulator.serialization import write_stable_json

_DATASET_README = """# Infineq v1 synthetic replay corpus

This directory contains a deterministic, synthetic incident-replay and evaluation corpus.
It is not a training dataset and it is not a measurement of vLLM, a GPU, EKS, Kubernetes,
or any production service.

- Every episode is generated and replayed from fixed simulator parameters and a seed.
- Request artifacts contain no customer prompt content or completion text.
- `observed/` is the only tree mounted by the agent-readable runtime.
- `hidden_oracles/` is evaluator-only answer-key data and is never exposed to agents or tools.
- Timing is a queue-model simulation, not a vLLM/GPU/EKS benchmark.
- Public BurstGPT is excluded from v1; it may be considered later only as a workload-shape input.
- Synthetic/replay data must never be pooled with future live data.
- The frozen corpus contains 8 development episodes and 16 held-out episodes.

Artifact hashes are content hashes. Filesystem paths are not part of evidence identity hashes.
"""


@dataclass(frozen=True, slots=True)
class GenerationSummary:
    """Public generation and reproducibility evidence."""

    episode_count: int
    development_count: int
    held_out_count: int
    observed_tree_sha256: str
    reproducible: bool

    def to_record(self) -> dict[str, object]:
        return {
            "episode_count": self.episode_count,
            "development_count": self.development_count,
            "held_out_count": self.held_out_count,
            "observed_tree_sha256": self.observed_tree_sha256,
            "reproducible": self.reproducible,
        }


def _reset_generated_roots(output_root: Path) -> None:
    for name in ("observed", "hidden_oracles", "knowledge"):
        target = output_root / name
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)


def _write_static_files(output_root: Path) -> None:
    (output_root / "README.md").write_text(_DATASET_README, encoding="utf-8")
    write_stable_json(
        output_root / "knowledge" / "source_manifest.json",
        {
            "schema_version": "1.0",
            "corpus_version": "v1",
            "design_version": "1.0",
            "source_type": "synthetic_replay",
            "source_id": "infineq-design-freeze-v1",
            "source_url": "local-design-freeze-v1",
            "retrieval_date": "2026-09-13",
            "notes": [
                "No production telemetry or customer prompts are included.",
                "Queue timings are simulator parameters, not hardware measurements.",
            ],
        },
    )


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(path for path in root.rglob("*") if path.is_file()):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _generate_once(output_root: Path) -> GenerationSummary:
    output_root.mkdir(parents=True, exist_ok=True)
    _reset_generated_roots(output_root)
    _write_static_files(output_root)
    entries: list[CorpusEpisode] = []
    cases = scenario_catalog()
    for case in cases:
        result = simulate(case.simulation_config)
        episode_dir = output_root / "observed" / case.episode_id
        emitted = emit_episode_artifacts(
            result,
            episode_id=case.episode_id,
            output_dir=episode_dir,
            telemetry=case.telemetry,
        )
        checksums = tuple(
            ArtifactChecksum(path=path, sha256=checksum)
            for path, checksum in sorted(emitted.checksums.items())
        )
        manifest = case.observed_manifest(artifact_checksums=checksums)
        manifest_hash = write_stable_json(
            episode_dir / "manifest.json", manifest.model_dump(mode="json")
        )
        write_hidden_oracle(case, output_root / "hidden_oracles")
        entries.append(
            CorpusEpisode(
                episode_id=case.episode_id,
                split=case.split,
                seed=case.seed,
                manifest_sha256=manifest_hash,
                artifact_checksums=checksums,
            )
        )
    corpus = CorpusManifest(
        simulator_version="sim-v1",
        observed_tree_sha256=_tree_hash(output_root / "observed"),
        episodes=tuple(sorted(entries, key=lambda item: item.episode_id)),
    )
    write_stable_json(output_root / "corpus_manifest.json", corpus.model_dump(mode="json"))
    return GenerationSummary(
        episode_count=len(entries),
        development_count=corpus.development_count,
        held_out_count=corpus.held_out_count,
        observed_tree_sha256=_tree_hash(output_root / "observed"),
        reproducible=False,
    )


def generate_corpus(output_root: Path, *, verify_reproducible: bool = False) -> GenerationSummary:
    """Generate v1 and optionally regenerate it in a temporary directory for comparison."""

    summary = _generate_once(output_root)
    if not verify_reproducible:
        return summary
    output_root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="infineq-repro-", dir=output_root.parent) as temporary:
        regenerated = _generate_once(Path(temporary))
        reproducible = (
            summary.observed_tree_sha256 == regenerated.observed_tree_sha256
            and summary.episode_count == regenerated.episode_count
            and summary.development_count == regenerated.development_count
            and summary.held_out_count == regenerated.held_out_count
        )
    return replace(summary, reproducible=reproducible)
