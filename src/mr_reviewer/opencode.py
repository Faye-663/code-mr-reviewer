from __future__ import annotations

import shlex
import os
import subprocess
from pathlib import Path


class OpenCodeRunner:
    def __init__(self, command: str = "opencode"):
        self.command = command

    def run_review(self, prompt: str, cwd: Path, timeout_seconds: int) -> str:
        args = shlex.split(self.command, posix=(os.name != "nt")) + ["run", prompt]
        result = subprocess.run(
            args,
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"opencode run failed: {result.stderr.strip()}")
        return result.stdout.strip()
