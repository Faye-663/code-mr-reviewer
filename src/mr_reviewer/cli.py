from __future__ import annotations

import argparse
import logging
import shutil
import sys
import time
import uuid

from mr_reviewer.config import Config
from mr_reviewer.git import GitClient
from mr_reviewer.gitlab import GitLabClient, GitLabMrUrl, parse_gitlab_mr_url
from mr_reviewer.im import should_trigger_review
from mr_reviewer.markdown_report import render_structured_output_as_markdown
from mr_reviewer.opencode import OpenCodeRunner
from mr_reviewer.process import split_command
from mr_reviewer.reviewer import ReviewService
from mr_reviewer.state import StateStore
from mr_reviewer.webhook import run_webhook_server
from mr_reviewer.welink import poll_messages, reply

LOG = logging.getLogger("mr_reviewer")


def build_service(config: Config) -> ReviewService:
    return ReviewService(
        GitLabClient(
            config.gitlab_base_url, config.gitlab_token, config.test_gitlab_responses
        ),
        GitClient(),
        OpenCodeRunner(
            config.opencode_command,
            debug=config.opencode_debug,
            diagnostic_dir=config.opencode_diagnostic_dir,
            prompt_transport=config.opencode_prompt_transport,
        ),
    )


def healthcheck(config: Config) -> int:
    checks = {
        "git": shutil.which("git") is not None,
        "opencode": shutil.which(split_command(config.opencode_command)[0]) is not None,
        "gitlab_base_url": bool(config.gitlab_base_url),
        "gitlab_token": bool(config.gitlab_token),
        "im_poll_command": bool(config.im_poll_command),
        "im_reply_command": bool(config.im_reply_command),
        "welink_group_id": bool(config.welink_group_id),
        "welink_onebox_space_id": bool(config.welink_onebox_space_id),
        "welink_onebox_parent_id": bool(config.welink_onebox_parent_id),
    }
    for name, ok in checks.items():
        print(f"{name}: {'ok' if ok else 'missing'}")
    print(f"webhook_endpoint: {config.webhook_host}:{config.webhook_port}{config.webhook_path}")
    print(f"webhook_secret: {'ok' if config.webhook_secret else 'optional'}")
    print(f"webhook_post_comment: {'enabled' if config.webhook_post_comment else 'disabled'}")
    return 0 if all(checks.values()) else 1


def run_once(config: Config, mr_url: str) -> int:
    service = build_service(config)
    mr = parse_gitlab_mr_url(mr_url, config.gitlab_base_url)
    report = render_structured_output_as_markdown(
        service.review(mr, config, task_id=f"manual-{uuid.uuid4().hex[:8]}")
    )
    print(report.markdown)
    return 0


def poll(config: Config, once: bool) -> int:
    state = StateStore(config.state_path)
    service = build_service(config)
    LOG.info(
        "poller status=started once=%s interval_seconds=%s state_path=%s",
        once,
        config.poll_interval_seconds,
        config.state_path,
    )

    while True:
        messages = _poll_messages(config)
        LOG.info("poller status=messages_received count=%s", len(messages))
        for message in messages:
            if state.is_processed(message.message_id):
                LOG.info(
                    "message=%s status=skipped reason=already_processed",
                    message.message_id,
                )
                continue

            request = should_trigger_review(message, config)
            if request is None:
                LOG.info(
                    "message=%s status=skipped reason=not_review_request",
                    message.message_id,
                )
                continue

            task_id = f"mr-{uuid.uuid4().hex[:12]}"
            start = time.monotonic()
            try:
                LOG.info(
                    "task=%s mr=%s/%s status=started",
                    task_id,
                    request.mr.project_path,
                    request.mr.mr_iid,
                )
                report = render_structured_output_as_markdown(
                    service.review(request.mr, config, task_id)
                )
                LOG.info(
                    "task=%s stage=report_content markdown=%s", task_id, report.markdown
                )
                LOG.info(
                    "task=%s stage=im_reply group_id=%s report_chars=%s",
                    task_id,
                    message.chat_id,
                    len(report.markdown),
                )
                _reply(config, report.markdown, request.mr)
                elapsed = time.monotonic() - start
                state.mark_processed(message.message_id, task_id, "success")
                LOG.info(
                    "task=%s mr=%s/%s elapsed=%.2fs status=success",
                    task_id,
                    request.mr.project_path,
                    request.mr.mr_iid,
                    elapsed,
                )
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
    return poll_messages(config)


def _reply(config: Config, markdown: str, mr: GitLabMrUrl) -> None:
    reply(config, markdown, mr)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    parser = argparse.ArgumentParser(prog="mr-reviewer")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("healthcheck")

    run_once_parser = subparsers.add_parser("run-once")
    run_once_parser.add_argument("mr_url")

    poll_parser = subparsers.add_parser("poll")
    poll_parser.add_argument("--once", action="store_true")

    subparsers.add_parser("webhook")

    args = parser.parse_args(argv)
    config = Config.from_env()

    if args.command == "healthcheck":
        return healthcheck(config)
    if args.command == "run-once":
        return run_once(config, args.mr_url)
    if args.command == "poll":
        return poll(config, args.once)
    if args.command == "webhook":
        return run_webhook_server(config, build_service(config))

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
