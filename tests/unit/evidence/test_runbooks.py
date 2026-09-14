import json
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from infineq.evidence.runbooks import RunbookCatalog, RunbookChunk
from infineq.evidence.store import EvidenceStore
from infineq.evidence.tool_schemas import (
    RunbookQueryEnum,
    RunbookSearchStatus,
    SearchRunbookRequest,
)
from infineq.evidence.tools import InvestigatorTools

PROJECT_ROOT = Path(__file__).parents[3]
RUNBOOK_PATH = PROJECT_ROOT / "data" / "infineq" / "v1" / "knowledge" / "runbook_chunks.jsonl"


def make_catalog() -> RunbookCatalog:
    return RunbookCatalog(project_root=PROJECT_ROOT)


def test_curated_chunks_are_immutable_complete_and_source_versioned() -> None:
    chunks = make_catalog().load()

    assert chunks
    assert {chunk.symptom_family for chunk in chunks} == set(RunbookQueryEnum)
    assert len({chunk.evidence_id for chunk in chunks}) == len(chunks)
    assert chunks == tuple(sorted(chunks, key=lambda item: item.evidence_id))
    assert all(chunk.bounded_action_class == "simulator_only" for chunk in chunks)
    assert all(chunk.approval_required is True for chunk in chunks)
    assert all(chunk.retrieval_date == date(2026, 9, 14) for chunk in chunks)
    assert all(chunk.source_url.startswith("https://") for chunk in chunks)
    assert all(chunk.source_version for chunk in chunks)
    assert all(chunk.prerequisites for chunk in chunks)
    assert all(chunk.diagnostic_checks for chunk in chunks)
    assert all(chunk.rollback_criteria for chunk in chunks)
    assert all(chunk.verification_criteria for chunk in chunks)

    with pytest.raises(ValidationError):
        chunks[0].title = "changed"


def test_runbook_search_filters_by_enum_and_orders_results_deterministically() -> None:
    catalog = make_catalog()

    for query_enum in RunbookQueryEnum:
        results = catalog.search(query_enum=query_enum, top_k=3)
        repeated = catalog.search(query_enum=query_enum, top_k=3)

        assert results
        assert results == repeated
        assert len(results) <= 3
        assert all(result.symptom_family is query_enum for result in results)
        assert tuple(result.evidence_id for result in results) == tuple(
            sorted(result.evidence_id for result in results)
        )

    assert catalog.search(query_enum=RunbookQueryEnum.CAPACITY_QUEUEING, top_k=1) == (
        catalog.search(query_enum=RunbookQueryEnum.CAPACITY_QUEUEING, top_k=3)[0],
    )


def test_runbook_search_rejects_unbounded_inputs_at_the_catalog_boundary() -> None:
    with pytest.raises(ValueError):
        make_catalog().search(query_enum="capacity_queueing", top_k=3)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        make_catalog().search(query_enum=RunbookQueryEnum.CAPACITY_QUEUEING, top_k=4)
    with pytest.raises(ValueError):
        make_catalog().search(query_enum=RunbookQueryEnum.CAPACITY_QUEUEING, top_k=True)  # type: ignore[arg-type]


def test_investigator_search_returns_available_typed_runbook_evidence() -> None:
    tools = InvestigatorTools(store=EvidenceStore(project_root=PROJECT_ROOT))

    response = tools.search_runbook(
        SearchRunbookRequest(query_enum=RunbookQueryEnum.CAPACITY_QUEUEING, top_k=3)
    )

    assert response.status is RunbookSearchStatus.AVAILABLE
    assert response.placeholder is False
    assert response.results
    assert {result.symptom_family for result in response.results} == {
        RunbookQueryEnum.CAPACITY_QUEUEING
    }
    assert len(response.results) <= 3
    assert response.notice

    serialized = json.dumps(response.model_dump(mode="json"), sort_keys=True)
    for forbidden in (
        "oracle",
        "root_cause",
        "fault_label",
        "filesystem_path",
        "hidden_oracles",
        "kubectl ",
        "curl ",
        "`",
    ):
        assert forbidden not in serialized.lower()


def test_runbook_chunk_rejects_shell_text_and_protected_fields() -> None:
    base = {
        "evidence_id": "runbook-capacity-queueing-v1",
        "symptom_family": RunbookQueryEnum.CAPACITY_QUEUEING,
        "title": "Capacity queueing",
        "summary": "Compare waiting and execution observations.",
        "prerequisites": ("Fresh observations are available.",),
        "diagnostic_checks": ("Compare queue observations with latency.",),
        "bounded_action_class": "simulator_only",
        "approval_required": True,
        "rollback_criteria": ("Do not mutate production.",),
        "verification_criteria": ("Re-measure the fixed criteria.",),
        "source_url": "https://docs.vllm.ai/en/stable/usage/metrics/",
        "retrieval_date": "2026-09-14",
        "source_version": "stable",
    }

    for diagnostic_checks in (
        ("kubectl get pods",),
        ("`curl http://example.test`",),
        ("echo unsafe",),
        ("inspect queue | compare load",),
    ):
        with pytest.raises(ValidationError):
            RunbookChunk(**{**base, "diagnostic_checks": diagnostic_checks})

    with pytest.raises(ValidationError):
        RunbookChunk(**{**base, "root_cause": "capacity_queueing"})


def test_runbook_source_file_is_jsonl_without_executable_shell_text() -> None:
    lines = RUNBOOK_PATH.read_text(encoding="utf-8").splitlines()

    assert lines
    records = [json.loads(line) for line in lines]
    assert all(record["evidence_id"].startswith("runbook-") for record in records)
    assert all("symptom_family" in record for record in records)
    raw = RUNBOOK_PATH.read_text(encoding="utf-8").lower()
    for forbidden in ("kubectl ", "curl ", "wget ", "`", "$(", "&&", ";"):
        assert forbidden not in raw
