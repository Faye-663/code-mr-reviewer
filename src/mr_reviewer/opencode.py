from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from mr_reviewer.process import format_command, prepare_command

LOG = logging.getLogger("mr_reviewer")
PROMPT_FILE_MESSAGE = "请读取附件 prompt.md，并严格按其中内容执行代码审查。"
PROMPT_TRANSPORTS = {"argument", "file"}


class OpenCodeRunner:
    def __init__(
            self,
            command: str = "opencode",
            debug: bool = True,
            diagnostic_dir: Path | None = None,
            prompt_transport: str = "argument",
    ):
        if prompt_transport not in PROMPT_TRANSPORTS:
            raise ValueError(f"unsupported opencode prompt transport: {prompt_transport}")
        self.command = command
        self.debug = debug
        self.diagnostic_dir = diagnostic_dir
        self.prompt_transport = prompt_transport

    def run_review(self, prompt: str, cwd: Path, timeout_seconds: int) -> str:
        args = shlex.split(self.command, posix=(os.name != "nt"))
        if self.debug:
            args += ["--print-logs", "--log-level", "DEBUG"]
        prompt_sha256 = _prompt_sha256(prompt)
        diagnostic_path = self._create_diagnostic_path(prompt_sha256) if self.diagnostic_dir else None
        prompt_file = None
        cleanup_prompt_file = False
        if self.prompt_transport == "file":
            prompt_file, cleanup_prompt_file = self._write_prompt_transfer_file(prompt, diagnostic_path)
            args += ["run", "--file", str(prompt_file), PROMPT_FILE_MESSAGE]
        else:
            args += ["run", prompt]
        LOG.info(
            "stage=opencode command=%s cwd=%s prompt_transport=%s prompt_chars=%s prompt_sha256=%s "
            "mr_url_present=%s diagnostic_path=%s",
            _command_for_log(args, redact_prompt=self.prompt_transport == "argument"),
            cwd,
            self.prompt_transport,
            len(prompt),
            prompt_sha256,
            _has_mr_url(prompt),
            diagnostic_path or "",
        )
        if diagnostic_path:
            self._write_diagnostic_inputs(diagnostic_path, args, prompt, cwd, prompt_sha256)
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
            return result.stdout.strip()
        finally:
            if cleanup_prompt_file and prompt_file:
                prompt_file.unlink(missing_ok=True)

    def _create_diagnostic_path(self, prompt_sha256: str) -> Path:
        path = self.diagnostic_dir / f"opencode-{prompt_sha256[:12]}-{uuid.uuid4().hex[:8]}"
        path.mkdir(parents=True, exist_ok=False)
        return path

    def _write_prompt_transfer_file(self, prompt: str, diagnostic_path: Path | None) -> tuple[Path, bool]:
        if diagnostic_path:
            prompt_file = diagnostic_path / "prompt.md"
            prompt_file.write_text(prompt, encoding="utf-8")
            return prompt_file, False

        # 非诊断模式也允许验证文件传输；临时文件只用于本次 opencode 调用，结束后清理。
        with tempfile.NamedTemporaryFile(
                mode="w",
                prefix="mr-reviewer-opencode-prompt-",
                suffix=".md",
                delete=False,
                encoding="utf-8",
        ) as file:
            file.write(prompt)
            return Path(file.name), True

    def _write_diagnostic_inputs(self, diagnostic_path: Path, args: list[str], prompt: str, cwd: Path,
                                 prompt_sha256: str) -> None:
        diagnostic_path.joinpath("prompt.md").write_text(prompt, encoding="utf-8")
        diagnostic_path.joinpath("cwd.txt").write_text(str(cwd), encoding="utf-8")
        diagnostic_path.joinpath("command.txt").write_text(
            _command_for_log(args, prompt_sha256, redact_prompt=self.prompt_transport == "argument"),
            encoding="utf-8",
        )
        diagnostic_path.joinpath("env-summary.json").write_text(
            json.dumps(_env_summary(args[0], self.debug), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _write_diagnostic_result(self, diagnostic_path: Path, result: subprocess.CompletedProcess[str]) -> None:
        diagnostic_path.joinpath("stdout.md").write_text(result.stdout or "", encoding="utf-8")
        diagnostic_path.joinpath("stderr.log").write_text(result.stderr or "", encoding="utf-8")
        diagnostic_path.joinpath("returncode.txt").write_text(str(result.returncode), encoding="utf-8")


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
    related_prefixes = ("OPENCODE", "CODEX")
    related_names = sorted(name for name in os.environ if name.upper().startswith(related_prefixes))
    return {
        "debug": debug,
        "executable": command,
        "resolved_executable": shutil.which(command),
        "path_entry_count": len(os.environ.get("PATH", "").split(os.pathsep)) if os.environ.get("PATH") else 0,
        "related_env_names": related_names,
    }
