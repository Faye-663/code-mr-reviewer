from __future__ import annotations

import logging
import math
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from mr_reviewer.config import Config
from mr_reviewer.dependency_review import (
    DependencyReviewPreparationError,
    DependencyReviewPreparer,
    DependencyReviewPrimary,
    PreparedDependencyReview,
)
from mr_reviewer.dependency_review_result import (
    dependency_review_result_as_single_review_json,
    parse_dependency_review_plan,
    parse_structured_dependency_review_result,
)
from mr_reviewer.git import GitCheckout, GitClient
from mr_reviewer.gitlab import GitLabClient, GitLabMrUrl, choose_diff_refs
from mr_reviewer.observability import task_stage
from mr_reviewer.opencode import AgentRunner
from mr_reviewer.prompting import (
    build_dependency_review_plan_prompt,
    build_dependency_review_prompt,
    build_review_plan_prompt,
    build_review_prompt,
    build_review_set_plan_prompt,
    build_review_set_review_prompt,
)
from mr_reviewer.review_routing import resolve_review_routing
from mr_reviewer.review_result import parse_review_plan
from mr_reviewer.repository_dependencies import (
    DependencyReviewSelection,
    RepositoryDependencyCatalogError,
    degrade_dependency_review,
    load_repository_dependency_catalog,
    select_dependency_review,
)
from mr_reviewer.review_set import PreparedReviewSetMember, ReviewSetManifest, ReviewSetPreparer
from mr_reviewer.review_set_result import (
    StructuredReviewSetResult,
    parse_review_set_plan,
    parse_structured_review_set_result,
)
from mr_reviewer.im import ReviewSetRequest

LOG = logging.getLogger("mr_reviewer")
DEFAULT_REVIEW_SKILL = "code-review"


@dataclass(frozen=True, slots=True)
class ReviewReport:
    markdown: str
    summary: dict[str, object] | None = None
    review_plan: dict[str, object] | None = None
    repo: str = ""
    mr_iid: int | None = None
    mr_url: str = ""
    source_branch: str = ""
    target_branch: str = ""
    base_sha: str = ""
    head_sha: str = ""
    changed_files: list[str] | None = None
    diff: str = ""
    opencode_returncode: int | None = None
    submission_owner: str = "none"
    submission_status: str = "unknown"
    structured_parse_status: str = ""
    finding_counts: dict[str, int] | None = None
    finding_results: list[dict] | None = None
    good: list[str] | None = None
    notes: list[str] | None = None
    test_gaps: list[str] | None = None
    rejected_findings: list[dict[str, object]] | None = None
    normalization_warnings: list[dict[str, str]] | None = None
    prompt_templates: dict[str, dict[str, str]] | None = None
    title: str = ""
    requested_review_mode: str = ""
    review_mode: str = ""
    review_scope: str = "single"
    routing_reason: str = ""
    routing_marker: str = ""
    dependency_context_status: str = "not_applicable"
    dependency_degradation_reason: str = ""
    dependency_failed_project: str = ""
    dependency_context_id: str = ""
    dependency_repositories: list[dict] | None = None
    dependency_preparation_seconds: float | None = None
    dependency_relationship_summary: list[str] | None = None
    agent_call_count: int = 0
    failure_stage: str = ""
    head_validation: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class ReviewSetReviewReport:
    manifest: ReviewSetManifest
    review_plan: dict[str, object]
    result: StructuredReviewSetResult
    members: tuple[PreparedReviewSetMember, ...]
    prompt_templates: dict[str, dict[str, str]]
    agent_call_count: int


class ReviewStageError(RuntimeError):
    def __init__(
            self,
            stage: str,
            cause: Exception,
            review_plan: dict[str, object] | None = None,
            agent_call_count: int = 0,
            report_context: dict[str, object] | None = None,
    ):
        super().__init__(f"{stage} stage failed: {cause}")
        self.stage = stage
        self.review_plan = review_plan
        self.summary = None
        self.agent_call_count = agent_call_count
        self.report_context = report_context or {}


class ReviewCancelledError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MergeRequestReviewTarget:
    base_url: str
    project_path: str
    mr_iid: int
    mr_url: str
    target_repo_url: str
    source_repo_url: str
    target_branch: str
    source_branch: str
    base_sha: str | None
    head_sha: str
    title: str = ""


@dataclass(frozen=True, slots=True)
class _DependencyContext:
    selection: DependencyReviewSelection
    prepared: PreparedDependencyReview | None = None
    failed_project: str = ""
    preparation_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class _ReviewExecution:
    markdown: str
    review_plan: dict[str, object] | None
    relationship_summary: list[str]
    prompt_templates: dict[str, dict[str, str]]
    agent_call_count: int
    structured_parse_status: str = ""
    rejected_findings: list[dict[str, object]] | None = None
    normalization_warnings: list[dict[str, str]] | None = None


class ReviewService:
    def __init__(self, gitlab: GitLabClient, git: GitClient, opencode: AgentRunner):
        self.gitlab = gitlab
        self.git = git
        self.opencode = opencode

    def review(
        self,
        mr: GitLabMrUrl,
        config: Config,
        task_id: str,
        should_cancel: Callable[[], bool] | None = None,
    ) -> ReviewReport:
        LOG.info("task=%s stage=gitlab_fetch repo=%s mr_iid=%s", task_id, mr.project_path, mr.mr_iid)
        target = self.resolve_target(mr)
        return self.review_target(
            target,
            config,
            task_id,
            structured_output=True,
            should_cancel=should_cancel,
        )

    def resolve_target(self, mr: GitLabMrUrl) -> MergeRequestReviewTarget:
        mr_data = self.gitlab.get_merge_request(mr)
        base_sha, head_sha = choose_diff_refs(mr_data)
        target_repo_url = self.gitlab.get_project_http_url(int(mr_data["target_project_id"]))
        source_repo_url = self.gitlab.get_project_http_url(int(mr_data["source_project_id"]))
        return MergeRequestReviewTarget(
            base_url=mr.base_url,
            project_path=mr.project_path,
            mr_iid=mr.mr_iid,
            mr_url=f"{mr.base_url}/{mr.project_path}/merge_requests/{mr.mr_iid}",
            target_repo_url=target_repo_url,
            source_repo_url=source_repo_url,
            target_branch=mr_data["target_branch"],
            source_branch=mr_data["source_branch"],
            base_sha=base_sha,
            head_sha=head_sha,
            title=str(mr_data.get("title") or ""),
        )

    def review_set(self, request: ReviewSetRequest, config: Config, task_id: str) -> ReviewSetReviewReport:
        task_dir = config.work_dir / task_id
        deadline = time.monotonic() + config.task_timeout_seconds
        review_plan = None
        agent_call_count = 0
        try:
            prepared = ReviewSetPreparer(self.gitlab, self.git).prepare(request, config, task_dir)
            member_ids = {member.member.member_id for member in prepared.members}
            LOG.info(
                "task=%s review_scope=review-set review_set_id=%s req_id=%s members=%s stage=prepared",
                task_id,
                prepared.manifest.review_set_id,
                prepared.manifest.req_id,
                len(prepared.members),
            )

            plan_prompt = build_review_set_plan_prompt(
                review_set_id=prepared.manifest.review_set_id,
                req_id=prepared.manifest.req_id,
            )
            try:
                agent_call_count += 1
                with task_stage("review_set_plan"):
                    raw_plan = self.opencode.run_review(
                        plan_prompt,
                        task_dir,
                        _remaining_timeout(deadline),
                        plan_prompt.metadata,
                    )
                review_plan = parse_review_set_plan(raw_plan, member_ids)
            except Exception as exc:  # noqa: BLE001 - 联合任务必须保留明确失败阶段且停止第二次调用。
                raise ReviewStageError("review_set_plan", exc, agent_call_count=agent_call_count) from exc

            review_prompt = build_review_set_review_prompt(
                review_set_id=prepared.manifest.review_set_id,
                req_id=prepared.manifest.req_id,
                review_plan=review_plan,
            )
            try:
                agent_call_count += 1
                with task_stage("review_set_review"):
                    raw_result = self.opencode.run_review(
                        review_prompt,
                        task_dir,
                        _remaining_timeout(deadline),
                        review_prompt.metadata,
                    )
                result = parse_structured_review_set_result(raw_result)
            except Exception as exc:  # noqa: BLE001 - 结构化结果失败时不得进入发布阶段。
                raise ReviewStageError("review_set_review", exc, review_plan, agent_call_count) from exc

            return ReviewSetReviewReport(
                manifest=prepared.manifest,
                review_plan=review_plan,
                result=result,
                members=prepared.members,
                prompt_templates={
                    "review_set_plan": {
                        "id": plan_prompt.template_id,
                        "version": plan_prompt.template_version,
                    },
                    "review_set_review": {
                        "id": review_prompt.template_id,
                        "version": review_prompt.template_version,
                    },
                },
                agent_call_count=agent_call_count,
            )
        finally:
            # ReviewSet 根目录同时包含多个源码 checkout，任何退出路径都必须整组清理。
            shutil.rmtree(task_dir, ignore_errors=True)
            LOG.info("task=%s review_scope=review-set stage=cleanup path=%s", task_id, task_dir)

    def review_target(
            self,
            target: MergeRequestReviewTarget,
            config: Config,
            task_id: str,
            structured_output: bool = True,
            should_cancel: Callable[[], bool] | None = None,
    ) -> ReviewReport:
        task_dir = config.work_dir / task_id
        deadline = time.monotonic() + config.task_timeout_seconds
        try:
            _raise_if_cancelled(should_cancel)
            LOG.info(
                "task=%s stage=gitlab_ready repo=%s source=%s target=%s",
                task_id,
                target.project_path,
                target.source_branch,
                target.target_branch,
            )
            diff_info = self.git.clone_checkout_and_diff(
                GitCheckout(
                    target_repo_url=target.target_repo_url,
                    source_repo_url=target.source_repo_url,
                    target_branch=target.target_branch,
                    source_branch=target.source_branch,
                    base_sha=target.base_sha,
                    head_sha=target.head_sha,
                    cancel_check=lambda: _raise_if_cancelled(should_cancel),
                ),
                config.gitlab_token,
                task_dir,
                {"max_files": config.max_files, "max_diff_lines": config.max_diff_lines},
            )
            _raise_if_cancelled(should_cancel)
            LOG.info(
                "task=%s stage=diff_ready repo=%s files=%s diff_lines=%s",
                task_id,
                target.project_path,
                len(diff_info["changed_files"]),
                len(diff_info["diff"].splitlines()),
            )
            routing = resolve_review_routing(target.title)
            dependency_context = self._prepare_dependency_context(
                target,
                config,
                routing,
                diff_info,
                task_dir,
                task_id,
            )
            _raise_if_cancelled(should_cancel)
            route_context = self._dependency_report_context(dependency_context)
            selection = dependency_context.selection
            LOG.info(
                "task=%s stage=review_routing requested_review_mode=%s review_mode=%s review_scope=%s "
                "dependency_context_status=%s degradation_reason=%s routing_reason=%s routing_marker=%s",
                task_id,
                selection.requested_review_mode,
                selection.review_mode,
                selection.review_scope,
                route_context["dependency_context_status"],
                selection.degradation_reason,
                routing.routing_reason,
                routing.routing_marker,
            )

            if dependency_context.prepared is not None:
                execution = self._run_dependency_review(
                    dependency_context.prepared,
                    task_dir,
                    deadline,
                    task_id,
                    route_context,
                    should_cancel,
                )
            else:
                execution = self._run_single_repository_review(
                    target,
                    config,
                    diff_info,
                    selection,
                    deadline,
                    task_id,
                    route_context,
                    structured_output,
                    should_cancel,
                )

            LOG.info(
                "task=%s stage=report_ready repo=%s report_chars=%s",
                task_id,
                target.project_path,
                len(execution.markdown),
            )
            uses_comment_skill = dependency_context.prepared is None and bool(config.comment_skill)
            return ReviewReport(
                markdown=execution.markdown,
                summary=None,
                review_plan=execution.review_plan,
                repo=target.project_path,
                mr_iid=target.mr_iid,
                mr_url=target.mr_url,
                source_branch=target.source_branch,
                target_branch=target.target_branch,
                base_sha=str(diff_info["base_sha"]),
                head_sha=str(diff_info["head_sha"]),
                changed_files=list(diff_info["changed_files"]),
                diff=str(diff_info["diff"]),
                opencode_returncode=0,
                submission_owner="skill" if uses_comment_skill else "none",
                submission_status="unknown" if uses_comment_skill else "not_configured",
                prompt_templates=execution.prompt_templates,
                title=target.title,
                routing_reason=routing.routing_reason,
                routing_marker=routing.routing_marker,
                dependency_relationship_summary=execution.relationship_summary,
                agent_call_count=execution.agent_call_count,
                structured_parse_status=execution.structured_parse_status,
                rejected_findings=execution.rejected_findings,
                normalization_warnings=execution.normalization_warnings,
                **route_context,
            )
        finally:
            # 任务目录含 clone 仓库和临时鉴权脚本，任何结果路径都必须清理。
            shutil.rmtree(task_dir, ignore_errors=True)
            LOG.info("task=%s stage=cleanup path=%s", task_id, task_dir)

    def _prepare_dependency_context(
            self,
            target: MergeRequestReviewTarget,
            config: Config,
            routing,
            diff_info: dict,
            task_dir: Path,
            task_id: str,
    ) -> _DependencyContext:
        selection = self._select_dependency_review(target, config, routing)
        if selection.review_scope != "dependency-review":
            return _DependencyContext(selection)

        preparation_started = time.monotonic()
        try:
            try:
                primary_repo_path = Path(diff_info["repo_path"]).relative_to(task_dir).as_posix()
            except ValueError as exc:
                raise DependencyReviewPreparationError(
                    "primary_context_invalid",
                    target.project_path,
                    "primary repository path is outside the task directory",
                ) from exc
            prepared = DependencyReviewPreparer(self.gitlab, self.git).prepare(
                primary=DependencyReviewPrimary(
                    project_path=target.project_path,
                    mr_iid=target.mr_iid,
                    base_sha=str(diff_info["base_sha"]),
                    head_sha=str(diff_info["head_sha"]),
                    target_branch=target.target_branch,
                    repo_path=primary_repo_path,
                ),
                dependency_project_paths=selection.dependencies,
                token=config.gitlab_token,
                task_dir=task_dir,
            )
            for dependency in prepared.dependencies:
                LOG.info(
                    "task=%s stage=dependency_prepare status=ready project=%s branch=%s "
                    "commit=%s elapsed=%.3fs",
                    task_id,
                    dependency.repository.project_path,
                    dependency.repository.branch,
                    dependency.repository.commit_sha,
                    dependency.preparation_seconds,
                )
            LOG.info(
                "task=%s stage=dependency_prepare status=complete dependencies=%s elapsed=%.3fs",
                task_id,
                len(prepared.dependencies),
                prepared.preparation_seconds,
            )
            return _DependencyContext(
                selection=selection,
                prepared=prepared,
                preparation_seconds=prepared.preparation_seconds,
            )
        except DependencyReviewPreparationError as exc:
            preparation_seconds = time.monotonic() - preparation_started
            degraded = degrade_dependency_review(selection, exc.reason_code)
            LOG.warning(
                "task=%s stage=dependency_prepare status=degraded project=%s reason=%s elapsed=%.3fs",
                task_id,
                exc.project_path,
                exc.reason_code,
                preparation_seconds,
            )
            return _DependencyContext(
                selection=degraded,
                failed_project=exc.project_path,
                preparation_seconds=preparation_seconds,
            )

    def _dependency_report_context(self, context: _DependencyContext) -> dict[str, object]:
        prepared = context.prepared
        selection = context.selection
        dependency_context_status = (
            "complete"
            if prepared is not None
            else "degraded"
            if selection.degradation_reason
            else "not_applicable"
        )
        return {
            "requested_review_mode": selection.requested_review_mode,
            "review_mode": selection.review_mode,
            "review_scope": selection.review_scope,
            "dependency_context_status": dependency_context_status,
            "dependency_degradation_reason": selection.degradation_reason,
            "dependency_failed_project": context.failed_project,
            "dependency_context_id": prepared.manifest.context_id if prepared is not None else "",
            "dependency_repositories": self._dependency_repository_report(prepared),
            "dependency_preparation_seconds": context.preparation_seconds,
        }

    def _run_single_repository_review(
            self,
            target: MergeRequestReviewTarget,
            config: Config,
            diff_info: dict,
            selection: DependencyReviewSelection,
            deadline: float,
            task_id: str,
            report_context: dict[str, object],
            structured_output: bool,
            should_cancel: Callable[[], bool] | None,
    ) -> _ReviewExecution:
        review_plan = None
        prompt_templates: dict[str, dict[str, str]] = {}
        agent_call_count = 0
        if selection.review_mode == "two-step":
            _raise_if_cancelled(should_cancel)
            plan_prompt = self._build_review_plan_prompt(target, diff_info)
            LOG.info("task=%s stage=review_plan repo=%s status=started", task_id, target.project_path)
            try:
                agent_call_count += 1
                with task_stage("review_plan"):
                    plan_raw = self.opencode.run_review(
                        plan_prompt,
                        diff_info["repo_path"],
                        _remaining_timeout(deadline),
                        plan_prompt.metadata,
                    )
                review_plan = parse_review_plan(plan_raw)
            except Exception as exc:  # noqa: BLE001 - 对外保留明确的执行阶段。
                raise ReviewStageError(
                    "review_plan",
                    exc,
                    agent_call_count=agent_call_count,
                    report_context=report_context,
                ) from exc
            prompt_templates["review_plan"] = {
                "id": plan_prompt.template_id,
                "version": plan_prompt.template_version,
            }
            _raise_if_cancelled(should_cancel)
            LOG.info("task=%s stage=review_plan repo=%s status=ready", task_id, target.project_path)

        # Agent 已在本地 checkout 后的仓库中运行，prompt 只传定位信息，避免把大 diff 塞进模型上下文。
        prompt = self._build_prompt(
            target,
            diff_info,
            config.comment_skill or DEFAULT_REVIEW_SKILL,
            structured_output,
            review_plan,
        )
        LOG.info(
            "task=%s stage=opencode_review repo=%s timeout_seconds=%s",
            task_id,
            target.project_path,
            config.task_timeout_seconds,
        )
        try:
            _raise_if_cancelled(should_cancel)
            agent_call_count += 1
            with task_stage("review"):
                markdown = self.opencode.run_review(
                    prompt,
                    diff_info["repo_path"],
                    _remaining_timeout(deadline),
                    prompt.metadata,
                )
            _raise_if_cancelled(should_cancel)
        except Exception as exc:  # noqa: BLE001 - 对外保留概要和失败阶段。
            raise ReviewStageError(
                "review",
                exc,
                review_plan,
                agent_call_count,
                report_context,
            ) from exc
        prompt_templates["review"] = {
            "id": prompt.template_id,
            "version": prompt.template_version,
        }
        return _ReviewExecution(
            markdown=markdown,
            review_plan=review_plan,
            relationship_summary=[],
            prompt_templates=prompt_templates,
            agent_call_count=agent_call_count,
        )

    def _select_dependency_review(self, target, config, routing) -> DependencyReviewSelection:
        # 普通 one-step 在这里直接返回，确保不会触碰可选目录文件。
        if routing.review_mode == "one-step" or config.repository_dependency_catalog is None:
            return select_dependency_review(routing, target.project_path)
        try:
            catalog = load_repository_dependency_catalog(config.repository_dependency_catalog)
        except RepositoryDependencyCatalogError as exc:
            return select_dependency_review(
                routing,
                target.project_path,
                catalog_error=exc.reason_code,
            )
        return select_dependency_review(routing, target.project_path, catalog)

    @staticmethod
    def _dependency_repository_report(prepared: PreparedDependencyReview | None) -> list[dict]:
        if prepared is None:
            return []
        return [
            {
                "repo_id": item.repository.repo_id,
                "project_path": item.repository.project_path,
                "branch": item.repository.branch,
                "commit_sha": item.repository.commit_sha,
                "preparation_seconds": item.preparation_seconds,
            }
            for item in prepared.dependencies
        ]

    def _run_dependency_review(
            self,
            prepared: PreparedDependencyReview,
            task_dir: Path,
            deadline: float,
            task_id: str,
            report_context: dict[str, object],
            should_cancel: Callable[[], bool] | None,
    ) -> _ReviewExecution:
        agent_call_count = 0
        _raise_if_cancelled(should_cancel)
        plan_prompt = build_dependency_review_plan_prompt(context_id=prepared.manifest.context_id)
        try:
            agent_call_count += 1
            with task_stage("dependency_review_plan"):
                raw_plan = self.opencode.run_review(
                    plan_prompt,
                    task_dir,
                    _remaining_timeout(deadline),
                    plan_prompt.metadata,
                )
            review_plan = parse_dependency_review_plan(raw_plan, prepared.manifest)
        except Exception as exc:  # noqa: BLE001 - 联合计划失败时不得进行第二次 Agent 调用。
            raise ReviewStageError(
                "dependency_review_plan",
                exc,
                agent_call_count=agent_call_count,
                report_context=report_context,
            ) from exc

        review_prompt = build_dependency_review_prompt(
            context_id=prepared.manifest.context_id,
            review_plan=review_plan,
        )
        _raise_if_cancelled(should_cancel)
        try:
            agent_call_count += 1
            with task_stage("dependency_review"):
                raw_result = self.opencode.run_review(
                    review_prompt,
                    task_dir,
                    _remaining_timeout(deadline),
                    review_prompt.metadata,
                )
            result = parse_structured_dependency_review_result(raw_result, prepared.manifest)
            _raise_if_cancelled(should_cancel)
        except Exception as exc:  # noqa: BLE001 - 非法联合结果不得进入主 MR 发布路径。
            raise ReviewStageError(
                "dependency_review",
                exc,
                review_plan,
                agent_call_count,
                report_context,
            ) from exc
        return _ReviewExecution(
            markdown=dependency_review_result_as_single_review_json(result),
            review_plan=review_plan,
            relationship_summary=result.relationship_summary,
            prompt_templates={
                "dependency_review_plan": {
                    "id": plan_prompt.template_id,
                    "version": plan_prompt.template_version,
                },
                "dependency_review": {
                    "id": review_prompt.template_id,
                    "version": review_prompt.template_version,
                },
            },
            agent_call_count=agent_call_count,
            structured_parse_status=result.structured_parse_status,
            rejected_findings=result.rejected_findings,
            normalization_warnings=result.normalization_warnings,
        )

    def _build_prompt(
            self,
            target: MergeRequestReviewTarget,
            diff_info: dict,
            skill_name: str,
            structured_output: bool = False,
            review_plan: dict[str, object] | None = None,
    ) -> str:
        if not structured_output:
            raise ValueError("automatic review prompt must use the structured template")
        return build_review_prompt(
            skill_name=skill_name,
            mr_url=target.mr_url,
            base_sha=str(diff_info["base_sha"]),
            head_sha=str(diff_info["head_sha"]),
            changed_files=list(diff_info["changed_files"]),
            repo_path=diff_info["repo_path"],
            review_plan=review_plan,
        )

    def _build_review_plan_prompt(self, target: MergeRequestReviewTarget, diff_info: dict) -> str:
        return build_review_plan_prompt(
            mr_url=target.mr_url,
            base_sha=str(diff_info["base_sha"]),
            head_sha=str(diff_info["head_sha"]),
            changed_files=list(diff_info["changed_files"]),
            repo_path=diff_info["repo_path"],
        )


def _remaining_timeout(deadline: float) -> int:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("review task timeout exhausted")
    return max(1, math.ceil(remaining))


def _raise_if_cancelled(should_cancel: Callable[[], bool] | None) -> None:
    if should_cancel is not None and should_cancel():
        raise ReviewCancelledError("review was superseded by a newer MR head")
