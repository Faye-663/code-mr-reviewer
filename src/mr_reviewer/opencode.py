from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from mr_reviewer.observability import current_task_context, redact_text
from mr_reviewer.prompting import PromptMetadata
from mr_reviewer.process import format_command, prepare_command

LOG = logging.getLogger("mr_reviewer")
PROMPT_FILE_MESSAGE = "Follow the instructions in the attached file."
PROMPT_TRANSPORTS = {"argument", "file"}


class AgentRunner(Protocol):
    def run_review(
            self, prompt: str, cwd: Path, timeout_seconds: int, prompt_metadata: PromptMetadata | None = None
    ) -> str:
        ...


class OpenCodeRunner:
    def __init__(
            self,
            command: str = "opencode",
            debug: bool = False,
            diagnostic_dir: Path | None = None,
            redaction_token: str = "",
            prompt_transport: str = "argument",
    ):
        if prompt_transport not in PROMPT_TRANSPORTS:
            raise ValueError(f"unsupported opencode prompt transport: {prompt_transport}")
        self.command = command
        self.debug = debug
        self.diagnostic_dir = diagnostic_dir
        self.redaction_token = redaction_token
        # argument 仅作为旧配置兼容输入；实际传输始终使用安全的文件附件。
        self.prompt_transport = "file"

    def run_review(
            self, prompt: str, cwd: Path, timeout_seconds: int, prompt_metadata: PromptMetadata | None = None
    ) -> str:
        args = shlex.split(self.command, posix=(os.name != "nt"))
        if self.debug:
            args += ["--print-logs", "--log-level", "DEBUG"]
        prompt_sha256 = _prompt_sha256(prompt)
        diagnostic_path = self._create_diagnostic_path(prompt_sha256) if self.debug and self.diagnostic_dir else None
        prompt_file = None
        cleanup_prompt_file = False
        # 多行 prompt 不进入 argv，避免 Windows 批处理重解析，并统一 Linux/Windows 行为。
        prompt_file, cleanup_prompt_file = self._write_prompt_transfer_file(prompt, diagnostic_path)
        # OpenCode 的 --file 是数组参数；位置参数必须放在它之前，避免被解析为额外附件。
        args += ["run", "--format", "json", PROMPT_FILE_MESSAGE, "--file", str(prompt_file)]
        LOG.info(
            "stage=opencode command=%s cwd=%s prompt_transport=%s prompt_chars=%s prompt_sha256=%s "
            "mr_url_present=%s template_id=%s template_version=%s diagnostic_path=%s",
            _command_for_log(args, redact_prompt=False),
            cwd,
            self.prompt_transport,
            len(prompt),
            prompt_sha256,
            _has_mr_url(prompt),
            prompt_metadata.template_id if prompt_metadata else "",
            prompt_metadata.template_version if prompt_metadata else "",
            diagnostic_path or "",
        )
        if diagnostic_path:
            self._write_diagnostic_inputs(diagnostic_path, args, prompt, cwd, prompt_sha256, prompt_metadata)
        try:
            result = subprocess.run(
                prepare_command(args),
                cwd=cwd,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
            if diagnostic_path:
                self._write_diagnostic_result(diagnostic_path, result)
            if result.returncode != 0:
                raise RuntimeError(f"opencode run failed: {result.stderr.strip()}")
            return "\n\n".join(_extract_opencode_json_texts(result.stdout or ""))
        finally:
            if cleanup_prompt_file and prompt_file:
                prompt_file.unlink(missing_ok=True)

    def _create_diagnostic_path(self, prompt_sha256: str) -> Path:
        context = current_task_context()
        if context is None:
            task_root = self.diagnostic_dir
            stage = "review"
        else:
            day = datetime.now(timezone.utc).strftime("%Y%m%d")
            task_root = context.debug_dir / day / context.task_id / "agent"
            stage = context.stage or "review"
        timestamp = datetime.now(timezone.utc).strftime("%H%M%S%f")
        agent = "claude-code" if isinstance(self, ClaudeCodeRunner) else "opencode"
        path = task_root / f"{stage}-{agent}-{timestamp}-{prompt_sha256[:12]}"
        path.mkdir(parents=True, exist_ok=False)
        return path

    def _write_prompt_transfer_file(self, prompt: str, diagnostic_path: Path | None) -> tuple[Path, bool]:
        if diagnostic_path:
            # 诊断副本必须脱敏；实际附件保留在临时目录，避免改变传给 Agent 的原始 prompt。
            diagnostic_path.joinpath("prompt.md").write_text(
                redact_text(prompt, self.redaction_token), encoding="utf-8"
            )

        with tempfile.NamedTemporaryFile(
                mode="w",
                prefix="mr-reviewer-agent-prompt-",
                suffix=".md",
                delete=False,
                encoding="utf-8",
        ) as file:
            file.write(prompt)
            return Path(file.name), True

    def _write_diagnostic_inputs(
            self,
            diagnostic_path: Path,
            args: list[str],
            prompt: str,
            cwd: Path,
            prompt_sha256: str,
            prompt_metadata: PromptMetadata | None,
    ) -> None:
        diagnostic_path.joinpath("request.json").write_text(
            json.dumps(
                {
                    "cwd": str(cwd),
                    "command": _command_for_log(args, prompt_sha256, redact_prompt=False),
                    "prompt_sha256": prompt_sha256,
                    "prompt_chars": len(prompt),
                    "prompt_template": {
                        "id": prompt_metadata.template_id if prompt_metadata else "",
                        "version": prompt_metadata.template_version if prompt_metadata else "",
                    },
                    "environment": _env_summary(args[0], self.debug),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def _write_diagnostic_result(self, diagnostic_path: Path, result: subprocess.CompletedProcess[str]) -> None:
        diagnostic_path.joinpath("stdout.md").write_text(redact_text(result.stdout or "", self.redaction_token), encoding="utf-8")
        diagnostic_path.joinpath("stderr.log").write_text(redact_text(result.stderr or "", self.redaction_token), encoding="utf-8")
        diagnostic_path.joinpath("result.json").write_text(
            json.dumps(
                {"returncode": result.returncode, "stdout_chars": len(result.stdout or ""), "stderr_chars": len(result.stderr or "")},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )


class ClaudeCodeRunner(OpenCodeRunner):
    def run_review(
            self, prompt: str, cwd: Path, timeout_seconds: int, prompt_metadata: PromptMetadata | None = None
    ) -> str:
        args = shlex.split(self.command, posix=(os.name != "nt"))
        if self.debug:
            args += ["--debug"]
        # text 模式只返回最后一条 assistant 消息；agent 最后输出任务总结时结构化结果会丢失。
        # stream-json 会输出全部 assistant 消息事件，由下游契约解析扫描任意消息中的结果 JSON。
        args += ["-p", "--output-format", "stream-json", "--verbose"]
        prompt_sha256 = _prompt_sha256(prompt)
        diagnostic_path = self._create_diagnostic_path(prompt_sha256) if self.debug and self.diagnostic_dir else None
        LOG.info(
            "stage=claude_code command=%s cwd=%s prompt_transport=stdin prompt_chars=%s prompt_sha256=%s "
            "mr_url_present=%s template_id=%s template_version=%s diagnostic_path=%s",
            format_command(prepare_command(args)),
            cwd,
            len(prompt),
            prompt_sha256,
            _has_mr_url(prompt),
            prompt_metadata.template_id if prompt_metadata else "",
            prompt_metadata.template_version if prompt_metadata else "",
            diagnostic_path or "",
        )
        if diagnostic_path:
            self._write_diagnostic_inputs(diagnostic_path, args, prompt, cwd, prompt_sha256, prompt_metadata)
        result = subprocess.run(
            prepare_command(args),
            cwd=cwd,
            input=prompt,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        if diagnostic_path:
            self._write_diagnostic_result(diagnostic_path, result)
        if result.returncode != 0:
            raise RuntimeError(f"claude code run failed: {result.stderr.strip()}")
        return "\n\n".join(_extract_claude_stream_json_texts(result.stdout or ""))


def build_agent_runner(
        agent_type: str,
        command: str,
        *,
        debug: bool = False,
        diagnostic_dir: Path | None = None,
        redaction_token: str = "",
) -> AgentRunner:
    if agent_type == "opencode":
        return OpenCodeRunner(command, debug=debug, diagnostic_dir=diagnostic_dir, redaction_token=redaction_token, prompt_transport="file")
    if agent_type == "claude-code":
        return ClaudeCodeRunner(command, debug=debug, diagnostic_dir=diagnostic_dir, redaction_token=redaction_token, prompt_transport="file")
    raise ValueError(f"unsupported agent type: {agent_type}")


def _extract_claude_stream_json_texts(stdout: str) -> list[str]:
    """提取 Claude Code 完整 assistant 消息，不扫描 tool 事件中的不可信嵌套内容。"""
    texts: list[str] = []
    seen: set[str] = set()
    result_seen = False
    for line_number, line in enumerate(stdout.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        event = _decode_json_event(line, line_number, "claude code stream-json")
        event_type = event.get("type")
        if event_type == "result":
            result_seen = True
            if event.get("is_error") is True or event.get("subtype") != "success":
                raise RuntimeError(
                    f"claude code stream-json reported an unsuccessful result: {event.get('subtype', 'unknown')}"
                )
            result_text = event.get("result")
            if not isinstance(result_text, str):
                raise RuntimeError(f"claude code stream-json result event at line {line_number} has no text result")
            _append_unique_text(texts, seen, result_text)
            continue
        if event_type != "assistant":
            continue
        # 转发的子 Agent 文本不是顶层 review 的权威输出，不能参与 findings 候选选择。
        if event.get("parent_tool_use_id") is not None:
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            raise RuntimeError(f"claude code stream-json assistant event at line {line_number} has no message object")
        content = message.get("content")
        if not isinstance(content, list):
            raise RuntimeError(f"claude code stream-json assistant event at line {line_number} has no content list")
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if not isinstance(text, str):
                    raise RuntimeError(
                        f"claude code stream-json text block at line {line_number} has no text value"
                    )
                _append_unique_text(texts, seen, text)
    if not result_seen:
        raise RuntimeError("claude code stream-json output ended without a result event")
    if not texts:
        raise RuntimeError("claude code stream-json output did not contain assistant text")
    return texts


def _extract_opencode_json_texts(stdout: str) -> list[str]:
    """提取 OpenCode 完整 text part；工具输出不得进入业务 JSON 候选集。"""
    texts: list[str] = []
    seen: set[str] = set()
    for line_number, line in enumerate(stdout.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        event = _decode_json_event(line, line_number, "opencode json")
        event_type = event.get("type")
        if event_type == "error":
            raise RuntimeError("opencode json output reported an error event")
        if event_type != "text":
            continue
        part = event.get("part")
        if not isinstance(part, dict) or part.get("type") != "text":
            raise RuntimeError(f"opencode json text event at line {line_number} has no text part")
        text = part.get("text")
        if not isinstance(text, str):
            raise RuntimeError(f"opencode json text event at line {line_number} has no text value")
        _append_unique_text(texts, seen, text)
    if not texts:
        raise RuntimeError("opencode json output did not contain text")
    return texts


def _decode_json_event(line: str, line_number: int, protocol: str) -> dict[str, object]:
    try:
        event = json.loads(line)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{protocol} event at line {line_number} is not valid JSON") from exc
    if not isinstance(event, dict):
        raise RuntimeError(f"{protocol} event at line {line_number} must be a JSON object")
    return event


def _append_unique_text(texts: list[str], seen: set[str], value: object) -> None:
    if not isinstance(value, str):
        return
    text = value.strip()
    if text and text not in seen:
        seen.add(text)
        texts.append(text)


def _command_for_log(args: list[str], prompt_sha256: str | None = None, redact_prompt: bool = True) -> str:
    safe_args = []
    seen_run = False
    redacted_prompt = False
    for arg in args:
        if seen_run and redact_prompt and not redacted_prompt and not arg.startswith("-"):
            hash_part = f" sha256={prompt_sha256}" if prompt_sha256 else ""
            safe_args.append(f"<prompt_chars={len(arg)}{hash_part}>")
            redacted_prompt = True
            continue
        safe_args.append(arg)
        if arg == "run":
            seen_run = True
    return format_command(prepare_command(safe_args))


def _prompt_sha256(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _has_mr_url(prompt: str) -> bool:
    return "http" in prompt and "/merge_requests/" in prompt


def _env_summary(command: str, debug: bool) -> dict[str, object]:
    related_prefixes = ("OPENCODE", "CODEX", "CLAUDE", "ANTHROPIC")
    related_names = sorted(name for name in os.environ if name.upper().startswith(related_prefixes))
    return {
        "debug": debug,
        "executable": command,
        "resolved_executable": shutil.which(command),
        "path_entry_count": len(os.environ.get("PATH", "").split(os.pathsep)) if os.environ.get("PATH") else 0,
        "related_env_names": related_names,
    }
