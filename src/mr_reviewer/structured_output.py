from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import TypeVar


LOG = logging.getLogger("mr_reviewer")
T = TypeVar("T")


def parse_json_object_output(
        raw_output: str,
        *,
        output_type: str,
        error_label: str,
        error_type: type[ValueError],
        parse_object: Callable[[object], T],
) -> T:
    """解析模型 JSON；仅在外层文本破坏整段 JSON 时恢复唯一合法契约对象。"""
    try:
        payload = json.loads(raw_output)
    except json.JSONDecodeError as strict_error:
        return _recover_contract_object(
            raw_output,
            output_type=output_type,
            error_label=error_label,
            error_type=error_type,
            parse_object=parse_object,
            strict_error=strict_error,
        )
    return parse_object(payload)


def _recover_contract_object(
        raw_output: str,
        *,
        output_type: str,
        error_label: str,
        error_type: type[ValueError],
        parse_object: Callable[[object], T],
        strict_error: json.JSONDecodeError,
) -> T:
    decoder = json.JSONDecoder()
    decoded_candidates: list[tuple[int, int, object]] = []
    valid_candidates: list[tuple[int, int, T]] = []
    validation_errors: list[ValueError] = []

    # 模型输出属于不可信边界；候选必须再次通过完整业务契约，不能仅凭花括号截取。
    for start, character in enumerate(raw_output):
        if character != "{":
            continue
        try:
            payload, end = decoder.raw_decode(raw_output, start)
        except json.JSONDecodeError:
            continue
        decoded_candidates.append((start, end, payload))

    decoded_candidates = _outermost_candidates(decoded_candidates)
    for start, end, payload in decoded_candidates:
        try:
            parsed = parse_object(payload)
        except error_type as exc:
            validation_errors.append(exc)
            continue
        valid_candidates.append((start, end, parsed))

    if len(valid_candidates) > 1:
        raise error_type(f"{error_label} output contains multiple valid JSON objects") from strict_error

    if len(valid_candidates) == 1:
        start, end, parsed = valid_candidates[0]
        LOG.warning(
            "stage=structured_output_normalize output=%s status=recovered "
            "prefix_chars=%s suffix_chars=%s candidate_count=%s",
            output_type,
            start,
            len(raw_output) - end,
            len(decoded_candidates),
        )
        return parsed

    if len(decoded_candidates) == 1 and len(validation_errors) == 1:
        raise validation_errors[0] from strict_error
    if decoded_candidates:
        raise error_type(
            f"{error_label} output does not contain a valid JSON object matching the required contract"
        ) from strict_error
    raise error_type(f"{error_label} output must be valid JSON: {strict_error}") from strict_error


def _outermost_candidates(candidates: list[tuple[int, int, object]]) -> list[tuple[int, int, object]]:
    outermost: list[tuple[int, int, object]] = []
    for candidate in sorted(candidates, key=lambda item: (item[0], -item[1])):
        start, end, _ = candidate
        if any(parent_start <= start and end <= parent_end for parent_start, parent_end, _ in outermost):
            continue
        outermost.append(candidate)
    return outermost
