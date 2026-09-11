from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import PurePosixPath
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
            normalized = re.sub(r"\s+", " ", item).replace("`", "'").strip()
            identifiers.append(normalized[:120])
    return " | ".join(identifiers) if identifiers else "unidentified finding"


def parse_review_position(
        value: object,
        *,
        error_type: type[ValueError],
        context: str,
        allow_none: bool,
) -> tuple[str, str, int, int, str]:
    """把 compact/legacy 位置规范成单侧 old/new 表示。"""
    if value is None:
        if allow_none:
            return "", "", -1, -1, "none"
        raise error_type(f"{context} must provide a position")
    if not isinstance(value, dict):
        raise error_type(f"{context} must be an object or null")

    if set(value) & {"path", "line", "side"}:
        unexpected = set(value) - {"path", "line", "side"}
        missing = {"path", "line", "side"} - set(value)
        if unexpected or missing:
            raise error_type(f"{context} compact position must contain only path, line and side")
        path = _position_path(value.get("path"), error_type, context)
        line = _position_line(value.get("line"), error_type, context)
        side = value.get("side")
        if side == "new":
            return "", path, -1, line, "new"
        if side == "old":
            return path, "", line, -1, "old"
        raise error_type(f"{context}.side must be old or new")

    unexpected = set(value) - {"old_path", "new_path", "old_line", "new_line"}
    if unexpected:
        raise error_type(f"{context} contains unexpected fields: {sorted(unexpected)}")
    old_line = _legacy_line(value.get("old_line", -1), "old_line", error_type, context)
    new_line = _legacy_line(value.get("new_line", -1), "new_line", error_type, context)

    # 旧格式保留两侧安全候选，实际 diff 映射时先新后旧；最终 GitLab 请求仍只发送命中的一侧。
    new_path = _safe_legacy_path(value.get("new_path")) if new_line > 0 else ""
    old_path = _safe_legacy_path(value.get("old_path")) if old_line > 0 else ""
    if new_path:
        return old_path, new_path, old_line if old_path else -1, new_line, "legacy"
    if old_path:
        return old_path, "", old_line, -1, "legacy"
    if allow_none and old_line == new_line == -1:
        return "", "", -1, -1, "none"
    if (new_line > 0 and value.get("new_path")) or (old_line > 0 and value.get("old_path")):
        raise error_type(f"{context}.path must be a safe relative path")
    raise error_type(f"{context} does not contain a usable old or new side")


def _position_line(value: object, error_type: type[ValueError], context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise error_type(f"{context}.line must be a positive integer")
    return value


def _legacy_line(value: object, field: str, error_type: type[ValueError], context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value == 0 or value < -1:
        raise error_type(f"{context}.{field} must be -1 or a positive integer")
    return value


def _position_path(value: object, error_type: type[ValueError], context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise error_type(f"{context}.path must be a non-empty string")
    path = value.strip()
    candidate = PurePosixPath(path)
    if (
        "\\" in path
        or path.startswith("/")
        or re.match(r"^[A-Za-z]:/", path)
        or ".." in candidate.parts
    ):
        raise error_type(f"{context}.path must be a safe relative path")
    return path


def _safe_legacy_path(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    path = value.strip()
    candidate = PurePosixPath(path)
    if (
        "\\" in path
        or path.startswith("/")
        or re.match(r"^[A-Za-z]:/", path)
        or ".." in candidate.parts
    ):
        return ""
    return path
