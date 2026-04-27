from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path


def split_command(command: str) -> list[str]:
    # Windows 路径包含反斜杠，不能使用 POSIX 模式拆分命令。
    return shlex.split(command, posix=(os.name != "nt"))


def prepare_command(args: list[str]) -> list[str]:
    if os.name != "nt" or not args:
        return args

    executable = shutil.which(args[0])
    if not executable:
        return args

    if Path(executable).suffix.lower() not in {".bat", ".cmd"}:
        return args

    # CreateProcess 直接执行批处理文件存在兼容性问题，显式交给 cmd.exe。
    command_line = subprocess.list2cmdline([executable, *args[1:]])
    return ["cmd.exe", "/d", "/s", "/c", command_line]


def format_command(args: list[str]) -> str:
    return subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)
