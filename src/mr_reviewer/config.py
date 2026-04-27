from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


ENV_PREFIX = "MR_REVIEWER_"


def _split_set(value: str | None) -> set[str]:
    if not value:
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}


def load_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


@dataclass(slots=True)
class Config:
    gitlab_base_url: str
    gitlab_token: str = ""
    im_poll_command: str = ""
    im_reply_command: str = ""
    bot_mention: str = "@Bot"
    bot_account: str = ""
    allowed_groups: set[str] = field(default_factory=set)
    allowed_users: set[str] = field(default_factory=set)
    allowed_repos: set[str] = field(default_factory=set)
    work_dir: Path = field(default_factory=lambda: Path(tempfile.gettempdir()) / "mr-review")
    state_path: Path = Path(".mr-reviewer-state.json")
    opencode_command: str = "opencode"
    max_files: int = 50
    max_diff_lines: int = 2000
    task_timeout_seconds: int = 900
    poll_interval_seconds: int = 15
    test_gitlab_responses: Path | None = None

    @classmethod
    def from_env(cls, dotenv_path: Path | None = None) -> "Config":
        dotenv_values = load_dotenv(dotenv_path or Path(".env"))

        def get(name: str, default: str = "") -> str:
            env_name = f"{ENV_PREFIX}{name}"
            value = os.environ.get(env_name, dotenv_values.get(env_name, default))
            return value if value != "" else default

        test_gitlab_responses = get("TEST_GITLAB_RESPONSES")
        return cls(
            gitlab_base_url=get("GITLAB_BASE_URL"),
            gitlab_token=get("GITLAB_TOKEN"),
            im_poll_command=get("IM_POLL_COMMAND"),
            im_reply_command=get("IM_REPLY_COMMAND"),
            bot_mention=get("BOT_MENTION", "@Bot"),
            bot_account=get("BOT_ACCOUNT"),
            allowed_groups=_split_set(get("ALLOWED_GROUPS")),
            allowed_users=_split_set(get("ALLOWED_USERS")),
            allowed_repos=_split_set(get("ALLOWED_REPOS")),
            work_dir=Path(get("WORK_DIR", str(Path(tempfile.gettempdir()) / "mr-review"))),
            state_path=Path(get("STATE_PATH", ".mr-reviewer-state.json")),
            opencode_command=get("OPENCODE_COMMAND", "opencode"),
            max_files=int(get("MAX_FILES", "50")),
            max_diff_lines=int(get("MAX_DIFF_LINES", "2000")),
            task_timeout_seconds=int(get("TASK_TIMEOUT_SECONDS", "900")),
            poll_interval_seconds=int(get("POLL_INTERVAL_SECONDS", "15")),
            test_gitlab_responses=Path(test_gitlab_responses) if test_gitlab_responses else None,
        )
