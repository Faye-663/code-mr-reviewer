from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from collections.abc import Callable
from typing import TypeVar


LOG = logging.getLogger("mr_reviewer")
T = TypeVar("T")


class AgentOutput(str):
    """保留顶层 assistant 消息边界及显式 final，同时兼容既有字符串协议。"""

    def __new__(
            cls,
            messages: tuple[str, ...] | list[str],
            final_text: str | None = None,
    ) -> "AgentOutput":
        ordered_messages = tuple(messages)
        combined = list(ordered_messages)
        if final_text and final_text not in combined:
            combined.append(final_text)
        value = super().__new__(cls, "\n\n".join(combined))
        value.messages = ordered_messages
        value.final_text = final_text
        return value


@dataclass(frozen=True, slots=True)
class _ValidCandidate:
    parsed: object
    canonical_json: str


def parse_json_object_output(
        raw_output: str,
        *,
        output_type: str,
        error_label: str,
        error_type: type[ValueError],
        parse_object: Callable[[object], T],
        prefer_authoritative_agent_output: bool = False,
) -> T:
    """解析模型 JSON；仅在外层文本破坏整段 JSON 时恢复唯一合法契约对象。"""
    if prefer_authoritative_agent_output and isinstance(raw_output, AgentOutput):
        selected = _select_authoritative_agent_output(
            raw_output,
            output_type=output_type,
            error_label=error_label,
            error_type=error_type,
            parse_object=parse_object,
        )
        if selected is not None:
            return selected
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


def _select_authoritative_agent_output(
        raw_output: AgentOutput,
        *,
        output_type: str,
        error_label: str,
        error_type: type[ValueError],
        parse_object: Callable[[object], T],
) -> T | None:
    sources: list[tuple[str, str]] = []
    if raw_output.final_text:
        sources.append(("final", raw_output.final_text))
    sources.extend(("assistant", message) for message in reversed(raw_output.messages))

    for source, text in sources:
        candidates = _valid_contract_candidates(text, error_type=error_type, parse_object=parse_object)
        if len(candidates) > 1:
            raise error_type(f"{error_label} output contains multiple valid JSON objects")
        if not candidates:
            continue
        candidate = candidates[0]
        LOG.info(
            "stage=structured_output_select output=%s status=selected source=%s "
            "message_count=%s payload_chars=%s",
            output_type,
            source,
            len(raw_output.messages),
            len(candidate.canonical_json),
        )
        return candidate.parsed  # type: ignore[return-value]
    return None


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
    valid_candidates: list[tuple[int, int, _ValidCandidate]] = []
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
        valid_candidates.append(
            (start, end, _ValidCandidate(parsed, _canonical_json(payload)))
        )

    valid_candidates = _deduplicate_candidates(valid_candidates)

    if len(valid_candidates) > 1:
        raise error_type(f"{error_label} output contains multiple valid JSON objects") from strict_error

    if len(valid_candidates) == 1:
        start, end, candidate = valid_candidates[0]
        LOG.warning(
            "stage=structured_output_normalize output=%s status=recovered "
            "prefix_chars=%s suffix_chars=%s candidate_count=%s",
            output_type,
            start,
            len(raw_output) - end,
            len(decoded_candidates),
        )
        return candidate.parsed  # type: ignore[return-value]

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


def _valid_contract_candidates(
        raw_output: str,
        *,
        error_type: type[ValueError],
        parse_object: Callable[[object], T],
) -> list[_ValidCandidate]:
    try:
        payload = json.loads(raw_output)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        decoded = []
        for start, character in enumerate(raw_output):
            if character != "{":
                continue
            try:
                payload, end = decoder.raw_decode(raw_output, start)
            except json.JSONDecodeError:
                continue
            decoded.append((start, end, payload))
        payloads = [payload for _, _, payload in _outermost_candidates(decoded)]
    else:
        payloads = [payload]

    candidates: dict[str, _ValidCandidate] = {}
    for payload in payloads:
        try:
            parsed = parse_object(payload)
        except error_type:
            continue
        canonical = _canonical_json(payload)
        candidates.setdefault(canonical, _ValidCandidate(parsed, canonical))
    return list(candidates.values())


def _deduplicate_candidates(
        candidates: list[tuple[int, int, _ValidCandidate]],
) -> list[tuple[int, int, _ValidCandidate]]:
    unique: dict[str, tuple[int, int, _ValidCandidate]] = {}
    for candidate in candidates:
        unique.setdefault(candidate[2].canonical_json, candidate)
    return list(unique.values())


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
