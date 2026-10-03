"""Strict, deterministic correction packets for the finite workflow.

The Verifier may request one correction, but it may not author an unrestricted
instruction for the Investigator.  This module turns the small set of typed
Verifier outcomes into a packet whose only actionable values are existing
evidence identifiers and, when explicitly allowed, one fixed read lookup.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from enum import StrEnum
from typing import Annotated, Final, Literal, TypeAlias

from pydantic import Field, StrictInt, StrictStr, field_validator, model_validator

from infineq.evidence.tool_schemas import SignalEnum, WindowEnum
from infineq.schemas.common import EvidenceId, OpaqueId, StrictModel
from infineq.schemas.incident import IncidentPacketV1
from infineq.schemas.investigation import InvestigationResultV1
from infineq.schemas.verification import VerificationResultV1, VerificationStatus

MAX_CORRECTION_ITEMS: Final = 10
MAX_REUSE_EVIDENCE_IDS: Final = 20
MAX_INVESTIGATOR_TOOL_CALLS: Final = 6

_EVIDENCE_ID_RE = re.compile(
    r"^ev:[a-z0-9-]+:[a-z0-9_-]+:[a-z0-9_-]+:[a-z0-9_-]+:[a-z0-9_-]+:[a-f0-9]{8}$"
)
_UNSAFE_IDENTIFIER_MARKERS: Final[tuple[str, ...]] = (
    "oracle",
    "expected-diagnosis",
    "expected_diagnosis",
    "hidden-cause",
    "hidden_cause",
    "root-cause",
    "root_cause",
    "recovery-truth",
    "recovery_truth",
    "capacity-queueing",
    "capacity_queueing",
    "backend-slowdown",
    "backend_slowdown",
    "workload-shape-change",
    "workload_shape_change",
    "replica-or-deployment-regression",
    "replica_or_deployment_regression",
    "diagnosis",
    "cause",
    "action",
    "recovery",
    "shell",
    "prompt",
    "query",
    "path",
    "url",
    "infrastructure",
)


class CorrectionCategory(StrEnum):
    """The only categories allowed in a correction packet."""

    FAILED_CLAIM_EVIDENCE_CHECK = "failed_claim_evidence_check"
    MISSING_ALTERNATIVE = "missing_alternative"
    MISSING_REQUIRED_FIELD = "missing_required_field"
    POLICY_VIOLATION = "policy_violation"
    FORMATTING_REPAIR = "formatting_repair"


class MissingRequiredField(StrEnum):
    """Fixed structured fields that may be requested without free text."""

    EVIDENCE_CITATIONS = "evidence_citations"
    ALTERNATIVE_HYPOTHESIS = "alternative_hypothesis"
    REQUIRED_STRUCTURED_FIELD = "required_structured_field"


class PolicyViolationCode(StrEnum):
    """Fixed policy failures that can be shown to the Investigator."""

    INVALID_POLICY_LOOKUP = "invalid_policy_lookup"
    ACTION_TARGET = "action_target"
    ACTION_TYPE = "action_type"
    REPLICA_LIMITS = "replica_limits"
    HUMAN_APPROVAL = "human_approval"
    POLICY_REFERENCE = "policy_reference"
    DRY_RUN_ONLY = "dry_run_only"
    ACTION_TTL = "action_ttl"


class FormattingRepairCode(StrEnum):
    """Fixed format repairs; no model-generated repair text is retained."""

    STRICT_VERIFICATION_RESULT_JSON = "strict_verification_result_json"


_CATEGORY_MESSAGES: Final[dict[CorrectionCategory, str]] = {
    CorrectionCategory.FAILED_CLAIM_EVIDENCE_CHECK: (
        "Recheck the failed claim against the returned evidence."
    ),
    CorrectionCategory.MISSING_ALTERNATIVE: "Add the required competing explanation.",
    CorrectionCategory.MISSING_REQUIRED_FIELD: "Provide the missing required structured field.",
    CorrectionCategory.POLICY_VIOLATION: "Repair the policy violation before presentation.",
    CorrectionCategory.FORMATTING_REPAIR: "Return only the required structured result format.",
}
_TTL_CORRECTION_DETAIL: Final = (
    "Set the dry-run action TTL to exactly 300 seconds before presentation."
)


def _fixed_detail(category: CorrectionCategory, policy_code: object) -> str:
    # The frozen policy-infineq-v1 contract permits only a 300-second TTL.
    if (
        category is CorrectionCategory.POLICY_VIOLATION
        and policy_code == PolicyViolationCode.ACTION_TTL
    ):
        return _TTL_CORRECTION_DETAIL
    return _CATEGORY_MESSAGES[category]


class CorrectionItem(StrictModel):
    """One non-prescriptive, redacted correction reason."""

    category: CorrectionCategory
    claim_id: OpaqueId | None = None
    evidence_ids: tuple[EvidenceId, ...] = Field(default=(), max_length=MAX_REUSE_EVIDENCE_IDS)
    missing_field: MissingRequiredField | None = None
    policy_code: PolicyViolationCode | None = None
    formatting_code: FormattingRepairCode | None = None
    user_visible_detail: StrictStr = Field(default="", min_length=1, max_length=120)

    @model_validator(mode="before")
    @classmethod
    def add_fixed_user_detail(cls, value: object) -> object:
        if not isinstance(value, Mapping) or "user_visible_detail" in value:
            return value
        try:
            category_value = value.get("category")
            if not isinstance(category_value, str):
                return value
            category = CorrectionCategory(category_value)
        except (TypeError, ValueError):
            return value
        return {
            **value,
            "user_visible_detail": _fixed_detail(category, value.get("policy_code")),
        }

    @field_validator("evidence_ids", mode="before")
    @classmethod
    def canonicalize_evidence_ids(cls, value: object) -> object:
        if isinstance(value, (list, tuple, set, frozenset)) and all(
            isinstance(item, str) for item in value
        ):
            return tuple(sorted(set(value)))
        return value

    @field_validator("claim_id")
    @classmethod
    def reject_sensitive_claim_ids(cls, value: str | None) -> str | None:
        if value is not None and any(
            marker in value.casefold() for marker in _UNSAFE_IDENTIFIER_MARKERS
        ):
            raise ValueError("claim identifier contains protected content")
        return value

    @model_validator(mode="after")
    def enforce_category_shape(self) -> CorrectionItem:
        expected_detail = _fixed_detail(self.category, self.policy_code)
        if self.user_visible_detail != expected_detail:
            raise ValueError("correction detail is not an allow-listed redacted detail")

        specific_values = (
            self.claim_id,
            self.missing_field,
            self.policy_code,
            self.formatting_code,
        )
        if self.category is CorrectionCategory.FAILED_CLAIM_EVIDENCE_CHECK:
            if self.claim_id is None:
                raise ValueError("failed claim correction requires a claim identifier")
            if any(value is not None for value in specific_values[1:]):
                raise ValueError("failed claim correction has unrelated fields")
        elif self.category is CorrectionCategory.MISSING_ALTERNATIVE:
            if any(value is not None for value in specific_values):
                raise ValueError("missing alternative correction has unrelated fields")
        elif self.category is CorrectionCategory.MISSING_REQUIRED_FIELD:
            if self.missing_field is None:
                raise ValueError("missing field correction requires a field enum")
            if (
                self.claim_id is not None
                or self.policy_code is not None
                or self.formatting_code is not None
            ):
                raise ValueError("missing field correction has unrelated fields")
        elif self.category is CorrectionCategory.POLICY_VIOLATION:
            if self.policy_code is None:
                raise ValueError("policy correction requires a policy enum")
            if (
                self.claim_id is not None
                or self.missing_field is not None
                or self.formatting_code is not None
            ):
                raise ValueError("policy correction has unrelated fields")
        elif self.category is CorrectionCategory.FORMATTING_REPAIR:
            if self.formatting_code is None:
                raise ValueError("formatting correction requires a format enum")
            if (
                self.claim_id is not None
                or self.missing_field is not None
                or self.policy_code is not None
            ):
                raise ValueError("formatting correction has unrelated fields")
        return self


class InvestigatorLookupType(StrEnum):
    """Read-only Investigator lookups permitted after an explicit correction."""

    SIGNAL_WINDOW = "get_signal_window"
    REQUEST_SAMPLES = "get_request_samples"
    DEPLOYMENT_SNAPSHOT = "get_deployment_snapshot"


class SignalWindowLookupV1(StrictModel):
    """Fixed signal/window lookup; it has no free-form query."""

    lookup_type: Literal[InvestigatorLookupType.SIGNAL_WINDOW] = (
        InvestigatorLookupType.SIGNAL_WINDOW
    )
    signal: SignalEnum
    window: WindowEnum


class RequestSamplesLookupV1(StrictModel):
    """Fixed request-sample lookup with a bounded limit."""

    lookup_type: Literal[InvestigatorLookupType.REQUEST_SAMPLES] = (
        InvestigatorLookupType.REQUEST_SAMPLES
    )
    window: WindowEnum
    limit: StrictInt = Field(ge=1, le=20)


class DeploymentSnapshotLookupV1(StrictModel):
    """Fixed deployment snapshot lookup with no target text."""

    lookup_type: Literal[InvestigatorLookupType.DEPLOYMENT_SNAPSHOT] = (
        InvestigatorLookupType.DEPLOYMENT_SNAPSHOT
    )
    window: WindowEnum


LookupRequest: TypeAlias = Annotated[  # noqa: UP040
    SignalWindowLookupV1 | RequestSamplesLookupV1 | DeploymentSnapshotLookupV1,
    Field(discriminator="lookup_type"),
]


class LookupAllowanceV1(StrictModel):
    """One Verifier-explicit, same-incident, read-only lookup allowance."""

    incident_id: OpaqueId
    explicitly_identified_by_verifier: Literal[True]
    lookup: LookupRequest

    @property
    def lookup_type(self) -> InvestigatorLookupType:
        """Expose the fixed tool name without adding an input field."""

        return self.lookup.lookup_type


class CorrectionPacketV1(StrictModel):
    """Revision-one correction packet with no unrestricted instruction channel."""

    schema_version: Literal["1.0"] = "1.0"
    correction_id: OpaqueId
    incident_id: OpaqueId
    investigation_id: OpaqueId
    verification_id: OpaqueId
    revision: Literal[1] = 1
    corrections: Annotated[
        tuple[CorrectionItem, ...], Field(min_length=1, max_length=MAX_CORRECTION_ITEMS)
    ]
    reuse_evidence_ids: tuple[EvidenceId, ...] = Field(
        default=(), max_length=MAX_REUSE_EVIDENCE_IDS
    )
    lookup_allowance: LookupAllowanceV1 | None = None
    remaining_investigator_tool_calls: StrictInt = Field(ge=0, le=MAX_INVESTIGATOR_TOOL_CALLS)

    @field_validator("reuse_evidence_ids", mode="before")
    @classmethod
    def canonicalize_reuse_ids(cls, value: object) -> object:
        if isinstance(value, (list, tuple, set, frozenset)) and all(
            isinstance(item, str) for item in value
        ):
            return tuple(sorted(set(value)))
        return value

    @model_validator(mode="after")
    def enforce_packet_bounds(self) -> CorrectionPacketV1:
        all_evidence_ids = {
            *self.reuse_evidence_ids,
            *(evidence_id for item in self.corrections for evidence_id in item.evidence_ids),
        }
        for evidence_id in all_evidence_ids:
            if not evidence_id.startswith(f"ev:{self.incident_id}:"):
                raise ValueError("correction evidence crosses the incident boundary")
        item_evidence_ids = {
            evidence_id for item in self.corrections for evidence_id in item.evidence_ids
        }
        if not item_evidence_ids.issubset(set(self.reuse_evidence_ids)):
            raise ValueError("correction cites evidence that is not authorized for reuse")
        if len(item_evidence_ids | set(self.reuse_evidence_ids)) > MAX_REUSE_EVIDENCE_IDS:
            raise ValueError("correction evidence allowance is too large")
        if self.lookup_allowance is not None and self.remaining_investigator_tool_calls < 1:
            raise ValueError("a permitted lookup requires remaining Investigator budget")
        return self

    @property
    def reusable_evidence_ids(self) -> tuple[EvidenceId, ...]:
        """Compatibility alias for consumers that call the allowance reusable."""

        return self.reuse_evidence_ids

    @property
    def reused_evidence_ids(self) -> tuple[EvidenceId, ...]:
        """Compatibility alias for the serialized reuse allowance."""

        return self.reuse_evidence_ids


_REQUEST_SPECS: Final[dict[str, tuple[CorrectionCategory, MissingRequiredField | None]]] = {
    "add a credible competing explanation": (CorrectionCategory.MISSING_ALTERNATIVE, None),
    "retrieve evidence for every cited claim": (
        CorrectionCategory.MISSING_REQUIRED_FIELD,
        MissingRequiredField.EVIDENCE_CITATIONS,
    ),
    "provide the missing required field": (
        CorrectionCategory.MISSING_REQUIRED_FIELD,
        MissingRequiredField.REQUIRED_STRUCTURED_FIELD,
    ),
}
_FORMAT_REQUESTS: Final[frozenset[str]] = frozenset(
    {"repair formatting", "return strict VerificationResultV1 JSON"}
)
_POLICY_SPECS: Final[dict[str, PolicyViolationCode]] = {
    "action policy lookup is invalid": PolicyViolationCode.INVALID_POLICY_LOOKUP,
    "action target is not simulator-only": PolicyViolationCode.ACTION_TARGET,
    "action type is not allow-listed": PolicyViolationCode.ACTION_TYPE,
    "action type does not match policy": PolicyViolationCode.ACTION_TYPE,
    "replica limits do not match policy": PolicyViolationCode.REPLICA_LIMITS,
    "human approval is required": PolicyViolationCode.HUMAN_APPROVAL,
    "action policy reference does not match": PolicyViolationCode.POLICY_REFERENCE,
    "policy does not permit presentation of a dry-run action": PolicyViolationCode.DRY_RUN_ONLY,
    "action TTL does not match policy": PolicyViolationCode.ACTION_TTL,
}


def _canonicalize_ids(
    values: Collection[str],
    *,
    label: str,
    incident_id: str,
    packet_evidence_ids: set[str],
) -> tuple[str, ...]:
    """Validate episode membership and return a sorted, deduplicated tuple."""

    if isinstance(values, (str, bytes)):
        raise ValueError(f"{label} must be a collection of evidence IDs")
    raw_values = tuple(values)
    if len(set(raw_values)) > MAX_REUSE_EVIDENCE_IDS:
        raise ValueError(f"{label} exceeds the evidence ID limit")
    for evidence_id in raw_values:
        if not isinstance(evidence_id, str) or _EVIDENCE_ID_RE.fullmatch(evidence_id) is None:
            raise ValueError(f"{label} contains a fabricated evidence ID")
        if not evidence_id.startswith(f"ev:{incident_id}:"):
            raise ValueError(f"{label} crosses the incident boundary")
        if evidence_id not in packet_evidence_ids:
            raise ValueError(f"{label} contains an evidence ID absent from the packet")
    return tuple(sorted(set(raw_values)))


def _correction_item_for_request(request: str) -> CorrectionItem | None:
    policy_code = _POLICY_SPECS.get(request)
    if policy_code is not None:
        return CorrectionItem(
            category=CorrectionCategory.POLICY_VIOLATION,
            policy_code=policy_code,
        )
    if request in _FORMAT_REQUESTS:
        return CorrectionItem(
            category=CorrectionCategory.FORMATTING_REPAIR,
            formatting_code=FormattingRepairCode.STRICT_VERIFICATION_RESULT_JSON,
        )
    specification = _REQUEST_SPECS.get(request)
    if specification is None:
        return None
    category, missing_field = specification
    return CorrectionItem(category=category, missing_field=missing_field)


def _correction_item_for_issue(issue: str) -> CorrectionItem | None:
    policy_code = _POLICY_SPECS.get(issue)
    if policy_code is None:
        return None
    return CorrectionItem(
        category=CorrectionCategory.POLICY_VIOLATION,
        policy_code=policy_code,
    )


def build_correction_packet(
    verification: VerificationResultV1,
    *,
    packet: IncidentPacketV1,
    investigation: InvestigationResultV1,
    prior_returned_evidence_ids: Collection[str],
    remaining_investigator_tool_calls: int,
    lookup_allowance: LookupAllowanceV1 | None = None,
    requested_reuse_evidence_ids: Collection[str] | None = None,
    revision: int = 1,
) -> CorrectionPacketV1:
    """Build one safe correction packet from an existing Verifier result.

    This function accepts only typed prior results and converts exact, known
    Verifier outcomes into enums.  It never copies a Verifier note, issue, or
    correction string into the packet.
    """

    if not isinstance(verification, VerificationResultV1):
        raise TypeError("verification must be VerificationResultV1")
    if not isinstance(packet, IncidentPacketV1):
        raise TypeError("packet must be IncidentPacketV1")
    if not isinstance(investigation, InvestigationResultV1):
        raise TypeError("investigation must be InvestigationResultV1")
    if not isinstance(remaining_investigator_tool_calls, int) or isinstance(
        remaining_investigator_tool_calls, bool
    ):
        raise TypeError("remaining Investigator budget must be an integer")
    if not 0 <= remaining_investigator_tool_calls <= MAX_INVESTIGATOR_TOOL_CALLS:
        raise ValueError("remaining Investigator budget exceeds the fixed maximum")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision != 1:
        raise ValueError("only revision one may create a correction packet")
    if verification.status is not VerificationStatus.REVISION_REQUIRED:
        raise ValueError("only revision_required verification can create a correction packet")
    if verification.incident_id != packet.incident_id:
        raise ValueError("correction incident does not match packet")
    if verification.investigation_id != investigation.investigation_id:
        raise ValueError("correction does not match investigation")
    if investigation.incident_id != packet.incident_id:
        raise ValueError("investigation incident does not match packet")

    packet_evidence_ids = {item.evidence_id for item in packet.evidence_refs}
    prior_ids = _canonicalize_ids(
        prior_returned_evidence_ids,
        label="prior returned evidence",
        incident_id=packet.incident_id,
        packet_evidence_ids=packet_evidence_ids,
    )
    _canonicalize_ids(
        investigation.cited_evidence_ids,
        label="investigation citations",
        incident_id=packet.incident_id,
        packet_evidence_ids=packet_evidence_ids,
    )
    for check in verification.claim_checks:
        check_ids = _canonicalize_ids(
            check.evidence_ids,
            label="claim check evidence",
            incident_id=packet.incident_id,
            packet_evidence_ids=packet_evidence_ids,
        )
        if not set(check_ids).issubset(set(prior_ids)):
            raise ValueError("claim check cites evidence that was not returned in the prior run")

    if requested_reuse_evidence_ids is None:
        reuse_ids = prior_ids
    else:
        requested_ids = _canonicalize_ids(
            requested_reuse_evidence_ids,
            label="requested reuse evidence",
            incident_id=packet.incident_id,
            packet_evidence_ids=packet_evidence_ids,
        )
        if not set(requested_ids).issubset(set(prior_ids)):
            raise ValueError("requested reuse evidence was not returned in the prior run")
        reuse_ids = requested_ids

    if lookup_allowance is not None:
        if not isinstance(lookup_allowance, LookupAllowanceV1):
            raise TypeError("lookup allowance must be LookupAllowanceV1")
        if "retrieve evidence for every cited claim" not in verification.correction_requests:
            raise ValueError("lookup allowance was not explicitly requested by the Verifier")
        if lookup_allowance.incident_id != packet.incident_id:
            raise ValueError("lookup allowance crosses the incident boundary")
        if remaining_investigator_tool_calls < 1:
            raise ValueError("lookup allowance exceeds the remaining Investigator budget")

    corrections: list[CorrectionItem] = []
    for check in verification.claim_checks:
        if not check.supported:
            corrections.append(
                CorrectionItem(
                    category=CorrectionCategory.FAILED_CLAIM_EVIDENCE_CHECK,
                    claim_id=check.claim_id,
                    evidence_ids=check.evidence_ids,
                )
            )
    for request in verification.correction_requests:
        item = _correction_item_for_request(request)
        if item is None:
            raise ValueError("verification contains an unsupported correction request")
        corrections.append(item)
    for issue in verification.issues:
        item = _correction_item_for_issue(issue)
        if item is None:
            raise ValueError("verification contains an unsupported correction issue")
        corrections.append(item)

    deduplicated: dict[tuple[object, ...], CorrectionItem] = {}
    for item in corrections:
        key = (
            item.category,
            item.claim_id,
            item.evidence_ids,
            item.missing_field,
            item.policy_code,
            item.formatting_code,
        )
        deduplicated.setdefault(key, item)
    ordered_corrections = tuple(
        sorted(
            deduplicated.values(),
            key=lambda item: (
                item.category.value,
                item.claim_id or "",
                tuple(item.evidence_ids),
                item.missing_field.value if item.missing_field else "",
                item.policy_code.value if item.policy_code else "",
                item.formatting_code.value if item.formatting_code else "",
            ),
        )
    )
    if not ordered_corrections:
        raise ValueError("verification contains no permitted correction")

    return CorrectionPacketV1(
        correction_id=f"correction-{packet.incident_id}-r1",
        incident_id=packet.incident_id,
        investigation_id=investigation.investigation_id,
        verification_id=verification.verification_id,
        revision=1,
        corrections=ordered_corrections,
        reuse_evidence_ids=reuse_ids,
        lookup_allowance=lookup_allowance,
        remaining_investigator_tool_calls=remaining_investigator_tool_calls,
    )


CorrectionPacket = CorrectionPacketV1
MissingEvidenceLookup = LookupAllowanceV1
PermittedLookup = LookupAllowanceV1


__all__ = [
    "CorrectionCategory",
    "CorrectionItem",
    "CorrectionPacket",
    "CorrectionPacketV1",
    "DeploymentSnapshotLookupV1",
    "FormattingRepairCode",
    "InvestigatorLookupType",
    "LookupAllowanceV1",
    "LookupRequest",
    "MissingEvidenceLookup",
    "MissingRequiredField",
    "PermittedLookup",
    "PolicyViolationCode",
    "RequestSamplesLookupV1",
    "SignalWindowLookupV1",
    "build_correction_packet",
]
