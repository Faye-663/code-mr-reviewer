from __future__ import annotations

import argparse
import logging
import os
import shlex
import shutil
import subprocess
import sys
import time
import uuid

from mr_reviewer.config import Config
from mr_reviewer.git import GitClient
from mr_reviewer.gitlab import GitLabClient, parse_gitlab_mr_url
from mr_reviewer.im import build_welink_reply_args, parse_poll_output, should_trigger_review
from mr_reviewer.opencode import OpenCodeRunner
from mr_reviewer.reviewer import ReviewService
from mr_reviewer.state import StateStore


LOG = logging.getLogger("mr_reviewer")


def split_command(command: str) -> list[str]:
    # Windows 路径包含反斜杠，不能使用 POSIX 模式拆分命令。
    return shlex.split(command, posix=(os.name != "nt"))


def build_service(config: Config) -> ReviewService:
    return ReviewService(
        GitLabClient(config.gitlab_base_url, config.gitlab_token, config.test_gitlab_responses),
        GitClient(),
        OpenCodeRunner(config.opencode_command),
    )


def healthcheck(config: Config) -> int:
    checks = {
        "git": shutil.which("git") is not None,
        "opencode": shutil.which(split_command(config.opencode_command)[0]) is not None,
        "gitlab_base_url": bool(config.gitlab_base_url),
        "gitlab_token": bool(config.gitlab_token),
        "im_poll_command": bool(config.im_poll_command),
        "im_reply_command": bool(config.im_reply_command),
    }
    for name, ok in checks.items():
        print(f"{name}: {'ok' if ok else 'missing'}")
    return 0 if all(checks.values()) else 1


def run_once(config: Config, mr_url: str) -> int:
    service = build_service(config)
    mr = parse_gitlab_mr_url(mr_url, config.gitlab_base_url)
    report = service.review(mr, config, task_id=f"manual-{uuid.uuid4().hex[:8]}")
    print(report.markdown)
    return 0


def poll(config: Config, once: bool) -> int:
    state = StateStore(config.state_path)
    service = build_service(config)
    LOG.info("poller status=started once=%s interval_seconds=%s state_path=%s", once, config.poll_interval_seconds, config.state_path)

    while True:
        messages = _poll_messages(config)
        LOG.info("poller status=messages_received count=%s", len(messages))
        for message in messages:
            if state.is_processed(message.message_id):
                LOG.info("message=%s status=skipped reason=already_processed", message.message_id)
                continue

            request = should_trigger_review(message, config)
            if request is None:
                LOG.info("message=%s status=skipped reason=not_review_request", message.message_id)
                continue

            task_id = f"mr-{uuid.uuid4().hex[:12]}"
            start = time.monotonic()
            try:
                LOG.info("task=%s mr=%s/%s status=started", task_id, request.mr.project_path, request.mr.mr_iid)
                report = service.review(request.mr, config, task_id)
                LOG.info("task=%s stage=im_reply group_id=%s report_chars=%s", task_id, message.chat_id, len(report.markdown))
                _reply(config, message.chat_id, report.markdown)
                elapsed = time.monotonic() - start
                state.mark_processed(message.message_id, task_id, "success")
                LOG.info("task=%s mr=%s/%s elapsed=%.2fs status=success", task_id, request.mr.project_path, request.mr.mr_iid, elapsed)
            except Exception as exc:  # noqa: BLE001 - 顶层任务必须记录失败并继续轮询。
                elapsed = time.monotonic() - start
                state.mark_processed(message.message_id, task_id, "failed", str(exc))
                LOG.error(
                    "task=%s mr=%s/%s elapsed=%.2fs status=failed error=%s",
                    task_id,
                    request.mr.project_path,
                    request.mr.mr_iid,
                    elapsed,
                    exc,
                )

        if once:
            return 0
        time.sleep(config.poll_interval_seconds)


def _poll_messages(config: Config):
    if not config.im_poll_command:
        raise ValueError("IM poll command is required")
    LOG.info("stage=im_poll command=%s", split_command(config.im_poll_command)[0])
    result = subprocess.run(split_command(config.im_poll_command), text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"IM poll command failed: {result.stderr.strip()}")
    return parse_poll_output(result.stdout)


def _reply(config: Config, group_id: str, markdown: str) -> None:
    if not config.im_reply_command:
        raise ValueError("IM reply command is required")
    LOG.info("stage=im_send command=%s group_id=%s text_chars=%s", split_command(config.im_reply_command)[0], group_id, len(markdown))
    result = subprocess.run(
        build_welink_reply_args(config.im_reply_command, group_id, markdown),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"IM reply command failed: {result.stderr.strip()}")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="mr-reviewer")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("healthcheck")

    run_once_parser = subparsers.add_parser("run-once")
    run_once_parser.add_argument("mr_url")

    poll_parser = subparsers.add_parser("poll")
    poll_parser.add_argument("--once", action="store_true")

    args = parser.parse_args(argv)
    config = Config.from_env()

    if args.command == "healthcheck":
        return healthcheck(config)
    if args.command == "run-once":
        return run_once(config, args.mr_url)
    if args.command == "poll":
        return poll(config, args.once)

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
