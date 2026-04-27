from __future__ import annotations

import logging
import os
import shlex
import subprocess
from pathlib import Path

from mr_reviewer.process import format_command, prepare_command


LOG = logging.getLogger("mr_reviewer")


class OpenCodeRunner:
    def __init__(self, command: str = "opencode"):
        self.command = command

    def run_review(self, prompt: str, cwd: Path, timeout_seconds: int) -> str:
        args = shlex.split(self.command, posix=(os.name != "nt")) + ["run", prompt]
        LOG.info("stage=opencode command=%s cwd=%s", _command_for_log(args), cwd)
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
        if result.returncode != 0:
            raise RuntimeError(f"opencode run failed: {result.stderr.strip()}")
        return result.stdout.strip()


def _command_for_log(args: list[str]) -> str:
    safe_args = []
    redact_next = False
    for arg in args:
        if redact_next:
            safe_args.append(f"<prompt_chars={len(arg)}>")
            redact_next = False
            continue
        safe_args.append(arg)
        if arg == "run":
            redact_next = True
    return format_command(prepare_command(safe_args))
