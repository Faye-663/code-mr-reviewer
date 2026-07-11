from __future__ import annotations

import json
from dataclasses import dataclass, field

ALLOWED_SEVERITIES = {"suggestion", "minjor", "major", "fatal"}
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


@dataclass(frozen=True, slots=True)
class StructuredReviewResult:
    findings: list[ReviewFinding]
    notes: list[str]
    test_gaps: list[str]
    good: list[str] = field(default_factory=list)


def parse_review_plan(raw_output: str) -> dict[str, object]:
    try:
        payload = json.loads(raw_output)
    except json.JSONDecodeError as exc:
        raise ReviewPlanParseError(f"review plan output must be valid JSON: {exc}") from exc
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
    parsed_paths = []
    for index, item in enumerate(critical_paths):
        if not isinstance(item, dict):
            raise ReviewPlanParseError(f"critical_paths[{index}] must be an object")
        unexpected = set(item) - {"path", "reason", "verify"}
        if unexpected:
            raise ReviewPlanParseError(f"critical_paths[{index}] contains unexpected fields: {sorted(unexpected)}")
        path = item.get("path")
        reason = item.get("reason")
        if not isinstance(path, str) or not path.strip():
            raise ReviewPlanParseError(f"critical_paths[{index}].path must be a non-empty string")
        if not isinstance(reason, str) or not reason.strip():
            raise ReviewPlanParseError(f"critical_paths[{index}].reason must be a non-empty string")
        verify = _review_plan_text_list(item, "verify", prefix=f"critical_paths[{index}].")
        if not verify:
            raise ReviewPlanParseError(f"critical_paths[{index}].verify must not be empty")
        parsed_paths.append({"path": path, "reason": reason, "verify": verify})
    plan["critical_paths"] = parsed_paths
    return plan


def _review_plan_text_list(payload: dict, field: str, prefix: str = "") -> list[str]:
    value = payload.get(field)
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ReviewPlanParseError(f"{prefix}{field} must be a list of non-empty strings")
    return value


def parse_structured_review_result(raw_output: str) -> StructuredReviewResult:
    try:
        payload = json.loads(raw_output)
    except json.JSONDecodeError as exc:
        raise StructuredReviewParseError(f"review output must be valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise StructuredReviewParseError("review output must be a JSON object")
    unexpected_fields = set(payload) - {"findings", "notes", "test_gaps", "good"}
    if unexpected_fields:
        raise StructuredReviewParseError(f"review output contains unexpected fields: {sorted(unexpected_fields)}")

    findings = _require_list(payload, "findings")
    return StructuredReviewResult(
        findings=[_parse_finding(item, index) for index, item in enumerate(findings)],
        notes=_optional_text_list(payload, "notes"),
        test_gaps=_optional_text_list(payload, "test_gaps"),
        good=_optional_text_list(payload, "good"),
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

    return ReviewFinding(
        rule_id=_require_text(value, "rule_id", index),
        severity=severity,
        confidence=confidence,
        old_path=_require_text(value, "old_path", index),
        new_path=_require_text(value, "new_path", index),
        old_line=_require_int(value, "old_line", index),
        new_line=_require_int(value, "new_line", index),
        title=_require_text(value, "title", index),
        evidence=_require_text(value, "evidence", index),
        impact=_require_text(value, "impact", index),
        suggestion=_require_text(value, "suggestion", index),
    )


def _require_list(payload: dict, field: str) -> list:
    value = payload.get(field)
    if not isinstance(value, list):
        raise StructuredReviewParseError(f"{field} must be a list")
    return value


def _optional_text_list(payload: dict, field: str) -> list[str]:
    value = payload.get(field, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise StructuredReviewParseError(f"{field} must be a list of strings")
    return value


def _require_text(payload: dict, field: str, index: int) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise StructuredReviewParseError(f"findings[{index}].{field} must be a non-empty string")
    return value


def _require_int(payload: dict, field: str, index: int) -> int:
    value = payload.get(field)
    if not isinstance(value, int):
        raise StructuredReviewParseError(f"findings[{index}].{field} must be an integer")
    return value
