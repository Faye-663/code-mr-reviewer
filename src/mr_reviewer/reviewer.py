from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass

from mr_reviewer.config import Config
from mr_reviewer.git import GitCheckout, GitClient
from mr_reviewer.gitlab import GitLabClient, GitLabMrUrl, choose_diff_refs
from mr_reviewer.opencode import AgentRunner

LOG = logging.getLogger("mr_reviewer")
DEFAULT_REVIEW_SKILL = "code-review"


@dataclass(frozen=True, slots=True)
class ReviewReport:
    markdown: str
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


class ReviewService:
    def __init__(self, gitlab: GitLabClient, git: GitClient, opencode: AgentRunner):
        self.gitlab = gitlab
        self.git = git
        self.opencode = opencode

    def review(self, mr: GitLabMrUrl, config: Config, task_id: str) -> ReviewReport:
        LOG.info("task=%s stage=gitlab_fetch repo=%s mr_iid=%s", task_id, mr.project_path, mr.mr_iid)
        mr_data = self.gitlab.get_merge_request(mr)
        base_sha, head_sha = choose_diff_refs(mr_data)
        target_repo_url = self.gitlab.get_project_http_url(int(mr_data["target_project_id"]))
        source_repo_url = self.gitlab.get_project_http_url(int(mr_data["source_project_id"]))
        target = MergeRequestReviewTarget(
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
        )
        return self.review_target(target, config, task_id, structured_output=True)

    def review_target(
            self,
            target: MergeRequestReviewTarget,
            config: Config,
            task_id: str,
            structured_output: bool = True,
    ) -> ReviewReport:
        task_dir = config.work_dir / task_id
        try:
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
                ),
                config.gitlab_token,
                task_dir,
                {"max_files": config.max_files, "max_diff_lines": config.max_diff_lines},
            )
            LOG.info(
                "task=%s stage=diff_ready repo=%s files=%s diff_lines=%s",
                task_id,
                target.project_path,
                len(diff_info["changed_files"]),
                len(diff_info["diff"].splitlines()),
            )
            # opencode 已在本地 checkout 后的仓库中运行，prompt 只传定位信息，避免把大 diff 塞进模型上下文。
            prompt = self._build_prompt(
                target,
                diff_info,
                config.comment_skill or DEFAULT_REVIEW_SKILL,
                structured_output,
            )
            LOG.info("task=%s stage=opencode_review repo=%s timeout_seconds=%s", task_id, target.project_path,
                     config.task_timeout_seconds)
            markdown = self.opencode.run_review(prompt, diff_info["repo_path"], config.task_timeout_seconds)
            LOG.info("task=%s stage=report_ready repo=%s report_chars=%s", task_id, target.project_path, len(markdown))
            return ReviewReport(
                markdown=markdown,
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
                submission_owner="skill" if config.comment_skill else "none",
                submission_status="unknown" if config.comment_skill else "not_configured",
            )
        finally:
            # 任务目录含 clone 仓库和临时鉴权脚本，任何结果路径都必须清理。
            shutil.rmtree(task_dir, ignore_errors=True)
            LOG.info("task=%s stage=cleanup path=%s", task_id, task_dir)

    def _build_prompt(
            self,
            target: MergeRequestReviewTarget,
            diff_info: dict,
            skill_name: str,
            structured_output: bool = False,
    ) -> str:
        changed_files = "\n".join(f"- {path}" for path in diff_info["changed_files"]) or "- <none>"
        prompt = (
            f"使用 {skill_name} skill 检视 GitLab MR。\n"
            f"MR URL: {target.mr_url}\n"
            f"Base SHA: {diff_info['base_sha']}\n"
            f"Head SHA: {diff_info['head_sha']}\n"
            "Changed files:\n"
            f"{changed_files}\n"
            f"代码仓在 {diff_info['repo_path']} 目录。\n"
            "只审查 Base SHA 到 Head SHA 的 MR range，不要按本地未提交变更审查。"
        )
        if not structured_output:
            return prompt

        return (
            f"{prompt}\n\n"
            "自动检视模式必须只输出 JSON，不要输出 Markdown 或代码围栏。JSON 结构为：\n"
            '{"findings":[{"rule_id":"...","severity":"major","confidence":"HIGH",'
            '"old_path":"src/example.py","new_path":"src/example.py","old_line":-1,'
            '"new_line":42,"title":"...","evidence":"...","suggestion":"..."}],'
            '"notes":[],"test_gaps":[]}\n'
            "severity 只能使用 suggestion、minjor、major、fatal；confidence 只能使用 HIGH、MEDIUM、LOW。"
        )
