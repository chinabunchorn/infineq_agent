from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from infineq.agents.prompt_manifest import PROMPT_CONSTRAINTS, PromptManifest
from infineq.foundry import deployment_cli
from infineq.foundry.agent_registry import (
    VERIFIER_AGENT_NAME,
    AgentVersionRecord,
    VerifierAgentVersionRecord,
)
from infineq.foundry.deployment_cli import build_parser, main
from infineq.foundry.verifier_tools import VERIFIER_TOOL_SCHEMA_SHA256


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


def test_deployment_cli_can_select_the_verifier_agent() -> None:
    args = build_parser().parse_args(["--agent", "verifier"])

    assert args.agent == "verifier"


def test_deployment_cli_rejects_unallowlisted_agent_modes() -> None:
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["--agent", "executor"])

    assert error.value.code == 2


def test_verifier_prompt_upgrade_requires_an_explicit_new_version_flag(tmp_path: Path) -> None:
    from infineq.foundry.deployment import write_verifier_deployment_manifest

    existing = VerifierAgentVersionRecord(
        name=VERIFIER_AGENT_NAME,
        version="1",
        prompt_version="verifier-v1",
        prompt_sha256="a" * 64,
        model_deployment_name="model-under-test",
        tool_schema_sha256=VERIFIER_TOOL_SCHEMA_SHA256,
        created_at=datetime(2026, 9, 14, tzinfo=UTC),
    )
    write_verifier_deployment_manifest(tmp_path, existing)
    new_prompt = PromptManifest(
        version="verifier-v2",
        prompt_sha256="b" * 64,
        prompt_path="new-prompt.md",
        constraints=PROMPT_CONSTRAINTS,
    )
    with pytest.raises(RuntimeError, match="prompt_version"):
        deployment_cli._expected_verifier_version(tmp_path, None, new_prompt, "model-under-test")
    assert deployment_cli._expected_verifier_version(
        tmp_path, None, new_prompt, "model-under-test", allow_new_version=True
    ) == (None, None)
    with pytest.raises(RuntimeError, match="cannot combine"):
        deployment_cli._expected_verifier_version(
            tmp_path, "1", new_prompt, "model-under-test", allow_new_version=True
        )


def test_verifier_upgrade_rejects_changed_hash_under_same_prompt_label(tmp_path: Path) -> None:
    from infineq.foundry.deployment import write_verifier_deployment_manifest

    existing = VerifierAgentVersionRecord(
        name=VERIFIER_AGENT_NAME,
        version="2",
        prompt_version="verifier-v2",
        prompt_sha256="a" * 64,
        model_deployment_name="model-under-test",
        tool_schema_sha256=VERIFIER_TOOL_SCHEMA_SHA256,
        created_at=datetime(2026, 9, 14, tzinfo=UTC),
    )
    write_verifier_deployment_manifest(tmp_path, existing)
    changed = PromptManifest(
        version="verifier-v2",
        prompt_sha256="b" * 64,
        prompt_path="changed-prompt.md",
        constraints=PROMPT_CONSTRAINTS,
    )
    with pytest.raises(RuntimeError, match="prompt version must change"):
        deployment_cli._expected_verifier_version(
            tmp_path, None, changed, "model-under-test", allow_new_version=True
        )


def test_deployment_script_verifier_success_uses_verifier_manifest_and_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prompt_manifest = PromptManifest(
        version="verifier-v1",
        prompt_sha256="a" * 64,
        prompt_path="prompt.md",
        constraints=PROMPT_CONSTRAINTS,
    )
    timestamp = datetime(2026, 9, 14, 19, 0, tzinfo=timezone(timedelta(hours=7)))
    record = VerifierAgentVersionRecord(
        name=VERIFIER_AGENT_NAME,
        version="1",
        prompt_version=prompt_manifest.version,
        prompt_sha256=prompt_manifest.prompt_sha256,
        model_deployment_name="model-under-test",
        tool_schema_sha256=VERIFIER_TOOL_SCHEMA_SHA256,
        created_at=timestamp,
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

        def ensure_verifier_version(self, **kwargs: object) -> VerifierAgentVersionRecord:
            assert kwargs["model_deployment_name"] == "model-under-test"
            assert kwargs["manifest"] is prompt_manifest
            assert kwargs["expected_version"] is None
            assert kwargs["creation_timestamp"] is None
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
        "load_verifier_prompt_manifest",
        lambda project_root: prompt_manifest,
        raising=False,
    )
    monkeypatch.setattr(deployment_cli, "DefaultAzureCredential", lambda **_kwargs: object())
    monkeypatch.setattr(deployment_cli, "AIProjectClient", FakeProject)
    monkeypatch.setattr(deployment_cli, "PromptAgentRegistry", FakeRegistry)

    exit_code = main(["--agent", "verifier", "--project-root", str(tmp_path)])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    assert payload["agent_name"] == VERIFIER_AGENT_NAME
    assert payload["agent_version"] == "1"
    assert payload["tool_schema_sha256"] == VERIFIER_TOOL_SCHEMA_SHA256
    assert payload["created_at"] == "2026-09-14T12:00:00Z"
    assert payload["previous_versions_deleted"] is False
    assert (tmp_path / ".local" / "foundry" / "verifier_deployment.json").exists()
    assert projects and all(project.closed for project in projects)
