from __future__ import annotations


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
