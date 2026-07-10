from __future__ import annotations

import json
from dataclasses import dataclass

ALLOWED_SEVERITIES = {"suggestion", "minjor", "major", "fatal"}
ALLOWED_CONFIDENCES = {"HIGH", "MEDIUM", "LOW"}
SUMMARY_LIST_FIELDS = ("change_areas", "behavior_changes", "risk_areas", "test_changes")


class StructuredReviewParseError(ValueError):
    pass


class ReviewSummaryParseError(ValueError):
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
    suggestion: str


@dataclass(frozen=True, slots=True)
class StructuredReviewResult:
    findings: list[ReviewFinding]
    notes: list[str]
    test_gaps: list[str]


def parse_review_summary(raw_output: str) -> dict[str, object]:
    try:
        payload = json.loads(raw_output)
    except json.JSONDecodeError as exc:
        raise ReviewSummaryParseError(f"summary output must be valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ReviewSummaryParseError("summary output must be a JSON object")
    expected_fields = {"overview", *SUMMARY_LIST_FIELDS}
    unexpected_fields = set(payload) - expected_fields
    if unexpected_fields:
        raise ReviewSummaryParseError(f"summary output contains unexpected fields: {sorted(unexpected_fields)}")

    overview = payload.get("overview")
    if not isinstance(overview, str) or not overview.strip():
        raise ReviewSummaryParseError("overview must be a non-empty string")

    summary: dict[str, object] = {"overview": overview}
    for field in SUMMARY_LIST_FIELDS:
        value = payload.get(field)
        if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
            raise ReviewSummaryParseError(f"{field} must be a list of non-empty strings")
        summary[field] = value
    return summary


def parse_structured_review_result(raw_output: str) -> StructuredReviewResult:
    try:
        payload = json.loads(raw_output)
    except json.JSONDecodeError as exc:
        raise StructuredReviewParseError(f"review output must be valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise StructuredReviewParseError("review output must be a JSON object")

    findings = _require_list(payload, "findings")
    return StructuredReviewResult(
        findings=[_parse_finding(item, index) for index, item in enumerate(findings)],
        notes=_optional_text_list(payload, "notes"),
        test_gaps=_optional_text_list(payload, "test_gaps"),
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
