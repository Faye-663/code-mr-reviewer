from __future__ import annotations

import argparse
import logging
import queue
import shutil
import sys
import threading
import time
import uuid
from dataclasses import dataclass

from mr_reviewer.config import Config
from mr_reviewer.git import GitClient
from mr_reviewer.gitlab import GitLabClient, GitLabMrUrl, parse_gitlab_mr_url
from mr_reviewer.im import ReviewRequest, ReviewSetRejection, ReviewSetRequest, resolve_review_trigger
from mr_reviewer.im_notifications import (
    render_review_set_accepted,
    render_review_set_failed,
    render_review_set_queue_full,
    render_review_set_rejected,
    render_review_set_terminal,
    render_single_accepted,
    render_single_failed,
    render_single_queue_full,
    render_single_terminal,
)
from mr_reviewer.markdown_report import render_structured_output_as_markdown
from mr_reviewer.observability import configure_logging, task_context
from mr_reviewer.opencode import build_agent_runner
from mr_reviewer.process import split_command
from mr_reviewer.review_set import ReviewSetValidationError
from mr_reviewer.review_set_publish import ReviewSetPublisher
from mr_reviewer.review_set_report import render_review_set_report
from mr_reviewer.reviewer import ReviewService
from mr_reviewer.single_mr import SingleMrReviewCoordinator, SingleMrTrigger
from mr_reviewer.repository_dependencies import (
    RepositoryDependencyCatalogError,
    load_repository_dependency_catalog,
)
from mr_reviewer.state import StateStore
from mr_reviewer.webhook import run_webhook_server
from mr_reviewer.welink import poll_messages, reply, reply_review_set, send_text, upload_onebox_report

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
    catalog_ok = True
    if config.repository_dependency_catalog is None:
        print("repository_dependency_catalog: optional")
    else:
        try:
            catalog = load_repository_dependency_catalog(config.repository_dependency_catalog)
        except RepositoryDependencyCatalogError as exc:
            catalog_ok = False
            print(
                "repository_dependency_catalog: "
                f"invalid ({exc.reason_code}) path={config.repository_dependency_catalog}"
            )
        else:
            print(
                "repository_dependency_catalog: "
                f"ok (repositories={len(catalog.repositories)}) path={config.repository_dependency_catalog}"
            )
    print(f"webhook_endpoint: {config.webhook_host}:{config.webhook_port}{config.webhook_path}")
    print(f"webhook_secret: {'ok' if config.webhook_secret else 'optional'}")
    print(f"im_post_comment: {'enabled' if config.im_post_comment else 'disabled'}")
    print(f"im_upload_onebox: {'enabled' if config.im_upload_onebox else 'disabled'}")
    print(f"webhook_post_comment: {'enabled' if config.webhook_post_comment else 'disabled'}")
    print(f"webhook_upload_onebox: {'enabled' if config.webhook_upload_onebox else 'disabled'}")
    print(f"coordination_db_path: {config.coordination_db_path}")
    allowlist_warning = _im_publish_allowlist_warning(config)
    if allowlist_warning:
        print(f"im_publish_allowlist: WARNING ({allowlist_warning})")
    else:
        print("im_publish_allowlist: restricted")
    print(f"review_set_post_comment: {'enabled' if config.review_set_post_comment else 'disabled'}")
    print(f"publish_min_severity: {config.publish_min_severity}")
    print(f"publish_min_confidence: {config.publish_min_confidence}")
    print(f"im_max_pending_reviews: {config.im_max_pending_reviews}")
    return 0 if all(checks.values()) and catalog_ok else 1


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
    worker = ImReviewWorker(config, state, service)
    allowlist_warning = _im_publish_allowlist_warning(config)
    if allowlist_warning:
        print(
            "WARNING stage=poller_startup outcome=warning "
            f"reason=im_gitlab_publish_unrestricted {allowlist_warning}",
            file=sys.stderr,
        )
    LOG.info(
        "poller status=started once=%s interval_seconds=%s state_path=%s",
        once,
        config.poll_interval_seconds,
        config.state_path,
    )

    worker.start()
    try:
        while True:
            worker.raise_if_failed()
            messages = _poll_messages(config)
            LOG.info("poller status=messages_received count=%s", len(messages))
            for message in messages:
                if state.is_processed(message.message_id):
                    LOG.info(
                        "message=%s status=skipped reason=already_processed",
                        message.message_id,
                    )
                    continue
                inflight = worker.inflight_details(message.message_id)
                if inflight is not None:
                    task_id, review_scope, ahead, queue_depth = inflight
                    LOG.info(
                        "message=%s task=%s review_scope=%s stage=im_queue event=skipped "
                        "queue_depth=%s ahead=%s outcome=already_inflight",
                        message.message_id,
                        task_id,
                        review_scope,
                        queue_depth,
                        ahead,
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
                    _reject_review_set(config, state, request, worker.notification_lock)
                else:
                    worker.submit(request)

            if once:
                worker.wait()
                worker.raise_if_failed()
                return 0
            time.sleep(config.poll_interval_seconds)
    finally:
        # 已发送受理通知的任务必须先形成终态，不能因 poll 查询退出而被静默丢弃。
        worker.close()


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


def _upload_onebox_report(config: Config, file_name: str, markdown: str) -> str | None:
    return upload_onebox_report(config, file_name, markdown)


def _im_publish_allowlist_warning(config: Config) -> str:
    if not config.im_post_comment:
        return ""
    users = "restricted" if config.allowed_users else "unrestricted"
    repos = "restricted" if config.allowed_repos else "unrestricted"
    if users == "restricted" and repos == "restricted":
        return ""
    return f"allowed_users={users} allowed_repos={repos}"


def _process_single_review(
        config: Config,
        state: StateStore,
        service: ReviewService,
        coordinator: SingleMrReviewCoordinator,
        request: ReviewRequest,
        task_id: str,
        accepted_status: str,
        notification_lock: threading.Lock,
) -> None:
    start = time.monotonic()
    try:
        LOG.info("task=%s mr=%s/%s status=started", task_id, request.mr.project_path, request.mr.mr_iid)
        with task_context(task_id, config.debug_dir, config.log_level == "DEBUG"):
            target = service.resolve_target(request.mr)
            outcome = coordinator.handle(
                target,
                SingleMrTrigger(
                    source="im",
                    trigger_id=request.message.message_id,
                    post_comment=config.im_post_comment,
                    upload_onebox=config.im_upload_onebox,
                ),
            )
    except Exception as exc:  # noqa: BLE001 - 保留现有单 MR 顶层失败语义。
        elapsed = time.monotonic() - start
        terminal_status = _safe_im_notify(
            config,
            render_single_failed(request.mr, task_id),
            task_id,
            "single",
            "terminal",
            notification_lock,
        )
        state.mark_processed(
            request.message.message_id,
            task_id,
            "failed",
            "single_review_failed",
            {"accepted": accepted_status, "terminal": terminal_status},
        )
        LOG.error(
            "task=%s mr=%s/%s elapsed=%.2fs status=failed error_type=%s",
            task_id,
            request.mr.project_path,
            request.mr.mr_iid,
            elapsed,
            type(exc).__name__,
        )
    else:
        terminal_status = _safe_im_notify(
            config,
            render_single_terminal(request.mr, outcome),
            outcome.review_run_id,
            "single",
            "terminal",
            notification_lock,
        )
        elapsed = time.monotonic() - start
        state.mark_processed(
            request.message.message_id,
            outcome.review_run_id,
            outcome.status,
            notifications={"accepted": accepted_status, "terminal": terminal_status},
        )
        LOG.info(
            "task=%s mr=%s/%s elapsed=%.2fs status=%s",
            outcome.review_run_id,
            request.mr.project_path,
            request.mr.mr_iid,
            elapsed,
            outcome.status,
        )


def _reject_review_set(
        config: Config,
        state: StateStore,
        rejection: ReviewSetRejection,
        notification_lock: threading.Lock,
) -> None:
    task_id = f"review-set-{uuid.uuid4().hex[:12]}"
    terminal_status = _safe_im_notify(
        config,
        render_review_set_rejected(
            rejection.members,
            _rejection_text(rejection.reason_code),
            task_id,
        ),
        task_id,
        "review-set",
        "terminal",
        notification_lock,
    )
    state.mark_processed(
        rejection.message.message_id,
        task_id,
        "rejected",
        rejection.reason_code,
        {"accepted": "not_applicable", "terminal": terminal_status},
    )
    LOG.info("task=%s review_scope=review-set status=rejected reason=%s", task_id, rejection.reason_code)


def _process_review_set(
        config: Config,
        state: StateStore,
        service: ReviewService,
        request: ReviewSetRequest,
        task_id: str,
        accepted_status: str,
        notification_lock: threading.Lock,
) -> None:
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
            file_name = f"review-set-{report.manifest.review_set_id[:12]}.md"
            try:
                upload_error = _upload_onebox_report(config, file_name, markdown)
            except Exception as exc:  # noqa: BLE001 - 报告上传失败不能改写已完成的联合检视。
                upload_error = "onebox_upload_failed"
                LOG.warning(
                    "task=%s review_scope=review-set stage=file_upload outcome=failed error_type=%s",
                    task_id,
                    type(exc).__name__,
                )
            final_status = (
                "success_with_warnings"
                if publication.status == "success_with_warnings" or upload_error
                else publication.status
            )
    except ReviewSetValidationError as exc:
        if exc.reason_code in {"req_id_missing", "req_id_mismatch"}:
            terminal_status = _safe_im_notify(
                config,
                render_review_set_rejected(
                    request.members,
                    _rejection_text(exc.reason_code),
                    task_id,
                ),
                task_id,
                "review-set",
                "terminal",
                notification_lock,
            )
            state.mark_processed(
                request.message.message_id,
                task_id,
                "rejected",
                exc.reason_code,
                {"accepted": accepted_status, "terminal": terminal_status},
            )
            LOG.info(
                "task=%s review_scope=review-set elapsed=%.2fs status=rejected reason=%s",
                task_id,
                time.monotonic() - start,
                exc.reason_code,
            )
        else:
            _fail_review_set(
                config, state, request, task_id, start, exc, accepted_status, notification_lock
            )
    except Exception as exc:  # noqa: BLE001 - 联合失败必须终结消息且只向 IM 暴露安全文案。
        _fail_review_set(
            config, state, request, task_id, start, exc, accepted_status, notification_lock
        )
    else:
        terminal_status = _safe_im_notify(
            config,
            render_review_set_terminal(report, publication, file_name, upload_error, task_id),
            task_id,
            "review-set",
            "terminal",
            notification_lock,
        )
        state.mark_processed(
            request.message.message_id,
            task_id,
            final_status,
            notifications={"accepted": accepted_status, "terminal": terminal_status},
        )
        LOG.info(
            "task=%s review_scope=review-set review_set_id=%s req_id=%s elapsed=%.2fs status=%s",
            task_id,
            report.manifest.review_set_id,
            report.manifest.req_id,
            time.monotonic() - start,
            final_status,
        )


def _fail_review_set(
        config: Config,
        state: StateStore,
        request: ReviewSetRequest,
        task_id: str,
        start: float,
        error: Exception,
        accepted_status: str,
        notification_lock: threading.Lock,
) -> None:
    terminal_status = _safe_im_notify(
        config,
        render_review_set_failed(
            request.members,
            task_id,
            str(getattr(error, "stage", "review_set_review")),
        ),
        task_id,
        "review-set",
        "terminal",
        notification_lock,
    )
    state.mark_processed(
        request.message.message_id,
        task_id,
        "failed",
        "review_set_failed",
        {"accepted": accepted_status, "terminal": terminal_status},
    )
    LOG.error(
        "task=%s review_scope=review-set elapsed=%.2fs status=failed error_type=%s",
        task_id,
        time.monotonic() - start,
        type(error).__name__,
    )


@dataclass(frozen=True, slots=True)
class _ImReviewJob:
    request: ReviewRequest | ReviewSetRequest
    task_id: str
    accepted_status: str
    review_scope: str


class ImReviewWorker:
    _STOP = object()

    def __init__(self, config: Config, state: StateStore, service: ReviewService) -> None:
        self.config = config
        self.state = state
        self.service = service
        self.notification_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._state_changed = threading.Condition(self._state_lock)
        self._queue: queue.Queue[_ImReviewJob | object] = queue.Queue(
            maxsize=config.im_max_pending_reviews
        )
        self._inflight: dict[str, tuple[str, str, int]] = {}
        self._pending = 0
        self._active = False
        self._failure: Exception | None = None
        self._single_coordinator: SingleMrReviewCoordinator | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="mr-reviewer-im-worker",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def inflight_details(self, message_id: str) -> tuple[str, str, int, int] | None:
        with self._state_lock:
            details = self._inflight.get(message_id)
            if details is None:
                return None
            task_id, review_scope, ahead = details
            return task_id, review_scope, ahead, self._pending

    def submit(self, request: ReviewRequest | ReviewSetRequest) -> str:
        message_id = request.message.message_id
        review_scope = "review-set" if isinstance(request, ReviewSetRequest) else "single"
        task_id = (
            f"review-set-{uuid.uuid4().hex[:12]}"
            if review_scope == "review-set"
            else f"mr-{uuid.uuid4().hex[:12]}"
        )
        with self._state_changed:
            if self.state.is_processed(message_id):
                return "already_processed"
            # 首个已受理请求在 worker dequeue 前暂计入 pending；等待接管可避免同批消息误判队列已满。
            while not self._active and self._pending > 0 and self._failure is None:
                self._state_changed.wait()
            if self._failure is not None:
                raise RuntimeError("IM review worker failed") from self._failure
            if message_id in self._inflight:
                return "already_inflight"
            if self._pending >= self.config.im_max_pending_reviews:
                full = True
                ahead = self._pending + int(self._active)
            else:
                full = False
                ahead = self._pending + int(self._active)
                self._inflight[message_id] = (task_id, review_scope, ahead)
                self._pending += 1

        if full:
            self._reject_full(request, task_id, review_scope, ahead)
            return "rejected"

        accepted_text = (
            render_review_set_accepted(request.members, ahead)
            if isinstance(request, ReviewSetRequest)
            else render_single_accepted(request.mr, ahead)
        )
        accepted_status = _safe_im_notify(
            self.config,
            accepted_text,
            task_id,
            review_scope,
            "accepted",
            self.notification_lock,
        )
        job = _ImReviewJob(request, task_id, accepted_status, review_scope)
        with self._state_lock:
            self._queue.put_nowait(job)
            LOG.info(
                "message=%s task=%s review_scope=%s stage=im_queue event=enqueued "
                "queue_depth=%s ahead=%s outcome=succeeded",
                message_id,
                task_id,
                review_scope,
                self._pending,
                ahead,
            )
        return "enqueued"

    def wait(self) -> None:
        self._queue.join()

    def close(self) -> None:
        self._queue.join()
        self._queue.put(self._STOP)
        self._thread.join()

    def raise_if_failed(self) -> None:
        with self._state_lock:
            failure = self._failure
        if failure is not None:
            raise RuntimeError("IM review worker failed") from failure

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is self._STOP:
                self._queue.task_done()
                return
            job = item
            message_id = job.request.message.message_id
            with self._state_changed:
                self._pending -= 1
                self._active = True
                queue_depth = self._pending
                self._state_changed.notify_all()
            LOG.info(
                "message=%s task=%s review_scope=%s stage=im_queue event=started "
                "queue_depth=%s ahead=0 outcome=started",
                message_id,
                job.task_id,
                job.review_scope,
                queue_depth,
            )
            try:
                self._process(job)
            except Exception as exc:  # noqa: BLE001 - 基础设施异常必须通知 poll 并继续 drain 已受理任务。
                with self._state_changed:
                    if self._failure is None:
                        self._failure = exc
                    self._state_changed.notify_all()
                LOG.error(
                    "message=%s task=%s review_scope=%s stage=im_queue event=completed "
                    "queue_depth=%s ahead=0 outcome=failed error_type=%s",
                    message_id,
                    job.task_id,
                    job.review_scope,
                    self._queue_depth(),
                    type(exc).__name__,
                )
            finally:
                processed = self.state.is_processed(message_id)
                with self._state_changed:
                    self._active = False
                    if processed:
                        self._inflight.pop(message_id, None)
                    self._state_changed.notify_all()
                if processed:
                    LOG.info(
                        "message=%s task=%s review_scope=%s stage=im_queue event=completed "
                        "queue_depth=%s ahead=0 outcome=succeeded",
                        message_id,
                        job.task_id,
                        job.review_scope,
                        self._queue_depth(),
                    )
                self._queue.task_done()

    def _process(self, job: _ImReviewJob) -> None:
        if isinstance(job.request, ReviewSetRequest):
            _process_review_set(
                self.config,
                self.state,
                self.service,
                job.request,
                job.task_id,
                job.accepted_status,
                self.notification_lock,
            )
            return
        if self._single_coordinator is None:
            self._single_coordinator = SingleMrReviewCoordinator(
                self.service,
                self.service.gitlab,
                self.config,
            )
        _process_single_review(
            self.config,
            self.state,
            self.service,
            self._single_coordinator,
            job.request,
            job.task_id,
            job.accepted_status,
            self.notification_lock,
        )

    def _reject_full(
        self,
        request: ReviewRequest | ReviewSetRequest,
        task_id: str,
        review_scope: str,
        ahead: int,
    ) -> None:
        text = (
            render_review_set_queue_full(request.members, self.config.im_max_pending_reviews)
            if isinstance(request, ReviewSetRequest)
            else render_single_queue_full(request.mr, self.config.im_max_pending_reviews)
        )
        terminal_status = _safe_im_notify(
            self.config,
            text,
            task_id,
            review_scope,
            "terminal",
            self.notification_lock,
        )
        self.state.mark_processed(
            request.message.message_id,
            task_id,
            "rejected",
            "queue_full",
            {"accepted": "not_applicable", "terminal": terminal_status},
        )
        LOG.info(
            "message=%s task=%s review_scope=%s stage=im_queue event=rejected "
            "queue_depth=%s ahead=%s outcome=queue_full",
            request.message.message_id,
            task_id,
            review_scope,
            self._queue_depth(),
            ahead,
        )

    def _queue_depth(self) -> int:
        with self._state_lock:
            return self._pending


def _safe_im_notify(
        config: Config,
        text: str,
        task_id: str,
        review_scope: str,
        event: str,
        notification_lock: threading.Lock | None = None,
) -> str:
    try:
        if notification_lock is None:
            _send_text(config, text)
        else:
            with notification_lock:
                _send_text(config, text)
    except Exception as exc:  # noqa: BLE001 - 通知失败不能改写检视事实或触发 Agent 重试。
        LOG.warning(
            "task=%s review_scope=%s stage=im_notify event=%s outcome=failed error_type=%s",
            task_id,
            review_scope,
            event,
            type(exc).__name__,
        )
        return "failed"
    LOG.info(
        "task=%s review_scope=%s stage=im_notify event=%s outcome=succeeded",
        task_id,
        review_scope,
        event,
    )
    return "succeeded"


def _rejection_text(reason_code: str) -> str:
    messages = {
        "too_many_mrs": "一条消息最多只能包含 3 个唯一 MR。",
        "same_project": "成员必须来自不同项目。",
        "repo_not_allowed": "消息中包含未授权仓库。",
        "req_id_missing": "至少一个 MR 缺少有效 ReqID。",
        "req_id_mismatch": "成员 MR 的 ReqID 不一致。",
    }
    return messages.get(reason_code, "请求不满足联合检视条件。")


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
