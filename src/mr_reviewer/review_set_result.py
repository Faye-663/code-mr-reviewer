from __future__ import annotations

from dataclasses import dataclass, field

from mr_reviewer.result_validation import (
    require_exact_fields as _exact_fields,
    require_integer as _integer,
    require_list as _require_list,
    require_object as _require_object,
    require_text as _text,
    require_text_list as _text_list,
    normalize_report_text_list,
    parse_findings_isolated,
    parse_review_position,
)
from mr_reviewer.review_result import ALLOWED_CONFIDENCES, ALLOWED_SEVERITIES
from mr_reviewer.structured_output import parse_json_object_output

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
    side: str = "legacy"


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
    structured_parse_status: str = "success"
    rejected_findings: list[dict[str, object]] = field(default_factory=list)
    normalization_warnings: list[dict[str, str]] = field(default_factory=list)


def parse_review_set_plan(raw_output: str, member_ids: set[str]) -> dict[str, object]:
    return parse_json_object_output(
        raw_output,
        output_type="review_set_plan",
        error_label="review set plan",
        error_type=ReviewSetPlanParseError,
        parse_object=lambda payload: _parse_review_set_plan_object(payload, member_ids),
    )


def _parse_review_set_plan_object(payload: object, member_ids: set[str]) -> dict[str, object]:
    payload = _require_object(payload, ReviewSetPlanParseError, "review set plan")
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
    return parse_json_object_output(
        raw_output,
        output_type="review_set_result",
        error_label="review set result",
        error_type=StructuredReviewSetParseError,
        parse_object=_parse_structured_review_set_object,
        prefer_authoritative_agent_output=True,
    )


def _parse_structured_review_set_object(payload: object) -> StructuredReviewSetResult:
    payload = _require_object(payload, StructuredReviewSetParseError, "review set result")
    unexpected = set(payload) - {
        "schema_version", "findings", "relationship_summary", "notes", "test_gaps", "good"
    }
    if unexpected:
        raise StructuredReviewSetParseError(f"review set result contains unexpected fields: {sorted(unexpected)}")
    missing_required = {"schema_version", "findings"} - set(payload)
    if missing_required:
        raise StructuredReviewSetParseError(f"review set result is missing fields: {sorted(missing_required)}")
    if payload.get("schema_version") != RESULT_SCHEMA_VERSION:
        raise StructuredReviewSetParseError(f"schema_version must be {RESULT_SCHEMA_VERSION}")
    findings = _require_list(payload, "findings", StructuredReviewSetParseError)
    relationship_summary, relationship_warnings = normalize_report_text_list(payload, "relationship_summary")
    notes, notes_warnings = normalize_report_text_list(payload, "notes")
    test_gaps, test_gap_warnings = normalize_report_text_list(payload, "test_gaps")
    good, good_warnings = normalize_report_text_list(payload, "good")
    parsed_findings, rejected = parse_findings_isolated(findings, _parse_finding, StructuredReviewSetParseError)
    warnings = [*relationship_warnings, *notes_warnings, *test_gap_warnings, *good_warnings]
    return StructuredReviewSetResult(
        schema_version=RESULT_SCHEMA_VERSION,
        findings=tuple(parsed_findings),
        relationship_summary=relationship_summary,
        notes=notes,
        test_gaps=test_gaps,
        good=good,
        structured_parse_status="partial" if rejected or warnings else "success",
        rejected_findings=rejected,
        normalization_warnings=warnings,
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
    old_path, new_path, old_line, new_line, side = parse_review_position(
        value,
        error_type=StructuredReviewSetParseError,
        context=context,
        allow_none=False,
    )
    return ReviewSetTargetPosition(
        old_path=old_path,
        new_path=new_path,
        old_line=old_line,
        new_line=new_line,
        side=side,
    )


def _evidence_to_dict(evidence: ReviewSetEvidenceRef) -> dict[str, object]:
    return {
        "member_id": evidence.member_id,
        "path": evidence.path,
        "start_line": evidence.start_line,
        "end_line": evidence.end_line,
        "detail": evidence.detail,
    }
