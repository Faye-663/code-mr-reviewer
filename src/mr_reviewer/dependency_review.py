from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from mr_reviewer.git import DependencyBranchMissingError, GitClient
from mr_reviewer.gitlab import GitLabClient


DEPENDENCY_REVIEW_SCHEMA_VERSION = "dependency-review/v1"
_COMMIT_SHA_PATTERN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


class DependencyReviewPreparationError(RuntimeError):
    def __init__(self, reason_code: str, project_path: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.project_path = project_path


@dataclass(frozen=True, slots=True)
class DependencyReviewPrimary:
    project_path: str
    mr_iid: int
    base_sha: str
    head_sha: str
    target_branch: str
    repo_path: str


@dataclass(frozen=True, slots=True)
class DependencyReviewRepository:
    repo_id: str
    project_path: str
    branch: str
    commit_sha: str
    repo_path: str


@dataclass(frozen=True, slots=True)
class DependencyReviewManifest:
    schema_version: str
    context_id: str
    primary: DependencyReviewPrimary
    dependencies: tuple[DependencyReviewRepository, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "context_id": self.context_id,
            "primary": asdict(self.primary),
            "dependencies": [asdict(dependency) for dependency in self.dependencies],
        }


@dataclass(frozen=True, slots=True)
class PreparedDependencyRepository:
    repository: DependencyReviewRepository
    repo_path: Path
    preparation_seconds: float


@dataclass(frozen=True, slots=True)
class PreparedDependencyReview:
    manifest: DependencyReviewManifest
    dependencies: tuple[PreparedDependencyRepository, ...]
    task_dir: Path
    preparation_seconds: float


class DependencyReviewPreparer:
    def __init__(self, gitlab: GitLabClient, git: GitClient) -> None:
        self.gitlab = gitlab
        self.git = git

    def prepare(
        self,
        *,
        primary: DependencyReviewPrimary,
        dependency_project_paths: tuple[str, ...],
        token: str,
        task_dir: Path,
    ) -> PreparedDependencyReview:
        started = time.monotonic()
        dependencies_root = task_dir / "dependencies"
        manifest_path = task_dir / "dependency-review.json"
        try:
            self._validate_primary(primary)
            if not 1 <= len(dependency_project_paths) <= 3:
                raise DependencyReviewPreparationError(
                    "dependency_count_invalid",
                    primary.project_path,
                    "dependency review preparation requires between one and three repositories",
                )
            if len(set(dependency_project_paths)) != len(dependency_project_paths):
                raise DependencyReviewPreparationError(
                    "dependency_count_invalid",
                    primary.project_path,
                    "dependency project paths must be unique",
                )

            prepared: list[PreparedDependencyRepository] = []
            repo_ids: set[str] = set()
            for project_path in sorted(dependency_project_paths):
                item = self._prepare_dependency(
                    project_path=project_path,
                    branch=primary.target_branch,
                    token=token,
                    dependencies_root=dependencies_root,
                )
                if item.repository.repo_id in repo_ids:
                    raise DependencyReviewPreparationError(
                        "dependency_project_lookup_failed",
                        project_path,
                        "GitLab returned the same project id for multiple dependencies",
                    )
                repo_ids.add(item.repository.repo_id)
                prepared.append(item)

            manifest = DependencyReviewManifest(
                schema_version=DEPENDENCY_REVIEW_SCHEMA_VERSION,
                context_id=self._context_id(primary.head_sha, tuple(item.repository for item in prepared)),
                primary=primary,
                dependencies=tuple(item.repository for item in prepared),
            )
            task_dir.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            return PreparedDependencyReview(
                manifest=manifest,
                dependencies=tuple(prepared),
                task_dir=task_dir,
                preparation_seconds=time.monotonic() - started,
            )
        except DependencyReviewPreparationError:
            self._cleanup(dependencies_root, manifest_path)
            raise
        except Exception as exc:
            self._cleanup(dependencies_root, manifest_path)
            raise DependencyReviewPreparationError(
                "dependency_manifest_write_failed",
                primary.project_path,
                "dependency review manifest could not be written",
            ) from exc

    def _prepare_dependency(
        self,
        *,
        project_path: str,
        branch: str,
        token: str,
        dependencies_root: Path,
    ) -> PreparedDependencyRepository:
        try:
            project = self.gitlab.get_project(project_path)
            project_id, repo_url = self._project_metadata(project_path, project)
        except Exception as exc:
            raise DependencyReviewPreparationError(
                "dependency_project_lookup_failed",
                project_path,
                f"dependency project metadata is invalid: {project_path}",
            ) from exc

        repo_id = f"p{project_id}"
        relative_repo_path = str(PurePosixPath("dependencies", repo_id, "repo"))
        repo_path = dependencies_root / repo_id / "repo"
        started = time.monotonic()
        try:
            commit_sha = self.git.clone_checkout_branch(repo_url, branch, token, repo_path)
        except DependencyBranchMissingError as exc:
            raise DependencyReviewPreparationError(
                "dependency_branch_missing",
                project_path,
                f"dependency target branch does not exist: {project_path}@{branch}",
            ) from exc
        except Exception as exc:
            raise DependencyReviewPreparationError(
                "dependency_checkout_failed",
                project_path,
                f"dependency checkout failed: {project_path}@{branch}",
            ) from exc
        if not _COMMIT_SHA_PATTERN.fullmatch(commit_sha):
            raise DependencyReviewPreparationError(
                "dependency_checkout_failed",
                project_path,
                f"dependency checkout returned an invalid commit SHA: {project_path}",
            )

        repository = DependencyReviewRepository(
            repo_id=repo_id,
            project_path=project_path,
            branch=branch,
            commit_sha=commit_sha,
            repo_path=relative_repo_path,
        )
        return PreparedDependencyRepository(repository, repo_path, time.monotonic() - started)

    @staticmethod
    def _project_metadata(project_path: str, project: object) -> tuple[int, str]:
        if not isinstance(project, dict):
            raise ValueError("GitLab project response must be an object")
        project_id = project.get("id")
        if isinstance(project_id, bool) or not isinstance(project_id, int) or project_id <= 0:
            raise ValueError("GitLab project id must be a positive integer")
        if project.get("path_with_namespace") != project_path:
            raise ValueError("GitLab project path does not match dependency catalog")
        repo_url = project.get("http_url_to_repo")
        if not isinstance(repo_url, str) or not repo_url:
            raise ValueError("GitLab project response has no HTTPS clone URL")
        parsed = urllib.parse.urlparse(repo_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("GitLab clone URL must be an HTTPS URL without credentials")
        return project_id, repo_url

    @staticmethod
    def _validate_primary(primary: DependencyReviewPrimary) -> None:
        if not primary.project_path or not primary.target_branch:
            raise DependencyReviewPreparationError(
                "primary_context_invalid",
                primary.project_path,
                "primary project path and target branch must be non-empty",
            )
        if isinstance(primary.mr_iid, bool) or not isinstance(primary.mr_iid, int) or primary.mr_iid <= 0:
            raise DependencyReviewPreparationError(
                "primary_context_invalid",
                primary.project_path,
                "primary MR iid must be a positive integer",
            )
        if not _COMMIT_SHA_PATTERN.fullmatch(primary.base_sha) or not _COMMIT_SHA_PATTERN.fullmatch(primary.head_sha):
            raise DependencyReviewPreparationError(
                "primary_context_invalid",
                primary.project_path,
                "primary base/head SHA must be full commit identifiers",
            )
        repo_path = PurePosixPath(primary.repo_path)
        if repo_path.is_absolute() or ".." in repo_path.parts or not primary.repo_path:
            raise DependencyReviewPreparationError(
                "primary_context_invalid",
                primary.project_path,
                "primary repo_path must be a safe task-relative path",
            )

    @staticmethod
    def _context_id(
        head_sha: str,
        dependencies: tuple[DependencyReviewRepository, ...],
    ) -> str:
        identities = sorted(f"{item.project_path}@{item.commit_sha}" for item in dependencies)
        canonical = "\n".join([head_sha, *identities])
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _cleanup(dependencies_root: Path, manifest_path: Path) -> None:
        # 主仓还要用于降级后的单仓 review，因此失败时只移除依赖上下文。
        shutil.rmtree(dependencies_root, ignore_errors=True)
        try:
            manifest_path.unlink(missing_ok=True)
        except OSError:
            pass
