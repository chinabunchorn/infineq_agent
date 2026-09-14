from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import infineq.detection.detector as detector_module
from infineq.agents import development_runner
from infineq.agents.development_runner import (
    _no_incident_result,
    development_episode_ids,
    unseen_evidence_count,
    write_episode_artifacts,
)
from infineq.agents.investigator import FROZEN_INCIDENT_FAMILIES
from infineq.foundry.deployment import run_manifest_path
from infineq.schemas.investigation import Disposition, InvestigationResultV1


def test_development_selector_returns_exactly_eight_variant_a_episodes_without_oracles() -> None:
    episode_ids = development_episode_ids()

    assert len(episode_ids) == 8
    assert len(set(episode_ids)) == 8
    assert all(episode_id.startswith("ep-") for episode_id in episode_ids)


def test_episode_artifacts_preserve_safe_raw_and_redacted_outputs(tmp_path: Path) -> None:
    result = InvestigationResultV1(
        investigation_id="investigation-ep-61d8aa",
        incident_id="ep-61d8aa",
        completed_at=datetime(2026, 9, 14, 1, tzinfo=UTC),
        disposition=Disposition.ANALYSIS_INCOMPLETE,
        summary="The bounded test run did not produce a final diagnosis.",
        hypotheses=(),
        cited_evidence_ids=(),
        limitations=("No action was proposed.",),
    )

    manifest = write_episode_artifacts(
        tmp_path,
        run_id="run-20260914-010000",
        episode_id="ep-61d8aa",
        result=result,
        final_output='{"summary":"safe", "token":"super-secret-value"}',
        trace_records=({"status": "analysis_incomplete", "response_id": "resp-1"},),
        successful_tool_calls=0,
        forbidden_tool_calls=0,
        latency_ms=10.0,
        input_tokens=1,
        output_tokens=2,
        total_tokens=3,
        unseen_evidence_count=0,
        agent_name="infineq-investigator",
        agent_version="7",
        prompt_version="investigator-v1",
        prompt_sha256="a" * 64,
    )

    assert manifest.status == "analysis_incomplete"
    assert manifest.total_tokens == 3
    assert manifest.unseen_evidence_count == 0
    run_root = tmp_path / "data" / "infineq" / "v1" / "runs" / "run-20260914-010000"
    files = tuple(path for path in run_root.rglob("*") if path.is_file())
    assert {path.name for path in files} == {
        "raw_output.json",
        "redacted_output.json",
        "trace.json",
        "run_manifest.json",
    }
    assert "super-secret-value" not in (run_root / "ep-61d8aa" / "raw_output.json").read_text()
    assert "oracle" not in " ".join(path.read_text() for path in files).casefold()
    json.loads((run_root / "ep-61d8aa" / "redacted_output.json").read_text())


def test_unseen_evidence_count_is_computed_without_opening_oracles() -> None:
    output = json.dumps(
        {
            "cited_evidence_ids": [
                "ev:ep-61d8aa:service_metrics:w0:ttft:p95:aaaaaaaa",
                "ev:ep-61d8aa:service_metrics:w1:ttft:p95:bbbbbbbb",
            ]
        }
    )

    assert (
        unseen_evidence_count(
            output,
            {"ev:ep-61d8aa:service_metrics:w0:ttft:p95:aaaaaaaa"},
        )
        == 1
    )


def test_no_incident_result_names_all_frozen_families_without_an_action() -> None:
    result = _no_incident_result("ep-e35192")
    text = " ".join((result.summary, *result.limitations))

    assert result.proposed_action is None
    for family in FROZEN_INCIDENT_FAMILIES:
        assert family in text


def test_environment_combines_dotenv_and_process_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".env").write_text("FROM_DOTENV=dotenv-value\n", encoding="utf-8")
    monkeypatch.setenv("FROM_PROCESS", "process-value")

    values = development_runner._environment(tmp_path)

    assert values["FROM_DOTENV"] == "dotenv-value"
    assert values["FROM_PROCESS"] == "process-value"


@pytest.mark.parametrize(
    "output",
    [None, "[]", '{"cited_evidence_ids":"not-a-list"}'],
)
def test_unseen_evidence_count_ignores_malformed_output(output: object) -> None:
    assert development_runner.unseen_evidence_count(output, set()) == 0  # type: ignore[arg-type]


def test_deployed_version_reads_the_exact_local_manifest(tmp_path: Path) -> None:
    path = tmp_path / ".local" / "foundry" / "investigator_deployment.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"agent_name": "infineq-investigator", "agent_version": "14"}),
        encoding="utf-8",
    )

    assert development_runner._deployed_version(tmp_path) == "14"


@pytest.mark.parametrize(
    "contents",
    [
        "not-json",
        json.dumps({"agent_name": "other-agent", "agent_version": "14"}),
        json.dumps({"agent_name": "infineq-investigator", "agent_version": 14}),
    ],
)
def test_deployed_version_rejects_unusable_manifests(tmp_path: Path, contents: str) -> None:
    path = tmp_path / ".local" / "foundry" / "investigator_deployment.json"
    path.parent.mkdir(parents=True)
    path.write_text(contents, encoding="utf-8")

    with pytest.raises(RuntimeError, match="deployment manifest"):
        development_runner._deployed_version(tmp_path)


def test_deployed_version_rejects_a_missing_manifest(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="deployment manifest"):
        development_runner._deployed_version(tmp_path)


def test_development_main_rejects_non_development_episode(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = development_runner.main(
        ["--project-root", str(tmp_path), "--episode-id", "ep-held-out"]
    )

    assert exit_code == 2
    assert "only development-visible" in capsys.readouterr().err


def test_development_main_writes_no_incident_and_investigator_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    deployment_path = tmp_path / ".local" / "foundry" / "investigator_deployment.json"
    deployment_path.parent.mkdir(parents=True)
    deployment_path.write_text(
        json.dumps({"agent_name": "infineq-investigator", "agent_version": "14"}),
        encoding="utf-8",
    )
    record = SimpleNamespace(
        name="infineq-investigator",
        version="14",
        prompt_version="investigator-v14",
        prompt_sha256="a" * 64,
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

        def ensure_version(self, **_kwargs: object) -> object:
            return record

    class FakeResponses:
        def __init__(self, _project: object, *, agent_name: str, agent_version: str) -> None:
            assert (agent_name, agent_version) == (record.name, record.version)

    class FakeStore:
        def __init__(self, *, project_root: Path) -> None:
            assert project_root == tmp_path

    class FakeTools:
        def __init__(self, *, store: object) -> None:
            assert isinstance(store, FakeStore)

    class FakeDetector:
        def __init__(self, *, store: object) -> None:
            assert isinstance(store, FakeStore)

        def run(self, episode_id: str) -> SimpleNamespace:
            packet = None if episode_id == "ep-e35192" else object()
            return SimpleNamespace(packet=packet)

    class FakeInvestigator:
        def __init__(self, **_kwargs: object) -> None:
            result = _no_incident_result("ep-61d8aa")
            self._result = result
            self.last_run = SimpleNamespace(
                traces=(
                    SimpleNamespace(
                        response_id="resp-1",
                        call_id="call-1",
                        name="get_incident_packet",
                        status="completed",
                        retry_count=0,
                        returned_evidence_ids=(),
                    ),
                ),
                successful_tool_calls=1,
                usage=(1, 2, 3),
                returned_evidence_ids=frozenset(),
                forbidden_tool_calls=0,
                final_output_text=result.model_dump_json(),
            )

        def investigate(self, _packet: object) -> InvestigationResultV1:
            return self._result

    monkeypatch.setattr(
        development_runner,
        "_environment",
        lambda _project_root: {
            "AZURE_AI_PROJECT_ENDPOINT": "https://test.services.ai.azure.com/api/projects/p",
            "AZURE_AI_MODEL_DEPLOYMENT_NAME": "model-under-test",
        },
    )
    monkeypatch.setattr(
        development_runner,
        "load_prompt_manifest",
        lambda project_root: SimpleNamespace(
            version="investigator-v14", prompt_sha256="a" * 64, project_root=project_root
        ),
    )
    monkeypatch.setattr(development_runner, "DefaultAzureCredential", lambda **_kwargs: object())
    monkeypatch.setattr(development_runner, "AIProjectClient", FakeProject)
    monkeypatch.setattr(development_runner, "PromptAgentRegistry", FakeRegistry)
    monkeypatch.setattr(development_runner, "AzureFoundryResponsesClient", FakeResponses)
    monkeypatch.setattr(development_runner, "EvidenceStore", FakeStore)
    monkeypatch.setattr(development_runner, "InvestigatorTools", FakeTools)
    monkeypatch.setattr(development_runner, "Investigator", FakeInvestigator)
    monkeypatch.setattr(detector_module, "Detector", FakeDetector)

    exit_code = development_runner.main(
        [
            "--project-root",
            str(tmp_path),
            "--episode-id",
            "ep-e35192",
            "--episode-id",
            "ep-61d8aa",
            "--run-id-prefix",
            "run-test",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert [item["episode_id"] for item in payload["episodes"]] == [
        "ep-e35192",
        "ep-61d8aa",
    ]
    assert payload["episodes"][0]["successful_tool_calls"] == 0
    assert payload["episodes"][1]["successful_tool_calls"] == 1
    assert run_manifest_path(tmp_path, "run-test-ep-e35192").exists()
    assert run_manifest_path(tmp_path, "run-test-ep-61d8aa").exists()
    assert projects and all(project.closed for project in projects)
