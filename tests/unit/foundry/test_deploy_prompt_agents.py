from __future__ import annotations

import json
from pathlib import Path

import pytest

from infineq.agents.prompt_manifest import PROMPT_CONSTRAINTS, PromptManifest
from infineq.foundry import deployment_cli
from infineq.foundry.agent_registry import AgentVersionRecord
from infineq.foundry.deployment_cli import main


def test_deployment_script_fails_honestly_when_foundry_prerequisites_are_absent(
    tmp_path: Path,
    capsys: object,
) -> None:
    exit_code = main(["--project-root", str(tmp_path)])

    assert exit_code == 2
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert "BLOCKED" in captured.err
    assert "https://" not in captured.err
    assert "secret" not in captured.err.casefold()


def test_deployment_script_retrieval_uses_only_the_exact_local_version(tmp_path: Path) -> None:
    local = tmp_path / ".local" / "foundry"
    local.mkdir(parents=True)
    (local / "investigator_deployment.json").write_text(
        json.dumps({"agent_name": "infineq-investigator", "agent_version": "7"}),
        encoding="utf-8",
    )

    assert deployment_cli._expected_version(tmp_path, None) == "7"


def test_deployment_script_creates_a_new_version_after_prompt_hash_changes(tmp_path: Path) -> None:
    local = tmp_path / ".local" / "foundry"
    local.mkdir(parents=True)
    (local / "investigator_deployment.json").write_text(
        json.dumps(
            {
                "agent_name": "infineq-investigator",
                "agent_version": "7",
                "prompt_version": "investigator-v1",
                "prompt_sha256": "b" * 64,
            }
        ),
        encoding="utf-8",
    )

    current = PromptManifest(
        version="investigator-v2",
        prompt_sha256="a" * 64,
        prompt_path="prompt.md",
        constraints=PROMPT_CONSTRAINTS,
    )

    assert deployment_cli._expected_version(tmp_path, None, current) is None


def test_expected_version_uses_an_explicit_version_before_reading_local_state(
    tmp_path: Path,
) -> None:
    assert deployment_cli._expected_version(tmp_path, "14") == "14"


@pytest.mark.parametrize(
    "contents",
    [
        "not-json",
        json.dumps({"agent_name": "other-agent", "agent_version": "14"}),
        json.dumps({"agent_name": "infineq-investigator"}),
    ],
)
def test_expected_version_rejects_unusable_local_state(tmp_path: Path, contents: str) -> None:
    path = tmp_path / ".local" / "foundry" / "investigator_deployment.json"
    path.parent.mkdir(parents=True)
    path.write_text(contents, encoding="utf-8")

    with pytest.raises(RuntimeError, match="manifest"):
        deployment_cli._expected_version(tmp_path, None)


def test_deployment_script_success_path_only_writes_the_local_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prompt_manifest = PromptManifest(
        version="investigator-v14",
        prompt_sha256="a" * 64,
        prompt_path="prompt.md",
        constraints=PROMPT_CONSTRAINTS,
    )
    record = AgentVersionRecord(
        name="infineq-investigator",
        version="14",
        prompt_version=prompt_manifest.version,
        prompt_sha256=prompt_manifest.prompt_sha256,
        model_deployment_name="model-under-test",
    )
    projects: list[object] = []

    class FakeProject:
        def __init__(self, **_kwargs: object) -> None:
            self.agents = object()
            self.closed = False
            projects.append(self)

        def close(self) -> None:
            self.closed = True

    class FakeRegistry:
        def __init__(self, _operations: object, *, project_root: Path) -> None:
            assert project_root == tmp_path

        def ensure_version(self, **kwargs: object) -> AgentVersionRecord:
            assert kwargs["model_deployment_name"] == "model-under-test"
            assert kwargs["manifest"] is prompt_manifest
            assert kwargs["expected_version"] is None
            return record

    monkeypatch.setattr(
        deployment_cli,
        "_environment",
        lambda _project_root: {
            "AZURE_AI_PROJECT_ENDPOINT": "https://test.services.ai.azure.com/api/projects/p",
            "AZURE_AI_MODEL_DEPLOYMENT_NAME": "model-under-test",
        },
    )
    monkeypatch.setattr(
        deployment_cli,
        "load_prompt_manifest",
        lambda project_root: prompt_manifest,
    )
    monkeypatch.setattr(deployment_cli, "DefaultAzureCredential", lambda **_kwargs: object())
    monkeypatch.setattr(deployment_cli, "AIProjectClient", FakeProject)
    monkeypatch.setattr(deployment_cli, "PromptAgentRegistry", FakeRegistry)

    exit_code = main(["--project-root", str(tmp_path)])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    assert payload["agent_version"] == "14"
    assert payload["previous_versions_deleted"] is False
    assert (tmp_path / ".local" / "foundry" / "investigator_deployment.json").exists()
    assert projects and all(project.closed for project in projects)
