from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class GitLabMrUrl:
    base_url: str
    project_path: str
    mr_iid: int


def _normalized_base(base_url: str) -> str:
    return base_url.rstrip("/")


def parse_gitlab_mr_url(url: str, base_url: str) -> GitLabMrUrl:
    parsed = urllib.parse.urlparse(url)
    base = urllib.parse.urlparse(_normalized_base(base_url))

    if (parsed.scheme, parsed.netloc.lower()) != (base.scheme, base.netloc.lower()):
        raise ValueError("GitLab host does not match configured base URL")

    marker = "/merge_requests/"
    if marker not in parsed.path:
        raise ValueError("URL is not a GitLab merge request URL")

    project_part, iid_part = parsed.path.split(marker, 1)
    project_path = urllib.parse.unquote(project_part.strip("/"))
    iid_text = iid_part.strip("/").split("/", 1)[0]
    if not project_path or not iid_text.isdigit():
        raise ValueError("GitLab MR URL is missing project path or MR IID")

    return GitLabMrUrl(_normalized_base(base_url), project_path, int(iid_text))


def choose_diff_refs(mr: dict) -> tuple[str, str]:
    diff_refs = mr.get("diff_refs") or {}
    base_sha = diff_refs.get("base_sha") or diff_refs.get("start_sha")
    head_sha = diff_refs.get("head_sha") or mr.get("sha")
    if not base_sha or not head_sha:
        raise ValueError("GitLab MR response does not include usable diff refs")
    return base_sha, head_sha


class GitLabClient:
    def __init__(self, base_url: str, token: str, fixture_path: Path | None = None):
        self.base_url = _normalized_base(base_url)
        self.token = token
        self.fixture_path = fixture_path
        self._fixtures = self._load_fixtures(fixture_path)

    def _load_fixtures(self, fixture_path: Path | None) -> dict[str, dict]:
        if not fixture_path:
            return {}
        return json.loads(fixture_path.read_text(encoding="utf-8"))

    def get_merge_request(self, mr: GitLabMrUrl) -> dict:
        project = urllib.parse.quote(mr.project_path, safe="")
        return self._get_json(f"/projects/{project}/merge_requests/{mr.mr_iid}")

    def get_mr_detail_for_discussion_position(self, target) -> dict:
        project = urllib.parse.quote(target.project_path, safe="")
        return self._get_json(f"/projects/{project}/merge_requests/{target.mr_iid}")

    def get_project_http_url(self, project_id: int) -> str:
        project = self._get_json(f"/projects/{project_id}")
        repo_url = project.get("http_url_to_repo")
        if not repo_url:
            raise ValueError("GitLab project response does not include http_url_to_repo")
        return repo_url

    def post_mr_note(self, mr: GitLabMrUrl, body: str) -> dict:
        project = urllib.parse.quote(mr.project_path, safe="")
        return self._post_form(
            f"/projects/{project}/merge_requests/{mr.mr_iid}/notes",
            {"body": body},
        )

    def list_mr_discussions(self, target) -> list[dict]:
        project = urllib.parse.quote(target.project_path, safe="")
        discussions = self._get_json(f"/projects/{project}/merge_requests/{target.mr_iid}/discussions")
        if not isinstance(discussions, list):
            raise ValueError("GitLab discussions response must be a list")
        return discussions

    def post_mr_discussion(self, target, body: str, severity: str, position: dict) -> dict:
        project = urllib.parse.quote(target.project_path, safe="")
        return self._post_json(
            f"/projects/{project}/merge_requests/{target.mr_iid}/discussions",
            {"body": body, "severity": severity, "position": position},
        )

    def _post_form(self, path: str, fields: dict[str, str]) -> dict:
        if not self.token:
            raise ValueError("GitLab token is required")

        data = urllib.parse.urlencode(fields).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method="POST",
            headers={
                "PRIVATE-TOKEN": self.token,
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"GitLab API request failed: HTTP {exc.code}") from exc

    def _post_json(self, path: str, payload: dict) -> dict:
        if not self.token:
            raise ValueError("GitLab token is required")

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method="POST",
            headers={
                "PRIVATE-TOKEN": self.token,
                "Accept": "application/json",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"GitLab API request failed: HTTP {exc.code}") from exc

    def _get_json(self, path: str):
        if path in self._fixtures:
            return self._fixtures[path]

        if not self.token:
            raise ValueError("GitLab token is required")

        request = urllib.request.Request(
            f"{self.base_url}{path}",
            headers={"PRIVATE-TOKEN": self.token, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"GitLab API request failed: HTTP {exc.code}") from exc
