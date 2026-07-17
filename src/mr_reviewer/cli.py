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
from mr_reviewer.im import ReviewRequest, ReviewSetRejection, ReviewSetRequest, resolve_review_trigger
from mr_reviewer.markdown_report import render_structured_output_as_markdown
from mr_reviewer.observability import configure_logging, task_context
from mr_reviewer.opencode import build_agent_runner
from mr_reviewer.process import split_command
from mr_reviewer.review_set import ReviewSetValidationError
from mr_reviewer.review_set_publish import ReviewSetPublisher
from mr_reviewer.review_set_report import render_review_set_report
from mr_reviewer.reviewer import ReviewService
from mr_reviewer.state import StateStore
from mr_reviewer.webhook import run_webhook_server
from mr_reviewer.welink import poll_messages, reply, reply_review_set, send_text

LOG = logging.getLogger("mr_reviewer")


def build_service(config: Config) -> ReviewService:
    return ReviewService(
        GitLabClient(
            config.gitlab_api_base_url, config.gitlab_token, config.test_gitlab_responses
        ),
        GitClient(),
        build_agent_runner(
            config.agent_type,
            config.agent_command or config.opencode_command,
            debug=config.agent_debug,
            diagnostic_dir=config.agent_diagnostic_dir,
            redaction_token=config.gitlab_token,
        ),
    )


def healthcheck(config: Config) -> int:
    checks = {
        "git": shutil.which("git") is not None,
        "agent": shutil.which(split_command(config.agent_command or config.opencode_command)[0]) is not None,
        "gitlab_base_url": bool(config.gitlab_base_url),
        "gitlab_api_base_url": bool(config.gitlab_api_base_url),
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
    print(f"review_set_post_comment: {'enabled' if config.review_set_post_comment else 'disabled'}")
    print(f"publish_min_severity: {config.publish_min_severity}")
    print(f"publish_min_confidence: {config.publish_min_confidence}")
    return 0 if all(checks.values()) else 1


def run_once(config: Config, mr_url: str) -> int:
    service = build_service(config)
    mr = parse_gitlab_mr_url(mr_url, config.gitlab_base_url)
    task_id = f"manual-{uuid.uuid4().hex[:8]}"
    with task_context(task_id, config.debug_dir, config.log_level == "DEBUG"):
        report = render_structured_output_as_markdown(service.review(mr, config, task_id=task_id))
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

            request = resolve_review_trigger(message, config)
            if request is None:
                LOG.info(
                    "message=%s status=skipped reason=not_review_request",
                    message.message_id,
                )
                continue
            if isinstance(request, ReviewSetRejection):
                _reject_review_set(config, state, request)
            elif isinstance(request, ReviewSetRequest):
                _process_review_set(config, state, service, request)
            else:
                _process_single_review(config, state, service, request)

        if once:
            return 0
        time.sleep(config.poll_interval_seconds)


def _poll_messages(config: Config):
    return poll_messages(config)


def _reply(config: Config, markdown: str, mr: GitLabMrUrl) -> None:
    reply(config, markdown, mr)


def _reply_review_set(
        config: Config,
        markdown: str,
        review_set_id: str,
        publish_counts: dict[str, int],
) -> None:
    reply_review_set(config, markdown, review_set_id, publish_counts)


def _send_text(config: Config, text: str) -> None:
    send_text(config, text)


def _process_single_review(
        config: Config,
        state: StateStore,
        service: ReviewService,
        request: ReviewRequest,
) -> None:
    task_id = f"mr-{uuid.uuid4().hex[:12]}"
    start = time.monotonic()
    try:
        LOG.info("task=%s mr=%s/%s status=started", task_id, request.mr.project_path, request.mr.mr_iid)
        with task_context(task_id, config.debug_dir, config.log_level == "DEBUG"):
            report = render_structured_output_as_markdown(service.review(request.mr, config, task_id))
            LOG.info(
                "task=%s stage=im_reply group_id=%s report_chars=%s",
                task_id,
                request.message.chat_id,
                len(report.markdown),
            )
            _reply(config, report.markdown, request.mr)
        elapsed = time.monotonic() - start
        state.mark_processed(request.message.message_id, task_id, "success")
        LOG.info(
            "task=%s mr=%s/%s elapsed=%.2fs status=success",
            task_id,
            request.mr.project_path,
            request.mr.mr_iid,
            elapsed,
        )
    except Exception as exc:  # noqa: BLE001 - 保留现有单 MR 顶层失败语义。
        elapsed = time.monotonic() - start
        state.mark_processed(request.message.message_id, task_id, "failed", str(exc))
        LOG.error(
            "task=%s mr=%s/%s elapsed=%.2fs status=failed error=%s",
            task_id,
            request.mr.project_path,
            request.mr.mr_iid,
            elapsed,
            exc,
        )


def _reject_review_set(config: Config, state: StateStore, rejection: ReviewSetRejection) -> None:
    task_id = f"review-set-{uuid.uuid4().hex[:12]}"
    state.mark_processed(rejection.message.message_id, task_id, "rejected", rejection.reason_code)
    _safe_send_text(config, _rejection_text(rejection.reason_code), task_id)
    LOG.info("task=%s review_scope=review-set status=rejected reason=%s", task_id, rejection.reason_code)


def _process_review_set(
        config: Config,
        state: StateStore,
        service: ReviewService,
        request: ReviewSetRequest,
) -> None:
    task_id = f"review-set-{uuid.uuid4().hex[:12]}"
    start = time.monotonic()
    try:
        with task_context(task_id, config.debug_dir, config.log_level == "DEBUG"):
            report = service.review_set(request, config, task_id)
            publication = ReviewSetPublisher(
                service.gitlab,
                config.publication_policy,
            ).publish(
                report,
                enabled=config.review_set_post_comment,
                model_name=config.agent_model_name,
            )
            markdown = render_review_set_report(report, publication)
            _reply_review_set(
                config,
                markdown,
                report.manifest.review_set_id,
                publication.counts,
            )
        state.mark_processed(request.message.message_id, task_id, publication.status)
        LOG.info(
            "task=%s review_scope=review-set review_set_id=%s req_id=%s elapsed=%.2fs status=%s",
            task_id,
            report.manifest.review_set_id,
            report.manifest.req_id,
            time.monotonic() - start,
            publication.status,
        )
    except ReviewSetValidationError as exc:
        if exc.reason_code in {"req_id_missing", "req_id_mismatch"}:
            state.mark_processed(request.message.message_id, task_id, "rejected", exc.reason_code)
            _safe_send_text(config, _rejection_text(exc.reason_code), task_id)
            LOG.info(
                "task=%s review_scope=review-set elapsed=%.2fs status=rejected reason=%s",
                task_id,
                time.monotonic() - start,
                exc.reason_code,
            )
        else:
            _fail_review_set(config, state, request, task_id, start, exc)
    except Exception as exc:  # noqa: BLE001 - 联合失败必须终结消息且只向 IM 暴露安全文案。
        _fail_review_set(config, state, request, task_id, start, exc)


def _fail_review_set(
        config: Config,
        state: StateStore,
        request: ReviewSetRequest,
        task_id: str,
        start: float,
        error: Exception,
) -> None:
    state.mark_processed(request.message.message_id, task_id, "failed", "review_set_failed")
    _safe_send_text(config, f"多 MR 联合检视执行失败，请使用任务号 {task_id} 查询日志。", task_id)
    LOG.exception(
        "task=%s review_scope=review-set elapsed=%.2fs status=failed error_type=%s",
        task_id,
        time.monotonic() - start,
        type(error).__name__,
        exc_info=True,
    )


def _safe_send_text(config: Config, text: str, task_id: str) -> None:
    try:
        _send_text(config, text)
    except Exception:  # noqa: BLE001 - 通知失败不能使终结状态重新进入轮询。
        LOG.exception("task=%s review_scope=review-set stage=im_notify status=failed", task_id)


def _rejection_text(reason_code: str) -> str:
    messages = {
        "too_many_mrs": "多 MR 联合检视已拒绝：一条消息最多只能包含 3 个唯一 MR。",
        "same_project": "多 MR 联合检视已拒绝：成员必须来自不同项目。",
        "repo_not_allowed": "多 MR 联合检视已拒绝：消息中包含未授权仓库。",
        "req_id_missing": "多 MR 联合检视已拒绝：至少一个 MR 缺少有效 ReqID。",
        "req_id_mismatch": "多 MR 联合检视已拒绝：成员 MR 的 ReqID 不一致。",
    }
    return messages.get(reason_code, "多 MR 联合检视已拒绝：请求不满足联合检视条件。")


def main(argv: list[str] | None = None) -> int:
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
    configure_logging(config.log_level)

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
