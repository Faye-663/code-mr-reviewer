from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass

from mr_reviewer.config import Config
from mr_reviewer.git import GitClient
from mr_reviewer.gitlab import GitLabClient, GitLabMrUrl, choose_diff_refs
from mr_reviewer.opencode import OpenCodeRunner


LOG = logging.getLogger("mr_reviewer")


@dataclass(frozen=True, slots=True)
class ReviewReport:
    markdown: str


class ReviewService:
    def __init__(self, gitlab: GitLabClient, git: GitClient, opencode: OpenCodeRunner):
        self.gitlab = gitlab
        self.git = git
        self.opencode = opencode

    def review(self, mr: GitLabMrUrl, config: Config, task_id: str) -> ReviewReport:
        task_dir = config.work_dir / task_id
        try:
            LOG.info("task=%s stage=gitlab_fetch repo=%s mr_iid=%s", task_id, mr.project_path, mr.mr_iid)
            mr_data = self.gitlab.get_merge_request(mr)
            base_sha, head_sha = choose_diff_refs(mr_data)
            repo_url = self.gitlab.get_project_http_url(int(mr_data["target_project_id"]))
            LOG.info(
                "task=%s stage=gitlab_ready repo=%s source=%s target=%s",
                task_id,
                mr.project_path,
                mr_data.get("source_branch", ""),
                mr_data.get("target_branch", ""),
            )
            diff_info = self.git.clone_checkout_and_diff(
                repo_url,
                config.gitlab_token,
                base_sha,
                head_sha,
                task_dir,
                {"max_files": config.max_files, "max_diff_lines": config.max_diff_lines},
            )
            LOG.info(
                "task=%s stage=diff_ready repo=%s files=%s diff_lines=%s",
                task_id,
                mr.project_path,
                len(diff_info["changed_files"]),
                len(diff_info["diff"].splitlines()),
            )
            prompt = self._build_prompt(mr, mr_data, diff_info)
            LOG.info("task=%s stage=opencode_review repo=%s timeout_seconds=%s", task_id, mr.project_path, config.task_timeout_seconds)
            markdown = self.opencode.run_review(prompt, diff_info["repo_path"], config.task_timeout_seconds)
            LOG.info("task=%s stage=report_ready repo=%s report_chars=%s", task_id, mr.project_path, len(markdown))
            return ReviewReport(markdown=markdown)
        finally:
            # 任务目录含 clone 仓库和临时鉴权脚本，任何结果路径都必须清理。
            shutil.rmtree(task_dir, ignore_errors=True)
            LOG.info("task=%s stage=cleanup path=%s", task_id, task_dir)

    def _build_prompt(self, mr: GitLabMrUrl, mr_data: dict, diff_info: dict) -> str:
        # 显式点名 skill，避免依赖模型自动触发。
        return "\n".join(
            [
                "请使用 mr-review skill 对以下 GitLab Merge Request 做保守代码检视。",
                "只输出 Markdown 检视报告，不要提交 MR 评论。",
                "",
                f"MR: {mr.base_url}/{mr.project_path}/-/merge_requests/{mr.mr_iid}",
                f"Title: {mr_data.get('title', '')}",
                f"Source branch: {mr_data.get('source_branch', '')}",
                f"Target branch: {mr_data.get('target_branch', '')}",
                f"Base SHA: {diff_info['base_sha']}",
                f"Head SHA: {diff_info['head_sha']}",
                f"Changed files: {', '.join(diff_info['changed_files'])}",
                "",
                "Diff:",
                "```diff",
                diff_info["diff"],
                "```",
            ]
        )
