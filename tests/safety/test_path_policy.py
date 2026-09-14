from pathlib import Path

import pytest

from infineq.evidence.paths import PathPolicy


def test_path_policy_anchors_everything_under_fixed_observed_root(tmp_path: Path) -> None:
    policy = PathPolicy(project_root=tmp_path)

    assert policy.observed_root == (tmp_path / "data/infineq/v1/observed").resolve()
    assert policy.episode_directory("ep-opaque1").is_relative_to(policy.observed_root)


def test_path_policy_rejects_traversal_absolute_paths_and_unknown_artifacts(tmp_path: Path) -> None:
    policy = PathPolicy(project_root=tmp_path)

    with pytest.raises(PermissionError):
        policy.episode_directory("../hidden_oracles")
    with pytest.raises(PermissionError):
        policy.artifact_path("ep-opaque1", "../../hidden_oracles/secret.json")
    with pytest.raises(PermissionError):
        policy.artifact_path("ep-opaque1", "/etc/passwd")
    with pytest.raises(PermissionError):
        policy.artifact_path("ep-opaque1", "arbitrary.json")


def test_path_policy_rejects_non_opaque_episode_ids(tmp_path: Path) -> None:
    policy = PathPolicy(project_root=tmp_path)

    for episode_id in (
        "queue_saturation",
        "ep-../escape",
        "ep-AABB",
        "ep-",
        "ep-" + "a" * 65,
    ):
        with pytest.raises(PermissionError):
            policy.episode_directory(episode_id)


def test_path_policy_rejects_symlinked_episode_directory_outside_observed_root(
    tmp_path: Path,
) -> None:
    policy = PathPolicy(project_root=tmp_path)
    outside = tmp_path / "hidden_oracles"
    outside.mkdir()
    policy.observed_root.mkdir(parents=True)
    (policy.observed_root / "ep-opaque1").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PermissionError):
        policy.episode_directory("ep-opaque1")


def test_path_policy_rejects_symlinked_observed_root(tmp_path: Path) -> None:
    observed_parent = tmp_path / "data" / "infineq" / "v1"
    observed_parent.mkdir(parents=True)
    (tmp_path / "hidden_oracles").mkdir()
    (observed_parent / "observed").symlink_to(
        tmp_path / "hidden_oracles",
        target_is_directory=True,
    )

    with pytest.raises(PermissionError):
        PathPolicy(project_root=tmp_path)


def test_path_policy_rejects_symlink_loops(tmp_path: Path) -> None:
    policy = PathPolicy(project_root=tmp_path)
    policy.observed_root.mkdir(parents=True)
    (policy.observed_root / "ep-opaque1").symlink_to("ep-opaque1")

    with pytest.raises(PermissionError):
        policy.episode_directory("ep-opaque1")
