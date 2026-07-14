from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from mr_reviewer.config import Config
from mr_reviewer.git import GitCheckout, GitClient
from mr_reviewer.gitlab import GitLabClient, GitLabMrUrl
from mr_reviewer.im import ReviewSetRequest

REVIEW_SET_SCHEMA_VERSION = "review-set/v1"


class ReviewSetValidationError(ValueError):
    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class ReviewSetMember:
    member_id: str
    project_id: int
    project_path: str
    mr_iid: int
    mr_url: str
    target_repo_url: str
    source_repo_url: str
    target_branch: str
    source_branch: str
    base_sha: str
    start_sha: str
    head_sha: str
    repo_path: str


@dataclass(frozen=True, slots=True)
class ReviewSetManifest:
    schema_version: str
    review_set_id: str
    req_id: str
    members: tuple[ReviewSetMember, ...]
    resource_limits: dict[str, int]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "review_set_id": self.review_set_id,
            "req_id": self.req_id,
            "members": [asdict(member) for member in self.members],
            "resource_limits": dict(self.resource_limits),
        }


@dataclass(frozen=True, slots=True)
class PreparedReviewSetMember:
    member: ReviewSetMember
    repo_path: Path
    diff: str
    changed_files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PreparedReviewSet:
    manifest: ReviewSetManifest
    members: tuple[PreparedReviewSetMember, ...]
    task_dir: Path


@dataclass(frozen=True, slots=True)
class _MemberMetadata:
    mr: GitLabMrUrl
    project_id: int
    req_id: str
    target_repo_url: str
    source_repo_url: str
    target_branch: str
    source_branch: str
    base_sha: str
    start_sha: str
    head_sha: str

    @property
    def member_id(self) -> str:
        return f"p{self.project_id}-mr{self.mr.mr_iid}"


def extract_req_id(mr_detail: dict) -> str:
    # 组织侧契约明确绑定首个 e2e issue，不能用后续元素或相近字段猜测需求关联。
    issues = mr_detail.get("e2e_issues")
    if not isinstance(issues, list) or not issues or not isinstance(issues[0], dict):
        raise ReviewSetValidationError("req_id_missing", "MR detail does not include a valid first e2e issue")
    req_id = issues[0].get("issue_num")
    if not isinstance(req_id, str) or not req_id.strip():
        raise ReviewSetValidationError("req_id_missing", "MR detail first e2e issue has no valid issue_num")
    return req_id.strip()


class ReviewSetPreparer:
    def __init__(self, gitlab: GitLabClient, git: GitClient):
        self.gitlab = gitlab
        self.git = git

    def prepare(self, request: ReviewSetRequest, config: Config, task_dir: Path) -> PreparedReviewSet:
        try:
            # 先完成整组元数据和 ReqID 校验，再产生任何 clone 副作用。
            metadata = tuple(self._load_member_metadata(mr) for mr in request.members)
            req_id = self._shared_req_id(metadata)
            review_set_id = self._review_set_id(req_id, metadata)
            limits = {"max_files": config.max_files, "max_diff_lines": config.max_diff_lines}
            prepared_members = tuple(
                self._checkout_member(item, config.gitlab_token, task_dir, limits) for item in metadata
            )
            manifest = ReviewSetManifest(
                schema_version=REVIEW_SET_SCHEMA_VERSION,
                review_set_id=review_set_id,
                req_id=req_id,
                members=tuple(item.member for item in prepared_members),
                resource_limits=limits,
            )
            task_dir.mkdir(parents=True, exist_ok=True)
            (task_dir / "review-set.json").write_text(
                json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            return PreparedReviewSet(manifest, prepared_members, task_dir)
        except Exception:
            shutil.rmtree(task_dir, ignore_errors=True)
            raise

    def _load_member_metadata(self, mr: GitLabMrUrl) -> _MemberMetadata:
        # project_id 必须来自 project path 查询；MR URL 本身只可信地提供 project path 和 iid。
        project = self.gitlab.get_project(mr.project_path)
        project_id = _required_int(project, "id", "project_metadata_invalid")
        detail = self.gitlab.get_review_set_merge_request(project_id, mr.mr_iid)
        if _required_int(detail, "project_id", "mr_detail_mismatch") != project_id:
            raise ReviewSetValidationError("mr_detail_mismatch", "MR detail project_id does not match project")
        if _required_int(detail, "iid", "mr_detail_mismatch") != mr.mr_iid:
            raise ReviewSetValidationError("mr_detail_mismatch", "MR detail iid does not match MR URL")
        base_sha, start_sha, head_sha = _diff_refs(detail)
        req_id = extract_req_id(detail)

        mr_data = self.gitlab.get_merge_request(mr)
        target_project_id = _required_int(mr_data, "target_project_id", "mr_metadata_invalid")
        source_project_id = _required_int(mr_data, "source_project_id", "mr_metadata_invalid")
        return _MemberMetadata(
            mr=mr,
            project_id=project_id,
            req_id=req_id,
            target_repo_url=self.gitlab.get_project_http_url(target_project_id),
            source_repo_url=self.gitlab.get_project_http_url(source_project_id),
            target_branch=_required_text(mr_data, "target_branch", "mr_metadata_invalid"),
            source_branch=_required_text(mr_data, "source_branch", "mr_metadata_invalid"),
            base_sha=base_sha,
            start_sha=start_sha,
            head_sha=head_sha,
        )

    def _checkout_member(
            self,
            metadata: _MemberMetadata,
            token: str,
            task_dir: Path,
            limits: dict[str, int],
    ) -> PreparedReviewSetMember:
        member_root = task_dir / "members" / metadata.member_id
        diff_info = self.git.clone_checkout_and_diff(
            GitCheckout(
                target_repo_url=metadata.target_repo_url,
                source_repo_url=metadata.source_repo_url,
                target_branch=metadata.target_branch,
                source_branch=metadata.source_branch,
                base_sha=metadata.base_sha,
                head_sha=metadata.head_sha,
            ),
            token,
            member_root,
            limits,
        )
        relative_repo_path = str(PurePosixPath("members", metadata.member_id, "repo"))
        member = ReviewSetMember(
            member_id=metadata.member_id,
            project_id=metadata.project_id,
            project_path=metadata.mr.project_path,
            mr_iid=metadata.mr.mr_iid,
            mr_url=f"{metadata.mr.base_url}/{metadata.mr.project_path}/merge_requests/{metadata.mr.mr_iid}",
            target_repo_url=metadata.target_repo_url,
            source_repo_url=metadata.source_repo_url,
            target_branch=metadata.target_branch,
            source_branch=metadata.source_branch,
            base_sha=metadata.base_sha,
            start_sha=metadata.start_sha,
            head_sha=metadata.head_sha,
            repo_path=relative_repo_path,
        )
        return PreparedReviewSetMember(
            member=member,
            repo_path=Path(diff_info["repo_path"]),
            diff=str(diff_info["diff"]),
            changed_files=tuple(diff_info["changed_files"]),
        )

    @staticmethod
    def _shared_req_id(metadata: tuple[_MemberMetadata, ...]) -> str:
        req_ids = {member.req_id for member in metadata}
        if len(req_ids) != 1:
            raise ReviewSetValidationError("req_id_mismatch", "ReviewSet members have different ReqID values")
        return next(iter(req_ids))

    @staticmethod
    def _review_set_id(req_id: str, metadata: tuple[_MemberMetadata, ...]) -> str:
        # 排序消除 IM URL 顺序差异，head SHA 则保证 MR 更新后形成新的检视集合。
        identities = sorted(
            f"{member.project_id}/{member.mr.mr_iid}@{member.head_sha}" for member in metadata
        )
        canonical = "\n".join([REVIEW_SET_SCHEMA_VERSION, req_id, *identities])
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _diff_refs(detail: dict) -> tuple[str, str, str]:
    refs = detail.get("diff_refs")
    if not isinstance(refs, dict):
        raise ReviewSetValidationError("diff_refs_invalid", "MR detail diff_refs must be an object")
    return (
        _required_text(refs, "base_sha", "diff_refs_invalid"),
        _required_text(refs, "start_sha", "diff_refs_invalid"),
        _required_text(refs, "head_sha", "diff_refs_invalid"),
    )


def _required_text(payload: dict, field: str, reason_code: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ReviewSetValidationError(reason_code, f"{field} must be a non-empty string")
    return value.strip()


def _required_int(payload: dict, field: str, reason_code: str) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReviewSetValidationError(reason_code, f"{field} must be an integer")
    return value
