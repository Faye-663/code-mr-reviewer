from __future__ import annotations

import base64
import contextlib
import contextvars
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


LOG = logging.getLogger("mr_reviewer")
_TASK_CONTEXT: contextvars.ContextVar["TaskContext | None"] = contextvars.ContextVar(
    "mr_reviewer_task_context", default=None
)


@dataclass(frozen=True, slots=True)
class TaskContext:
    task_id: str
    debug_dir: Path
    enabled: bool
    stage: str = ""


def configure_logging(level: str) -> None:
    """根据全局配置启用安全的控制台日志；OFF 必须压制所有项目日志。"""
    logging.disable(logging.NOTSET)
    if level == "OFF":
        logging.disable(logging.CRITICAL)
        return
    logging.basicConfig(
        level=logging.DEBUG if level == "DEBUG" else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


@contextlib.contextmanager
def task_context(task_id: str, debug_dir: Path, enabled: bool, stage: str = "") -> Iterator[None]:
    token = _TASK_CONTEXT.set(TaskContext(task_id=task_id, debug_dir=debug_dir, enabled=enabled, stage=stage))
    try:
        yield
    finally:
        _TASK_CONTEXT.reset(token)


@contextlib.contextmanager
def task_stage(stage: str) -> Iterator[None]:
    current = _TASK_CONTEXT.get()
    if current is None:
        yield
        return
    token = _TASK_CONTEXT.set(TaskContext(current.task_id, current.debug_dir, current.enabled, stage))
    try:
        yield
    finally:
        _TASK_CONTEXT.reset(token)


def current_task_context() -> TaskContext | None:
    return _TASK_CONTEXT.get()


def write_debug_json(category: str, name: str, payload: object, token: str = "") -> Path | None:
    context = _TASK_CONTEXT.get()
    if context is None or not context.enabled:
        return None
    path = _artifact_path(context, category, name, ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_redact_value(payload, token), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


def write_debug_text(category: str, name: str, suffix: str, content: str, token: str = "") -> Path | None:
    context = _TASK_CONTEXT.get()
    if context is None or not context.enabled:
        return None
    path = _artifact_path(context, category, name, suffix)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(redact_text(content, token), encoding="utf-8")
    return path


def redact_text(value: str, token: str = "") -> str:
    result = value
    if token:
        encoded = base64.b64encode(f"oauth2:{token}".encode("utf-8")).decode("ascii")
        result = result.replace(token, "<redacted>").replace(encoded, "<redacted>")
    result = re.sub(r"(?im)(private-token|authorization)\s*[:=]\s*[^\s,;]+", r"\1: <redacted>", result)
    result = re.sub(r"(?i)basic\s+[A-Za-z0-9+/=]+", "Basic <redacted>", result)
    return result


def _artifact_path(context: TaskContext, category: str, name: str, suffix: str) -> Path:
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    timestamp = datetime.now(timezone.utc).strftime("%H%M%S%f")
    safe_task_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", context.task_id)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_") or "call"
    return context.debug_dir / day / safe_task_id / category / f"{timestamp}-{safe_name}{suffix}"


def _redact_value(value: object, token: str) -> object:
    if isinstance(value, str):
        return redact_text(value, token)
    if isinstance(value, dict):
        return {str(key): _redact_value(item, token) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item, token) for item in value]
    return value
