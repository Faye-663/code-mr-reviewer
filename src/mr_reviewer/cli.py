from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from mr_reviewer.config import Config
from mr_reviewer.git import GitClient
from mr_reviewer.gitlab import GitLabClient, GitLabMrUrl, parse_gitlab_mr_url
from mr_reviewer.im import (
    build_welink_reply_args,
    parse_poll_output,
    should_trigger_review,
)
from mr_reviewer.opencode import OpenCodeRunner
from mr_reviewer.process import format_command, prepare_command, split_command
from mr_reviewer.reviewer import ReviewService
from mr_reviewer.state import StateStore

LOG = logging.getLogger("mr_reviewer")


def command_for_log(args: list[str]) -> str:
    safe_args = []
    skip_text = False
    for arg in args:
        if skip_text:
            safe_args.append(f"<text_chars={len(arg)}>")
            skip_text = False
            continue
        safe_args.append(arg)
        if arg == "--text":
            skip_text = True
    return format_command(prepare_command(safe_args))


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
                report = service.review(request.mr, config, task_id)
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
    if not config.im_poll_command:
        raise ValueError("IM poll command is required")
    group_id = _require_welink_group_id(config)
    args = split_command(config.im_poll_command) + ["--group-id", group_id]
    LOG.info("stage=im_poll command=%s", command_for_log(args))
    result = subprocess.run(
        prepare_command(args),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"IM poll command failed: {result.stderr.strip()}")
    return parse_poll_output(result.stdout)


def _reply(config: Config, markdown: str, mr: GitLabMrUrl) -> None:
    if not config.im_reply_command:
        raise ValueError("IM reply command is required")
    group_id = _require_welink_group_id(config)

    project_name = mr.project_path.split("/")[-1]
    random_suffix = uuid.uuid4().hex[:6]
    prefix = f"review-{project_name}-mr-{mr.mr_iid}-{random_suffix}"
    file_path = None
    try:
        # WeLink 群消息不适合承载完整 Markdown，先落临时文件再上传 OneBox。
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8", prefix=prefix) as f:
            f.write(markdown)
            file_path = f.name

        file_name = os.path.basename(file_path)
        upload_error = _upload_report(config, file_path, markdown)

        # 群里只发送文件名通知，避免日志和 IM 文本中出现完整 review 正文。
        if upload_error:
            notify_text = (
                "代码审查报告已生成，但 OneBox 上传失败，请检查 space-id/parent 是否存在或账号是否有权限。"
                f"错误: {upload_error}"
            )
        else:
            notify_text = f"代码审查报告已上传到 WeLink OneBox，群空间Review目录下: {file_name}"
        LOG.info("stage=im_send group_id=%s text=%s", group_id, notify_text)
        reply_args = split_command(config.im_reply_command) + ["--group-id", group_id, "--text", notify_text]
        reply_result = subprocess.run(
            prepare_command(reply_args),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        LOG.info(
            "stage=im_send_result returncode=%s stdout=%s stderr=%s",
            reply_result.returncode,
            reply_result.stdout.strip(),
            reply_result.stderr.strip(),
        )
        if reply_result.returncode != 0:
            raise RuntimeError(f"IM reply command failed: {reply_result.stderr.strip()}")
    finally:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
            LOG.info("stage=file_cleanup path=%s", file_path)


def _upload_report(config: Config, file_path: str, markdown: str) -> str | None:
    LOG.info("stage=file_upload path=%s chars=%s", file_path, len(markdown))
    if not config.welink_onebox_space_id or not config.welink_onebox_parent_id:
        message = "missing OneBox space-id/parent config"
        LOG.warning("stage=file_upload_result returncode=skipped error=%s", message)
        return message

    upload_args = [
        "welink-cli",
        "onebox",
        "file-upload",
        "--space-id",
        config.welink_onebox_space_id,
        "--parent",
        config.welink_onebox_parent_id,
        file_path,
    ]
    LOG.info("stage=file_upload command=%s", command_for_log(upload_args))
    upload_result = subprocess.run(
        prepare_command(upload_args),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    LOG.info(
        "stage=file_upload_result returncode=%s stdout=%s stderr=%s",
        upload_result.returncode,
        upload_result.stdout.strip(),
        upload_result.stderr.strip(),
    )
    if upload_result.returncode != 0:
        error = upload_result.stderr.strip() or upload_result.stdout.strip() or f"returncode={upload_result.returncode}"
        LOG.warning("stage=file_upload_failed error=%s", error)
        return error
    return None


def _require_welink_group_id(config: Config) -> str:
    if not config.welink_group_id:
        raise ValueError("WeLink group ID is required")
    return config.welink_group_id


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
