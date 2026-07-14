from __future__ import annotations

import json
from dataclasses import dataclass, field

from mr_reviewer.review_result import ALLOWED_CONFIDENCES, ALLOWED_SEVERITIES

PLAN_SCHEMA_VERSION = "review-set-plan/v1"
RESULT_SCHEMA_VERSION = "review-set-review/v1"


class ReviewSetPlanParseError(ValueError):
    pass


class StructuredReviewSetParseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ReviewSetEvidenceRef:
    member_id: str
    path: str
    start_line: int
    end_line: int
    detail: str


@dataclass(frozen=True, slots=True)
class ReviewSetTargetPosition:
    old_path: str
    new_path: str
    old_line: int
    new_line: int


@dataclass(frozen=True, slots=True)
class ReviewSetFindingTarget:
    member_id: str
    position: ReviewSetTargetPosition | None
    suggestion: str


@dataclass(frozen=True, slots=True)
class ReviewSetFinding:
    issue_id: str
    rule_id: str
    severity: str
    confidence: str
    title: str
    impact: str
    evidence_refs: tuple[ReviewSetEvidenceRef, ...]
    targets: tuple[ReviewSetFindingTarget, ...]


@dataclass(frozen=True, slots=True)
class StructuredReviewSetResult:
    schema_version: str
    findings: tuple[ReviewSetFinding, ...]
    relationship_summary: list[str]
    notes: list[str]
    test_gaps: list[str]
    good: list[str] = field(default_factory=list)


def parse_review_set_plan(raw_output: str, member_ids: set[str]) -> dict[str, object]:
    payload = _load_object(raw_output, ReviewSetPlanParseError, "review set plan")
    _exact_fields(
        payload,
        {"schema_version", "member_focus", "relationships", "open_questions"},
        ReviewSetPlanParseError,
        "review set plan",
    )
    if payload.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise ReviewSetPlanParseError(f"schema_version must be {PLAN_SCHEMA_VERSION}")

    member_focus = _require_list(payload, "member_focus", ReviewSetPlanParseError)
    parsed_focus = [_parse_member_focus(item, index) for index, item in enumerate(member_focus)]
    focus_ids = [item["member_id"] for item in parsed_focus]
    if len(focus_ids) != len(set(focus_ids)) or set(focus_ids) != member_ids:
        raise ReviewSetPlanParseError("member_focus must cover every manifest member exactly once")

    relationships = _require_list(payload, "relationships", ReviewSetPlanParseError)
    parsed_relationships = [
        _parse_relationship(item, index, member_ids) for index, item in enumerate(relationships)
    ]
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "member_focus": parsed_focus,
        "relationships": parsed_relationships,
        "open_questions": _text_list(payload, "open_questions", ReviewSetPlanParseError),
    }


def parse_structured_review_set_result(raw_output: str) -> StructuredReviewSetResult:
    payload = _load_object(raw_output, StructuredReviewSetParseError, "review set result")
    _exact_fields(
        payload,
        {"schema_version", "findings", "relationship_summary", "notes", "test_gaps", "good"},
        StructuredReviewSetParseError,
        "review set result",
    )
    if payload.get("schema_version") != RESULT_SCHEMA_VERSION:
        raise StructuredReviewSetParseError(f"schema_version must be {RESULT_SCHEMA_VERSION}")
    findings = _require_list(payload, "findings", StructuredReviewSetParseError)
    relationship_summary = _text_list(payload, "relationship_summary", StructuredReviewSetParseError)
    if not relationship_summary:
        raise StructuredReviewSetParseError("relationship_summary must not be empty")
    return StructuredReviewSetResult(
        schema_version=RESULT_SCHEMA_VERSION,
        findings=tuple(_parse_finding(item, index) for index, item in enumerate(findings)),
        relationship_summary=relationship_summary,
        notes=_text_list(payload, "notes", StructuredReviewSetParseError),
        test_gaps=_text_list(payload, "test_gaps", StructuredReviewSetParseError),
        good=_text_list(payload, "good", StructuredReviewSetParseError),
    )


def _parse_member_focus(value: object, index: int) -> dict[str, object]:
    context = f"member_focus[{index}]"
    item = _require_object(value, ReviewSetPlanParseError, context)
    _exact_fields(
        item,
        {"member_id", "change_intent", "critical_paths", "test_risks"},
        ReviewSetPlanParseError,
        context,
    )
    critical_paths = _require_list(item, "critical_paths", ReviewSetPlanParseError)
    return {
        "member_id": _text(item, "member_id", ReviewSetPlanParseError, context),
        "change_intent": _text_list(item, "change_intent", ReviewSetPlanParseError, context),
        "critical_paths": [
            _parse_critical_path(path, path_index, context) for path_index, path in enumerate(critical_paths)
        ],
        "test_risks": _text_list(item, "test_risks", ReviewSetPlanParseError, context),
    }


def _parse_critical_path(value: object, index: int, parent: str) -> dict[str, object]:
    context = f"{parent}.critical_paths[{index}]"
    item = _require_object(value, ReviewSetPlanParseError, context)
    _exact_fields(item, {"path", "reason", "verify"}, ReviewSetPlanParseError, context)
    verify = _text_list(item, "verify", ReviewSetPlanParseError, context)
    if not verify:
        raise ReviewSetPlanParseError(f"{context}.verify must not be empty")
    return {
        "path": _text(item, "path", ReviewSetPlanParseError, context),
        "reason": _text(item, "reason", ReviewSetPlanParseError, context),
        "verify": verify,
    }


def _parse_relationship(value: object, index: int, member_ids: set[str]) -> dict[str, object]:
    context = f"relationships[{index}]"
    item = _require_object(value, ReviewSetPlanParseError, context)
    _exact_fields(
        item,
        {"from_member_id", "to_member_id", "contract", "evidence_refs", "verification"},
        ReviewSetPlanParseError,
        context,
    )
    from_member = _text(item, "from_member_id", ReviewSetPlanParseError, context)
    to_member = _text(item, "to_member_id", ReviewSetPlanParseError, context)
    if from_member not in member_ids or to_member not in member_ids or from_member == to_member:
        raise ReviewSetPlanParseError(f"{context} must reference two different manifest members")
    evidence = _require_list(item, "evidence_refs", ReviewSetPlanParseError)
    verification = _text_list(item, "verification", ReviewSetPlanParseError, context)
    if not evidence or not verification:
        raise ReviewSetPlanParseError(f"{context} evidence_refs and verification must not be empty")
    return {
        "from_member_id": from_member,
        "to_member_id": to_member,
        "contract": _text(item, "contract", ReviewSetPlanParseError, context),
        "evidence_refs": [
            _evidence_to_dict(_parse_evidence_ref(ref, ref_index, ReviewSetPlanParseError, context))
            for ref_index, ref in enumerate(evidence)
        ],
        "verification": verification,
    }


def _parse_finding(value: object, index: int) -> ReviewSetFinding:
    context = f"findings[{index}]"
    item = _require_object(value, StructuredReviewSetParseError, context)
    _exact_fields(
        item,
        {"issue_id", "rule_id", "severity", "confidence", "title", "impact", "evidence_refs", "targets"},
        StructuredReviewSetParseError,
        context,
    )
    severity = _text(item, "severity", StructuredReviewSetParseError, context)
    confidence = _text(item, "confidence", StructuredReviewSetParseError, context)
    if severity not in ALLOWED_SEVERITIES:
        raise StructuredReviewSetParseError(f"{context}.severity must be one of {sorted(ALLOWED_SEVERITIES)}")
    if confidence not in ALLOWED_CONFIDENCES:
        raise StructuredReviewSetParseError(f"{context}.confidence must be one of {sorted(ALLOWED_CONFIDENCES)}")
    evidence = _require_list(item, "evidence_refs", StructuredReviewSetParseError)
    targets = _require_list(item, "targets", StructuredReviewSetParseError)
    if not evidence or not targets:
        raise StructuredReviewSetParseError(f"{context} evidence_refs and targets must not be empty")
    return ReviewSetFinding(
        issue_id=_text(item, "issue_id", StructuredReviewSetParseError, context),
        rule_id=_text(item, "rule_id", StructuredReviewSetParseError, context),
        severity=severity,
        confidence=confidence,
        title=_text(item, "title", StructuredReviewSetParseError, context),
        impact=_text(item, "impact", StructuredReviewSetParseError, context),
        evidence_refs=tuple(
            _parse_evidence_ref(ref, ref_index, StructuredReviewSetParseError, context)
            for ref_index, ref in enumerate(evidence)
        ),
        targets=tuple(_parse_target(target, target_index, context) for target_index, target in enumerate(targets)),
    )


def _parse_evidence_ref(value: object, index: int, error_type, parent: str) -> ReviewSetEvidenceRef:
    context = f"{parent}.evidence_refs[{index}]"
    item = _require_object(value, error_type, context)
    _exact_fields(item, {"member_id", "path", "start_line", "end_line", "detail"}, error_type, context)
    start_line = _integer(item, "start_line", error_type, context)
    end_line = _integer(item, "end_line", error_type, context)
    if start_line < 1 or end_line < start_line:
        raise error_type(f"{context} line range is invalid")
    return ReviewSetEvidenceRef(
        member_id=_text(item, "member_id", error_type, context),
        path=_text(item, "path", error_type, context),
        start_line=start_line,
        end_line=end_line,
        detail=_text(item, "detail", error_type, context),
    )


def _parse_target(value: object, index: int, parent: str) -> ReviewSetFindingTarget:
    context = f"{parent}.targets[{index}]"
    item = _require_object(value, StructuredReviewSetParseError, context)
    _exact_fields(item, {"member_id", "position", "suggestion"}, StructuredReviewSetParseError, context)
    raw_position = item.get("position")
    position = None if raw_position is None else _parse_position(raw_position, context)
    return ReviewSetFindingTarget(
        member_id=_text(item, "member_id", StructuredReviewSetParseError, context),
        position=position,
        suggestion=_text(item, "suggestion", StructuredReviewSetParseError, context),
    )


def _parse_position(value: object, parent: str) -> ReviewSetTargetPosition:
    context = f"{parent}.position"
    item = _require_object(value, StructuredReviewSetParseError, context)
    _exact_fields(
        item,
        {"old_path", "new_path", "old_line", "new_line"},
        StructuredReviewSetParseError,
        context,
    )
    return ReviewSetTargetPosition(
        old_path=_text(item, "old_path", StructuredReviewSetParseError, context),
        new_path=_text(item, "new_path", StructuredReviewSetParseError, context),
        old_line=_integer(item, "old_line", StructuredReviewSetParseError, context),
        new_line=_integer(item, "new_line", StructuredReviewSetParseError, context),
    )


def _load_object(raw_output: str, error_type, label: str) -> dict:
    try:
        payload = json.loads(raw_output)
    except json.JSONDecodeError as exc:
        raise error_type(f"{label} output must be valid JSON: {exc}") from exc
    return _require_object(payload, error_type, label)


def _require_object(value: object, error_type, context: str) -> dict:
    if not isinstance(value, dict):
        raise error_type(f"{context} must be an object")
    return value


def _require_list(payload: dict, field_name: str, error_type) -> list:
    value = payload.get(field_name)
    if not isinstance(value, list):
        raise error_type(f"{field_name} must be a list")
    return value


def _text(payload: dict, field_name: str, error_type, context: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise error_type(f"{context}.{field_name} must be a non-empty string")
    return value.strip()


def _integer(payload: dict, field_name: str, error_type, context: str) -> int:
    value = payload.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise error_type(f"{context}.{field_name} must be an integer")
    return value


def _text_list(payload: dict, field_name: str, error_type, context: str = "") -> list[str]:
    value = _require_list(payload, field_name, error_type)
    if not all(isinstance(item, str) and item.strip() for item in value):
        prefix = f"{context}." if context else ""
        raise error_type(f"{prefix}{field_name} must contain non-empty strings")
    return [item.strip() for item in value]


def _exact_fields(payload: dict, expected: set[str], error_type, context: str) -> None:
    unexpected = set(payload) - expected
    missing = expected - set(payload)
    if unexpected:
        raise error_type(f"{context} contains unexpected fields: {sorted(unexpected)}")
    if missing:
        raise error_type(f"{context} is missing fields: {sorted(missing)}")


def _evidence_to_dict(evidence: ReviewSetEvidenceRef) -> dict[str, object]:
    return {
        "member_id": evidence.member_id,
        "path": evidence.path,
        "start_line": evidence.start_line,
        "end_line": evidence.end_line,
        "detail": evidence.detail,
    }
