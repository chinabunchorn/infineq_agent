from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from infineq.evaluation import live_baselines

_SCRIPT_PATH = Path(__file__).parents[3] / "scripts" / "run_development_baselines.py"
_SCRIPT_SPEC = importlib.util.spec_from_file_location("run_development_baselines", _SCRIPT_PATH)
assert _SCRIPT_SPEC is not None and _SCRIPT_SPEC.loader is not None
run_development_baselines = importlib.util.module_from_spec(_SCRIPT_SPEC)
_SCRIPT_SPEC.loader.exec_module(run_development_baselines)


CANONICAL_RUN_ID = "run-phase5-canonical-live-v1"


def test_live_cli_dispatches_the_explicit_canonical_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[Path, str]] = []

    def fake_run_live_baseline(project_root: Path, run_id: str) -> dict[str, object]:
        calls.append((project_root, run_id))
        return {
            "run_id": run_id,
            "output_directory": "data/infineq/v1/runs/run-phase5-canonical-live-v1",
            "live_execution": True,
        }

    monkeypatch.setenv("INFIN_EQ_RUN_LIVE_BASELINES", "1")
    monkeypatch.setattr(
        run_development_baselines,
        "run_live_baseline",
        fake_run_live_baseline,
        raising=False,
    )

    exit_code = run_development_baselines.main(
        [
            "--live",
            "--project-root",
            str(tmp_path),
            "--run-id",
            CANONICAL_RUN_ID,
        ]
    )

    assert exit_code == 0
    assert calls == [(tmp_path.resolve(), CANONICAL_RUN_ID)]
    assert json.loads(capsys.readouterr().out) == {
        "live_execution": True,
        "output_directory": "data/infineq/v1/runs/run-phase5-canonical-live-v1",
        "run_id": CANONICAL_RUN_ID,
    }


def test_all_development_cli_requires_process_opt_in_not_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / ".env").write_text(
        "INFIN_EQ_RUN_LIVE_BASELINES=1\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("INFIN_EQ_RUN_LIVE_BASELINES", raising=False)
    calls: list[str] = []
    monkeypatch.setattr(
        run_development_baselines,
        "build_live_runtime",
        lambda *_args, **_kwargs: calls.append("build"),
    )

    exit_code = run_development_baselines.main(
        [
            "--all-development-live",
            "--project-root",
            str(tmp_path),
            "--run-id",
            "run-phase5-development-live-v1",
        ]
    )

    assert exit_code == 2
    assert calls == []
    captured = capsys.readouterr()
    assert "blocked" in captured.err.casefold()
    assert captured.out == ""


def test_all_development_cli_dispatches_once_with_exact_eight_and_redacted_aggregate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("INFIN_EQ_RUN_LIVE_BASELINES", "1")
    run_id = "run-phase5-development-live-v1"
    calls: list[tuple[Path, str, Path | None]] = []
    aggregate = {
        "episode_count": 8,
        "mode_record_count": 24,
        "episode_ids": ["ep-secret"],
        "pass_counts": {"static": 8, "investigator": 4, "investigator_verifier": 5},
        "unsupported_claims_by_mode": {
            "static": 0,
            "investigator": 3,
            "investigator_verifier": 1,
        },
        "policy_errors_by_mode": {
            "static": 0,
            "investigator": 2,
            "investigator_verifier": 0,
        },
        "two_agent_justification_passed": True,
        "action_execution_count": 0,
        "endpoint": "https://secret.example",
        "prompt": "secret prompt body",
        "response_ids": ["resp-secret"],
    }

    def fake_runner(
        project_root: Path,
        received_run_id: str,
        *,
        diagnostic_path: Path | None = None,
    ) -> dict[str, object]:
        calls.append((project_root, received_run_id, diagnostic_path))
        selected = live_baselines.select_development_episode_ids()
        assert len(selected) == 8
        assert "episode_ids" not in {"project_root", "run_id"}
        return {
            "live_execution": True,
            "scope": "all_development",
            "run_id": received_run_id,
            "output_directory": "data/infineq/v1/runs/run-phase5-development-live-v1",
            "aggregate_metrics": aggregate,
        }

    monkeypatch.setattr(
        run_development_baselines,
        "run_all_development_live_baseline",
        fake_runner,
        raising=False,
    )

    exit_code = run_development_baselines.main(
        [
            "--all-development-live",
            "--project-root",
            str(tmp_path),
            "--run-id",
            run_id,
        ]
    )

    assert exit_code == 0
    assert calls == [(tmp_path.resolve(), run_id, None)]
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["scope"] == "all_development"
    assert payload["episode_count"] == 8
    assert payload["mode_record_count"] == 24
    assert payload["pass_counts"] == aggregate["pass_counts"]
    assert payload["two_agent_justification_passed"] is True
    assert "ep-secret" not in output
    assert "secret.example" not in output
    assert "secret prompt body" not in output
    assert "resp-secret" not in output


@pytest.mark.parametrize(
    "extra_args",
    [
        ["--live"],
        ["--recorded-input", "recorded.json"],
        ["--run-id", "run-phase5-canonical-live-v1"],
        ["--run-id", "run-baseline-arbitrary"],
    ],
)
def test_all_development_cli_rejects_incompatible_modes_and_arbitrary_ids(
    extra_args: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("INFIN_EQ_RUN_LIVE_BASELINES", "1")
    calls: list[str] = []
    if extra_args[:1] != ["--run-id"]:
        monkeypatch.setattr(
            run_development_baselines,
            "run_all_development_live_baseline",
            lambda *_args, **_kwargs: calls.append("called"),
            raising=False,
        )

    arguments = [
        "--all-development-live",
        "--project-root",
        str(tmp_path),
        "--run-id",
        "run-phase5-development-live-v1",
    ]
    if extra_args[:1] == ["--run-id"]:
        arguments[arguments.index("--run-id") + 1] = extra_args[1]
    else:
        arguments.extend(extra_args)

    exit_code = run_development_baselines.main(arguments)

    assert exit_code == 2
    assert calls == []
    assert "blocked" in capsys.readouterr().err.casefold()


def test_all_development_live_helper_builds_one_runtime_and_calls_one_retrieval_only_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INFIN_EQ_RUN_LIVE_BASELINES", "1")
    run_id = "run-phase5-development-live-v2"
    expected_output = (tmp_path / "data" / "infineq" / "v1" / "runs" / run_id).resolve()
    calls: list[object] = []
    runtime = SimpleNamespace(
        store=object(),
        investigator=object(),
        verifier=object(),
        policy=object(),
        pins=None,
        close=lambda: calls.append("close"),
    )

    def fake_build(project_root: Path, **_kwargs: object) -> SimpleNamespace:
        calls.append(("build", project_root))
        return runtime

    class FakeDetector:
        def __init__(self, *, store: object) -> None:
            calls.append(("detector", store))

    aggregate = {
        "episode_count": 8,
        "mode_record_count": 24,
        "pass_counts": {"static": 8, "investigator": 4, "investigator_verifier": 5},
        "unsupported_claims_by_mode": {"static": 0, "investigator": 3, "investigator_verifier": 1},
        "policy_errors_by_mode": {"static": 0, "investigator": 2, "investigator_verifier": 0},
        "two_agent_justification_passed": True,
        "action_execution_count": 0,
        "prompt": "must not print",
        "response_ids": ["resp-secret"],
    }

    def fake_run_all(**kwargs: object) -> SimpleNamespace:
        calls.append(("run_all", kwargs))
        assert kwargs["project_root"] == tmp_path.resolve()
        assert kwargs["run_id"] == run_id
        assert kwargs["detector"] is not None
        assert kwargs["investigator"] is runtime.investigator
        assert kwargs["verifier"] is runtime.verifier
        assert kwargs["policy"] is runtime.policy
        assert kwargs["pins"] is None
        return SimpleNamespace(output_directory=expected_output, aggregate_metrics=aggregate)

    monkeypatch.setattr(run_development_baselines, "build_live_runtime", fake_build)
    monkeypatch.setattr(run_development_baselines, "Detector", FakeDetector)
    monkeypatch.setattr(run_development_baselines, "run_all_development_live_modes", fake_run_all)
    monkeypatch.setattr(
        run_development_baselines,
        "validate_live_artifacts",
        lambda output, received_run_id, **kwargs: calls.append(
            ("validate", output, received_run_id, kwargs)
        ),
    )

    payload = run_development_baselines.run_all_development_live_baseline(tmp_path, run_id)

    assert calls[0] == ("build", tmp_path.resolve())
    assert calls[1] == ("detector", runtime.store)
    assert sum(item[0] == "run_all" for item in calls if isinstance(item, tuple)) == 1
    assert calls[-1] == "close"
    assert payload["episode_count"] == 8
    assert payload["mode_record_count"] == 24
    serialized = json.dumps(payload, sort_keys=True)
    assert "must not print" not in serialized
    assert "resp-secret" not in serialized


def test_all_development_live_helper_rejects_preexisting_output_before_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INFIN_EQ_RUN_LIVE_BASELINES", "1")
    run_id = "run-phase5-development-live-v3"
    output = tmp_path / "data" / "infineq" / "v1" / "runs" / run_id
    output.mkdir(parents=True)
    calls: list[str] = []
    monkeypatch.setattr(
        run_development_baselines,
        "build_live_runtime",
        lambda *_args, **_kwargs: calls.append("build"),
    )

    with pytest.raises(ValueError, match="already exists"):
        run_development_baselines.run_all_development_live_baseline(tmp_path, run_id)

    assert calls == []


def test_all_development_cli_preserves_typed_failure_without_leaking_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("INFIN_EQ_RUN_LIVE_BASELINES", "1")

    def failing_runner(*_args: object, **_kwargs: object) -> object:
        raise live_baselines.LivePrerequisiteError(
            "endpoint=https://secret.example response_id=resp-secret"
        )

    monkeypatch.setattr(
        run_development_baselines,
        "run_all_development_live_baseline",
        failing_runner,
        raising=False,
    )

    exit_code = run_development_baselines.main(
        [
            "--all-development-live",
            "--project-root",
            str(tmp_path),
            "--run-id",
            "run-phase5-development-live-v4",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "LivePrerequisiteError" in captured.err
    assert "secret.example" not in captured.err
    assert "resp-secret" not in captured.err
    assert captured.out == ""


def test_live_diagnostic_rethrows_and_sanitizes_the_provider_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diagnostic_path = tmp_path / "live-diagnostic.json"
    monkeypatch.setenv("INFIN_EQ_RUN_LIVE_BASELINES", "1")
    monkeypatch.setenv("INFIN_EQ_LIVE_DIAGNOSTICS", "1")

    def failing_provider_step() -> None:
        raise ValueError(
            "endpoint=https://secret.services.ai.azure.com/projects/secret "
            "subscription=12345678-1234-1234-1234-123456789012 "
            "response_id=resp-secret-response call_id=call-secret-call "
            "run_id=run-phase5-canonical-live-v3 episode_id=ep-61d8aa "
            "oracle=capacity_queueing prompt='do not persist this body' "
            "token=secret-token provider runtime mismatch"
        )

    with pytest.raises(ValueError, match="provider runtime mismatch"):
        live_baselines.run_live_step(
            "provider_runtime",
            failing_provider_step,
            diagnostic_path=diagnostic_path,
        )

    payload = json.loads(diagnostic_path.read_text(encoding="utf-8"))
    assert payload["phase"] == "provider_runtime"
    assert payload["exception_type"] == "ValueError"
    assert "provider runtime mismatch" in payload["message"]
    serialized = json.dumps(payload, sort_keys=True)
    assert "secret.services.ai.azure.com" not in serialized
    assert "12345678-1234-1234-1234-123456789012" not in serialized
    assert "resp-secret-response" not in serialized
    assert "call-secret-call" not in serialized
    assert "run-phase5-canonical-live-v3" not in serialized
    assert "ep-61d8aa" not in serialized
    assert "capacity_queueing" not in serialized
    assert "do not persist this body" not in serialized
    assert "secret-token" not in serialized
    assert payload["traceback"]


def test_live_diagnostic_requires_the_second_opt_in_and_cli_stays_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    diagnostic_path = tmp_path / "should-not-exist.json"
    monkeypatch.setenv("INFIN_EQ_RUN_LIVE_BASELINES", "1")
    monkeypatch.delenv("INFIN_EQ_LIVE_DIAGNOSTICS", raising=False)

    def failing_provider_step() -> None:
        raise ValueError("provider secret-token boundary detail")

    with pytest.raises(ValueError, match="secret-token"):
        live_baselines.run_live_step(
            "provider_runtime",
            failing_provider_step,
            diagnostic_path=diagnostic_path,
        )
    assert not diagnostic_path.exists()

    def failing_live_baseline(_project_root: Path, _run_id: str) -> dict[str, object]:
        raise ValueError("provider secret-token boundary detail")

    monkeypatch.setattr(
        run_development_baselines,
        "run_live_baseline",
        failing_live_baseline,
        raising=False,
    )
    exit_code = run_development_baselines.main(
        ["--live", "--project-root", str(tmp_path), "--run-id", CANONICAL_RUN_ID]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "ValueError" in captured.err
    assert "secret-token" not in captured.err
    assert captured.out == ""


def test_live_runner_reaches_runtime_for_required_phase5_canonical_run_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INFIN_EQ_RUN_LIVE_BASELINES", "1")

    class RuntimeBoundary(RuntimeError):
        pass

    def fake_build_runtime(*_args: object, **_kwargs: object) -> object:
        raise RuntimeBoundary("deterministic runtime boundary")

    monkeypatch.setattr(run_development_baselines, "build_live_runtime", fake_build_runtime)

    with pytest.raises(RuntimeBoundary, match="deterministic runtime boundary"):
        run_development_baselines.run_live_baseline(
            tmp_path,
            "run-phase5-canonical-live-v3",
        )


def _live_runtime_double() -> SimpleNamespace:
    return SimpleNamespace(
        store=object(),
        investigator=object(),
        verifier=object(),
        policy=object(),
        pins=None,
        close=lambda: None,
    )


def _raise_stage_error(stage: str) -> None:
    raise ValueError(
        f"stage={stage} response_id=resp-stage-secret prompt='stage body' "
        "endpoint=https://stage.secret.example oracle=capacity_queueing"
    )


@pytest.mark.parametrize(
    ("stage",),
    [
        ("detector_construction",),
        ("detector_detection",),
        ("matched_live_modes",),
        ("artifact_validation",),
        ("summary_construction",),
    ],
)
def test_live_runner_diagnostic_covers_each_remaining_top_level_stage(
    stage: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INFIN_EQ_RUN_LIVE_BASELINES", "1")
    monkeypatch.setenv("INFIN_EQ_LIVE_DIAGNOSTICS", "1")
    monkeypatch.setattr(
        run_development_baselines,
        "build_live_runtime",
        lambda *_args, **_kwargs: _live_runtime_double(),
    )

    class FakeDetector:
        def __init__(self, **_kwargs: object) -> None:
            if stage == "detector_construction":
                _raise_stage_error(stage)

        def detect(self, _episode_id: str) -> object:
            if stage == "detector_detection":
                _raise_stage_error(stage)
            return object()

    monkeypatch.setattr(run_development_baselines, "Detector", FakeDetector)

    if stage == "matched_live_modes":

        def fail_matched(**_kwargs: object) -> object:
            _raise_stage_error(stage)

        monkeypatch.setattr(run_development_baselines, "run_matched_live_modes", fail_matched)
    elif stage == "artifact_validation":
        monkeypatch.setattr(
            run_development_baselines,
            "run_matched_live_modes",
            lambda **_kwargs: SimpleNamespace(output_directory=tmp_path / "output"),
        )

        def fail_validation(*_args: object, **_kwargs: object) -> None:
            _raise_stage_error(stage)

        monkeypatch.setattr(run_development_baselines, "validate_live_artifacts", fail_validation)
    elif stage == "summary_construction":
        monkeypatch.setattr(
            run_development_baselines,
            "run_matched_live_modes",
            lambda **_kwargs: SimpleNamespace(
                output_directory=tmp_path / "output",
                episode=SimpleNamespace(episode_id=live_baselines.CANONICAL_EPISODE_ID),
                runs=(),
                evaluation=object(),
                pins=None,
            ),
        )
        monkeypatch.setattr(
            run_development_baselines,
            "validate_live_artifacts",
            lambda *_args, **_kwargs: None,
        )

    diagnostic_path = tmp_path / f"{stage}.json"
    with pytest.raises((AttributeError, ValueError)):
        run_development_baselines.run_live_baseline(
            tmp_path,
            f"run-baseline-diagnostic-{stage}",
            diagnostic_path=diagnostic_path,
        )

    payload = json.loads(diagnostic_path.read_text(encoding="utf-8"))
    assert payload["phase"] == stage
    serialized = json.dumps(payload, sort_keys=True)
    assert "resp-stage-secret" not in serialized
    assert "stage body" not in serialized
    assert "stage.secret.example" not in serialized
    assert "capacity_queueing" not in serialized
