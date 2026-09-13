"""Evidence-linked investigation contracts."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import AwareDatetime, Field, field_validator, model_validator

from infineq.schemas.action import ActionPlanV1
from infineq.schemas.common import EvidenceId, OpaqueId, SchemaVersion, StrictModel


class IncidentFamily(StrEnum):
    """Frozen diagnosis families visible to the agents."""

    HEALTHY = "healthy"
    CAPACITY_QUEUEING = "capacity_queueing"
    BACKEND_SLOWDOWN = "backend_slowdown"
    BACKEND_ERROR = "backend_error"
    WORKLOAD_SHAPE_CHANGE = "workload_shape_change"
    REPLICA_OR_DEPLOYMENT_REGRESSION = "replica_or_deployment_regression"
    UNKNOWN = "unknown"


class Disposition(StrEnum):
    """Investigator completion state."""

    DIAGNOSED = "diagnosed"
    INDETERMINATE = "indeterminate"
    NO_INCIDENT = "no_incident"
    ANALYSIS_INCOMPLETE = "analysis_incomplete"


class EvidenceCoverage(StrEnum):
    """Qualitative evidence sufficiency, never a fabricated probability."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    INSUFFICIENT = "insufficient"


class Hypothesis(StrictModel):
    """One ranked explanation with support, contradiction, and unknowns."""

    schema_version: SchemaVersion = "1.0"
    hypothesis_id: OpaqueId
    family: IncidentFamily
    rank: Annotated[int, Field(ge=1, le=3)]
    evidence_coverage: EvidenceCoverage
    statement: Annotated[str, Field(min_length=1, max_length=500)]
    supporting_evidence_ids: tuple[EvidenceId, ...]
    contradicting_evidence_ids: tuple[EvidenceId, ...]
    missing_evidence: tuple[str, ...]
    next_check: Annotated[str | None, Field(max_length=500)] = None


class InvestigationResultV1(StrictModel):
    """Normalized Investigator output; hidden reasoning is intentionally absent."""

    schema_version: SchemaVersion = "1.0"
    investigation_id: OpaqueId
    incident_id: OpaqueId
    completed_at: AwareDatetime
    disposition: Disposition
    summary: Annotated[str, Field(min_length=1, max_length=1_000)]
    hypotheses: Annotated[tuple[Hypothesis, ...], Field(max_length=3)]
    leading_hypothesis_id: OpaqueId | None = None
    proposed_action: ActionPlanV1 | None = None
    cited_evidence_ids: tuple[EvidenceId, ...]
    limitations: tuple[str, ...] = ()

    @field_validator("completed_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def enforce_grounded_disposition(self) -> "InvestigationResultV1":
        ids = [hypothesis.hypothesis_id for hypothesis in self.hypotheses]
        ranks = [hypothesis.rank for hypothesis in self.hypotheses]
        if len(ids) != len(set(ids)) or sorted(ranks) != list(range(1, len(ranks) + 1)):
            raise ValueError("hypotheses require unique IDs and contiguous ranks")

        cited = set(self.cited_evidence_ids)
        used = {
            evidence_id
            for hypothesis in self.hypotheses
            for evidence_id in (
                *hypothesis.supporting_evidence_ids,
                *hypothesis.contradicting_evidence_ids,
            )
        }
        if not used.issubset(cited):
            raise ValueError("hypothesis evidence must be listed as cited evidence")
        if any(
            self.disposition is Disposition.DIAGNOSED
            and hypothesis.evidence_coverage is EvidenceCoverage.COMPLETE
            and not hypothesis.supporting_evidence_ids
            for hypothesis in self.hypotheses
        ):
            raise ValueError("complete hypotheses require supporting evidence")

        if self.disposition is Disposition.DIAGNOSED:
            if not self.hypotheses or self.leading_hypothesis_id not in ids:
                raise ValueError("a diagnosis requires a ranked leading hypothesis")
            leading = next(
                item for item in self.hypotheses if item.hypothesis_id == self.leading_hypothesis_id
            )
            if self.proposed_action and (
                leading.evidence_coverage is not EvidenceCoverage.COMPLETE
                or not leading.supporting_evidence_ids
                or not set(leading.supporting_evidence_ids).issubset(cited)
            ):
                raise ValueError("an action proposal requires complete, cited supporting evidence")
        elif self.leading_hypothesis_id is not None or self.proposed_action is not None:
            raise ValueError(
                "non-diagnostic dispositions cannot select a cause or propose an action"
            )

        if self.proposed_action and self.proposed_action.incident_id != self.incident_id:
            raise ValueError("action and investigation incident IDs must match")
        return self
