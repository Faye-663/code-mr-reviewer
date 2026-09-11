from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar


T = TypeVar("T")


def require_object(value: object, error_type, context: str) -> dict:
    if not isinstance(value, dict):
        raise error_type(f"{context} must be an object")
    return value


def require_list(payload: dict, field: str, error_type) -> list:
    value = payload.get(field)
    if not isinstance(value, list):
        raise error_type(f"{field} must be a list")
    return value


def require_text(payload: dict, field: str, error_type, context: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise error_type(f"{context}.{field} must be a non-empty string")
    return value.strip()


def require_integer(payload: dict, field: str, error_type, context: str) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise error_type(f"{context}.{field} must be an integer")
    return value


def require_text_list(payload: dict, field: str, error_type, context: str = "") -> list[str]:
    value = require_list(payload, field, error_type)
    if not all(isinstance(item, str) and item.strip() for item in value):
        prefix = f"{context}." if context else ""
        raise error_type(f"{prefix}{field} must contain non-empty strings")
    return [item.strip() for item in value]


def require_exact_fields(payload: dict, expected: set[str], error_type, context: str) -> None:
    unexpected = set(payload) - expected
    missing = expected - set(payload)
    if unexpected:
        raise error_type(f"{context} contains unexpected fields: {sorted(unexpected)}")
    if missing:
        raise error_type(f"{context} is missing fields: {sorted(missing)}")


def normalize_report_text_list(payload: dict, field: str) -> tuple[list[str], list[dict[str, str]]]:
    """归一化非关键报告字段；只记录发生类型修复或丢弃时的审计 warning。"""
    if field not in payload:
        return [], []
    value = payload[field]
    if value is None:
        return [], [{"field": field, "reason_code": "null_report_field_defaulted"}]
    if isinstance(value, str):
        normalized = value.strip()
        return ([normalized] if normalized else []), [
            {"field": field, "reason_code": "string_report_field_wrapped"}
        ]
    if not isinstance(value, list):
        return [], [{"field": field, "reason_code": "invalid_report_field_dropped"}]

    normalized = [item.strip() for item in value if isinstance(item, str) and item.strip()]
    if len(normalized) == len(value):
        return normalized, []
    return normalized, [{"field": field, "reason_code": "invalid_report_items_dropped"}]


def parse_findings_isolated(
        values: list,
        parse_item: Callable[[object, int], T],
        error_type: type[ValueError],
) -> tuple[list[T], list[dict[str, object]]]:
    parsed: list[T] = []
    rejected: list[dict[str, object]] = []
    for index, value in enumerate(values):
        try:
            parsed.append(parse_item(value, index))
        except error_type:
            rejected.append(
                {
                    "index": index,
                    "reason_code": "invalid_finding_contract",
                    "summary": _safe_finding_summary(value),
                }
            )
    return parsed, rejected


def _safe_finding_summary(value: object) -> str:
    if not isinstance(value, dict):
        return "unidentified finding"
    identifiers = []
    for field in ("issue_id", "rule_id", "title"):
        item = value.get(field)
        if isinstance(item, str) and item.strip():
            identifiers.append(item.strip()[:120])
    return " | ".join(identifiers) if identifiers else "unidentified finding"
