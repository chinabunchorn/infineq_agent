"""Read-only, enum-filtered access to curated runbook evidence."""

from __future__ import annotations

import errno
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from infineq.errors import DataMissingError, SchemaError
from infineq.evidence.tool_schemas import RunbookQueryEnum, RunbookResult

RUNBOOK_RELATIVE_PATH: Final = (
    Path("data") / "infineq" / "v1" / "knowledge" / "runbook_chunks.jsonl"
)

# The catalog is intentionally not a general URL retriever.  These are the
# public references cited by the frozen design and roadmap.
CURATED_RUNBOOK_SOURCE_URLS: Final = frozenset(
    {
        "https://raw.githubusercontent.com/vllm-project/vllm/v0.8.5/docs/source/serving/metrics.md",
        "https://docs.vllm.ai/en/stable/usage/metrics/",
        "https://docs.vllm.ai/en/latest/benchmarking/cli/",
        "https://kubernetes.io/docs/concepts/workloads/autoscaling/horizontal-pod-autoscale/",
        "https://kserve.github.io/website/docs/model-serving/generative-inference/llmisvc/autoscaling/llmisvc-autoscaling",
    }
)


class RunbookChunk(RunbookResult):
    """One immutable curated runbook record loaded from the knowledge catalog."""


def _read_only_bytes(path: Path) -> bytes:
    """Read the fixed knowledge file without following the final symlink."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_descriptor: int | None = None
    try:
        file_descriptor = os.open(path, flags)
        if not stat.S_ISREG(os.fstat(file_descriptor).st_mode):
            raise DataMissingError("curated runbook evidence is missing")
        with os.fdopen(file_descriptor, "rb", closefd=True) as handle:
            file_descriptor = None
            return handle.read()
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        raise DataMissingError("curated runbook evidence is missing") from None
    except PermissionError:
        raise PermissionError("curated runbook evidence access is denied") from None
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EPERM, errno.ELOOP}:
            raise PermissionError("curated runbook evidence access is denied") from None
        raise DataMissingError("curated runbook evidence is missing") from None
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)


def _validated_chunk(raw: object) -> RunbookChunk:
    if not isinstance(raw, dict):
        raise SchemaError("curated runbook evidence record is invalid")
    try:
        chunk = RunbookChunk.model_validate(raw)
    except ValidationError:
        raise SchemaError("curated runbook evidence record is invalid") from None
    if chunk.source_url not in CURATED_RUNBOOK_SOURCE_URLS:
        raise SchemaError("curated runbook source is not allow-listed")
    return chunk


@dataclass(frozen=True, slots=True)
class RunbookCatalog:
    """Load and filter the immutable v1 runbook catalog."""

    project_root: Path

    def __post_init__(self) -> None:
        root = Path(self.project_root).resolve()
        object.__setattr__(self, "project_root", root)

    @property
    def _catalog_path(self) -> Path:
        return self.project_root / RUNBOOK_RELATIVE_PATH

    def load(self) -> tuple[RunbookChunk, ...]:
        """Return all curated chunks in stable evidence-ID order."""

        path = self._catalog_path
        if path.is_symlink() or path.parent.is_symlink():
            raise PermissionError("curated runbook evidence must not use symlinks")
        try:
            resolved_path = path.resolve()
        except RuntimeError:
            raise PermissionError("curated runbook evidence resolution failed") from None
        if resolved_path != path:
            raise PermissionError(
                "curated runbook evidence must remain under the fixed knowledge root"
            )

        payload = _read_only_bytes(path)
        if not payload.strip():
            raise SchemaError("curated runbook evidence is empty")
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            raise SchemaError("curated runbook evidence is not valid UTF-8") from None

        chunks: list[RunbookChunk] = []
        seen_ids: set[str] = set()
        for line in text.splitlines():
            if not line.strip():
                raise SchemaError("curated runbook evidence contains a blank record")
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                raise SchemaError("curated runbook evidence contains invalid JSON") from None
            chunk = _validated_chunk(raw)
            if chunk.evidence_id in seen_ids:
                raise SchemaError("curated runbook evidence IDs must be unique")
            seen_ids.add(chunk.evidence_id)
            chunks.append(chunk)

        if not chunks:
            raise SchemaError("curated runbook evidence is empty")
        return tuple(sorted(chunks, key=lambda item: item.evidence_id))

    def search(self, *, query_enum: RunbookQueryEnum, top_k: int) -> tuple[RunbookChunk, ...]:
        """Filter only by the fixed symptom enum and return at most three chunks."""

        if not isinstance(query_enum, RunbookQueryEnum):
            raise ValueError("runbook query must be an allow-listed enum")
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 3:
            raise ValueError("runbook top_k must be between one and three")
        return tuple(chunk for chunk in self.load() if chunk.symptom_family is query_enum)[:top_k]


def load_runbook_chunks(project_root: Path) -> tuple[RunbookChunk, ...]:
    """Load the fixed runbook catalog without accepting a caller-selected path."""

    return RunbookCatalog(project_root=project_root).load()


def search_runbook_chunks(
    project_root: Path, *, query_enum: RunbookQueryEnum, top_k: int
) -> tuple[RunbookChunk, ...]:
    """Run the fixed enum/filter lookup used by the investigator tool."""

    return RunbookCatalog(project_root=project_root).search(
        query_enum=query_enum,
        top_k=top_k,
    )


__all__ = [
    "CURATED_RUNBOOK_SOURCE_URLS",
    "RUNBOOK_RELATIVE_PATH",
    "RunbookCatalog",
    "RunbookChunk",
    "load_runbook_chunks",
    "search_runbook_chunks",
]
