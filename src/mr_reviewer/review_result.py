from __future__ import annotations

from dataclasses import dataclass, field

from mr_reviewer.result_validation import (
    normalize_report_text_list,
    parse_findings_isolated,
    parse_review_position,
)
from mr_reviewer.structured_output import parse_json_object_output

ALLOWED_SEVERITIES = {"suggestion", "minor", "major", "fatal"}
ALLOWED_CONFIDENCES = {"HIGH", "MEDIUM", "LOW"}
REVIEW_PLAN_LIST_FIELDS = (
    "change_intent",
    "external_contracts",
    "state_invariants",
    "transaction_async_boundaries",
    "test_risks",
    "open_questions",
)


class StructuredReviewParseError(ValueError):
    pass


class ReviewPlanParseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ReviewFinding:
    rule_id: str
    severity: str
    confidence: str
    old_path: str
    new_path: str
    old_line: int
    new_line: int
    title: str
    evidence: str
    impact: str
    suggestion: str
    position_side: str = "legacy"


@dataclass(frozen=True, slots=True)
class StructuredReviewResult:
    findings: list[ReviewFinding]
    notes: list[str]
    test_gaps: list[str]
    good: list[str] = field(default_factory=list)
    structured_parse_status: str = "success"
    rejected_findings: list[dict[str, object]] = field(default_factory=list)
    normalization_warnings: list[dict[str, str]] = field(default_factory=list)


def parse_review_plan(raw_output: str) -> dict[str, object]:
    return parse_json_object_output(
        raw_output,
        output_type="review_plan",
        error_label="review plan",
        error_type=ReviewPlanParseError,
        parse_object=_parse_review_plan_object,
    )


def _parse_review_plan_object(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ReviewPlanParseError("review plan output must be a JSON object")
    expected_fields = {*REVIEW_PLAN_LIST_FIELDS, "critical_paths"}
    unexpected_fields = set(payload) - expected_fields
    if unexpected_fields:
        raise ReviewPlanParseError(f"review plan output contains unexpected fields: {sorted(unexpected_fields)}")

    plan: dict[str, object] = {}
    for field in REVIEW_PLAN_LIST_FIELDS:
        plan[field] = _review_plan_text_list(payload, field)

    critical_paths = payload.get("critical_paths")
    if not isinstance(critical_paths, list):
        raise ReviewPlanParseError("critical_paths must be a list")
    plan["critical_paths"] = [
        _parse_review_plan_critical_path(item, index) for index, item in enumerate(critical_paths)
    ]
    return plan


def _parse_review_plan_critical_path(value: object, index: int) -> dict[str, object]:
    context = f"critical_paths[{index}]"
    if not isinstance(value, dict):
        raise ReviewPlanParseError(f"{context} must be an object")
    unexpected = set(value) - {"path", "reason", "verify"}
    if unexpected:
        raise ReviewPlanParseError(f"{context} contains unexpected fields: {sorted(unexpected)}")
    path = value.get("path")
    reason = value.get("reason")
    if not isinstance(path, str) or not path.strip():
        raise ReviewPlanParseError(f"{context}.path must be a non-empty string")
    if not isinstance(reason, str) or not reason.strip():
        raise ReviewPlanParseError(f"{context}.reason must be a non-empty string")
    verify = _review_plan_text_list(value, "verify", prefix=f"{context}.")
    if not verify:
        raise ReviewPlanParseError(f"{context}.verify must not be empty")
    return {"path": path, "reason": reason, "verify": verify}


def _review_plan_text_list(payload: dict, field: str, prefix: str = "") -> list[str]:
    value = payload.get(field)
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ReviewPlanParseError(f"{prefix}{field} must be a list of non-empty strings")
    return value


def parse_structured_review_result(raw_output: str) -> StructuredReviewResult:
    return parse_json_object_output(
        raw_output,
        output_type="review_result",
        error_label="review",
        error_type=StructuredReviewParseError,
        parse_object=_parse_structured_review_object,
        prefer_authoritative_agent_output=True,
    )


def _parse_structured_review_object(payload: object) -> StructuredReviewResult:
    if not isinstance(payload, dict):
        raise StructuredReviewParseError("review output must be a JSON object")
    unexpected_fields = set(payload) - {"findings", "notes", "test_gaps", "good"}
    if unexpected_fields:
        raise StructuredReviewParseError(f"review output contains unexpected fields: {sorted(unexpected_fields)}")

    findings = _require_list(payload, "findings")
    parsed_findings, rejected = parse_findings_isolated(findings, _parse_finding, StructuredReviewParseError)
    notes, notes_warnings = normalize_report_text_list(payload, "notes")
    test_gaps, test_gap_warnings = normalize_report_text_list(payload, "test_gaps")
    good, good_warnings = normalize_report_text_list(payload, "good")
    warnings = [*notes_warnings, *test_gap_warnings, *good_warnings]
    return StructuredReviewResult(
        findings=parsed_findings,
        notes=notes,
        test_gaps=test_gaps,
        good=good,
        structured_parse_status="partial" if rejected or warnings else "success",
        rejected_findings=rejected,
        normalization_warnings=warnings,
    )


def _parse_finding(value: object, index: int) -> ReviewFinding:
    if not isinstance(value, dict):
        raise StructuredReviewParseError(f"findings[{index}] must be an object")

    severity = _require_text(value, "severity", index)
    if severity not in ALLOWED_SEVERITIES:
        raise StructuredReviewParseError(
            f"findings[{index}].severity must be one of {sorted(ALLOWED_SEVERITIES)}"
        )

    confidence = _require_text(value, "confidence", index)
    if confidence not in ALLOWED_CONFIDENCES:
        raise StructuredReviewParseError(
            f"findings[{index}].confidence must be one of {sorted(ALLOWED_CONFIDENCES)}"
        )

    if "position" in value:
        old_path, new_path, old_line, new_line, side = parse_review_position(
            value.get("position"),
            error_type=StructuredReviewParseError,
            context=f"findings[{index}].position",
            allow_none=True,
        )
    else:
        old_path, new_path, old_line, new_line, side = parse_review_position(
            {field: value[field] for field in ("old_path", "new_path", "old_line", "new_line") if field in value},
            error_type=StructuredReviewParseError,
            context=f"findings[{index}].position",
            allow_none=True,
        )

    return ReviewFinding(
        rule_id=_require_text(value, "rule_id", index),
        severity=severity,
        confidence=confidence,
        old_path=old_path,
        new_path=new_path,
        old_line=old_line,
        new_line=new_line,
        title=_require_text(value, "title", index),
        evidence=_require_text(value, "evidence", index),
        impact=_require_text(value, "impact", index),
        suggestion=_require_text(value, "suggestion", index),
        position_side=side,
    )


def _require_list(payload: dict, field: str) -> list:
    value = payload.get(field)
    if not isinstance(value, list):
        raise StructuredReviewParseError(f"{field} must be a list")
    return value


def _require_text(payload: dict, field: str, index: int) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise StructuredReviewParseError(f"findings[{index}].{field} must be a non-empty string")
    return value
