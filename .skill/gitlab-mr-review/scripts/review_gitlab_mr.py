from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from string import Template
from typing import NamedTuple


class RenderedPrompt(str):
    """独立 skill 保持字符串兼容，同时暴露模板审计元数据。"""

    def __new__(cls, content: str, template_id: str, template_version: str):
        value = super().__new__(cls, content)
        value.template_id = template_id
        value.template_version = template_version
        return value

    @property
    def content(self) -> str:
        return str(self)


class MrUrl(NamedTuple):
    base_url: str
    project_path: str
    mr_iid: int


class Config(NamedTuple):
    gitlab_base_url: str
    gitlab_api_base_url: str
    gitlab_token: str
    agent_type: str
    agent_command: str
    work_dir: Path
    submit_comment: bool


class ReviewRoutingDecision(NamedTuple):
    review_mode: str
    routing_reason: str
    routing_marker: str


DEEP_REVIEW_MARKER = "【Deep-Review】"
DEEP_REVIEW_MARKERS = (DEEP_REVIEW_MARKER, "[Deep-Review]")
ALLOWED_SEVERITIES = {"suggestion", "minor", "major", "fatal"}
ALLOWED_CONFIDENCES = {"HIGH", "MEDIUM", "LOW"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Review a GitLab MR with a configured agent and post the report as an MR comment.")
    parser.add_argument("mr_url", help="GitLab MR URL, for example https://gitlab.example.com/team/project/merge_requests/7")
    args = parser.parse_args(argv)

    try:
        config = load_config()
        result = review_gitlab_mr(args.mr_url, config)
    except Exception as exc:
        token = os.environ.get("GITLAB_TOKEN", "")
        print(f"error: {redact(str(exc), token)}", file=sys.stderr)
        return 1

    print(f"report_path={result['report_path']}")
    print(f"base_sha={result['base_sha']}")
    print(f"head_sha={result['head_sha']}")
    print(f"changed_files={result['changed_files_count']}")
    print(f"review_mode={result['review_mode']}")
    print(f"agent_call_count={result['agent_call_count']}")
    print(f"comment_submitted={str(result['comment_submitted']).lower()}")
    return 0


def load_config() -> Config:
    base_url = os.environ.get("GITLAB_BASE_URL", "").strip()
    token = os.environ.get("GITLAB_TOKEN", "").strip()
    if not base_url:
        raise ValueError("GITLAB_BASE_URL is required")
    if not token:
        raise ValueError("GITLAB_TOKEN is required")

    work_dir = Path(os.environ.get("MR_REVIEW_WORK_DIR") or Path(tempfile.gettempdir()) / "gitlab-mr-review")
    agent_type = os.environ.get("MR_REVIEWER_AGENT_TYPE", "opencode").strip().lower()
    if agent_type not in {"opencode", "claude-code"}:
        raise ValueError(f"unsupported agent type: {agent_type}")
    agent_command = os.environ.get("MR_REVIEWER_AGENT_COMMAND", "").strip()
    if not agent_command:
        agent_command = "opencode" if agent_type == "opencode" else "claude"
    return Config(
        gitlab_base_url=normalize_base_url(base_url),
        gitlab_api_base_url=normalize_base_url(
            os.environ.get("GITLAB_API_BASE_URL", "").strip() or f"{normalize_base_url(base_url)}/api/v4"
        ),
        gitlab_token=token,
        agent_type=agent_type,
        agent_command=agent_command,
        work_dir=work_dir,
        submit_comment=_parse_bool(os.environ.get("MR_REVIEW_SUBMIT_COMMENT", "true")),
    )


def review_gitlab_mr(mr_url: str, config: Config) -> dict[str, object]:
    mr = parse_mr_url(mr_url, config.gitlab_base_url)
    client = GitLabApi(config.gitlab_api_base_url, config.gitlab_token)
    metadata = client.get_json(mr_api_path(mr.project_path, mr.mr_iid))
    base_sha, head_sha = choose_diff_refs(metadata)
    target_repo_url = client.get_project_http_url(int(metadata["target_project_id"]))
    source_repo_url = client.get_project_http_url(int(metadata.get("source_project_id") or metadata["target_project_id"]))

    task_dir = config.work_dir / f"mr-{mr.mr_iid}-{int(time.time())}"
    repo_path = task_dir / "repo"
    report_path = task_dir / "review-report.md"
    task_dir.mkdir(parents=True, exist_ok=True)

    try:
        clone_checkout(
            target_repo_url=target_repo_url,
            source_repo_url=source_repo_url,
            target_branch=metadata["target_branch"],
            source_branch=metadata["source_branch"],
            base_sha=base_sha,
            head_sha=head_sha,
            repo_path=repo_path,
            token=config.gitlab_token,
        )
        changed_files = git_output(["git", "diff", "--name-only", f"{base_sha}...{head_sha}"], repo_path).splitlines()
        routing = resolve_review_routing(metadata.get("title"))
        review_result = run_review(
            config.agent_type,
            config.agent_command,
            mr_url=f"{mr.base_url}/{mr.project_path}/merge_requests/{mr.mr_iid}",
            base_sha=base_sha,
            head_sha=head_sha,
            changed_files=changed_files,
            repo_path=repo_path,
            title=str(metadata.get("title") or ""),
            routing=routing,
        )
        report_path.write_text(str(review_result["local_report"]), encoding="utf-8")
        comment_submitted = False
        if config.submit_comment:
            client.post_form(mr_note_api_path(mr.project_path, mr.mr_iid), {"body": str(review_result["comment_body"])})
            comment_submitted = True
        return {
            "report_path": str(report_path),
            "base_sha": base_sha,
            "head_sha": head_sha,
            "changed_files_count": len(changed_files),
            "comment_submitted": comment_submitted,
            "review_mode": routing.review_mode,
            "routing_reason": routing.routing_reason,
            "routing_marker": routing.routing_marker,
            "agent_call_count": review_result["agent_call_count"],
        }
    except Exception as exc:
        raise RuntimeError(redact(str(exc), config.gitlab_token)) from exc


def parse_mr_url(url: str, base_url: str) -> MrUrl:
    parsed = urllib.parse.urlparse(url)
    base = urllib.parse.urlparse(normalize_base_url(base_url))
    if (parsed.scheme, parsed.netloc.lower()) != (base.scheme, base.netloc.lower()):
        raise ValueError("GitLab host does not match GITLAB_BASE_URL")

    marker = "/merge_requests/"
    if marker not in parsed.path:
        raise ValueError("URL is not a GitLab merge request URL")
    project_part, iid_part = parsed.path.split(marker, 1)
    project_path = urllib.parse.unquote(project_part.strip("/"))
    iid = iid_part.strip("/").split("/", 1)[0]
    if not project_path or not iid.isdigit():
        raise ValueError("GitLab MR URL is missing project path or MR IID")
    return MrUrl(normalize_base_url(base_url), project_path, int(iid))


def normalize_base_url(base_url: str) -> str:
    return base_url.rstrip("/")


def resolve_review_routing(title: object) -> ReviewRoutingDecision:
    normalized = title if isinstance(title, str) else ""
    normalized = normalized.lstrip().casefold()
    for marker in DEEP_REVIEW_MARKERS:
        if normalized.startswith(marker.casefold()):
            return ReviewRoutingDecision("two-step", "title_prefix", marker)
    return ReviewRoutingDecision("one-step", "default", "")


def mr_api_path(project_path: str, mr_iid: int) -> str:
    project = urllib.parse.quote(project_path, safe="")
    return f"/projects/{project}/merge_requests/{mr_iid}"


def mr_note_api_path(project_path: str, mr_iid: int) -> str:
    return f"{mr_api_path(project_path, mr_iid)}/notes"


def choose_diff_refs(metadata: dict[str, object]) -> tuple[str, str]:
    diff_refs = metadata.get("diff_refs")
    if not isinstance(diff_refs, dict):
        diff_refs = {}
    base_sha = diff_refs.get("base_sha") or diff_refs.get("start_sha")
    head_sha = diff_refs.get("head_sha") or metadata.get("sha")
    if not isinstance(base_sha, str) or not isinstance(head_sha, str):
        raise ValueError("GitLab MR response does not include usable diff refs")
    return base_sha, head_sha


def build_review_prompt(
    *,
    mr_url: str,
    base_sha: str,
    head_sha: str,
    changed_files: list[str],
    repo_path: Path,
    review_plan: dict[str, object] | None = None,
) -> str:
    values = {
        "skill_name": "code-review",
        "mr_url": mr_url,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "changed_files": _changed_files(changed_files),
        "repo_path": str(repo_path),
    }
    template_id = "review"
    if review_plan is not None:
        template_id = "deep-review"
        values["review_plan_json"] = json.dumps(review_plan, ensure_ascii=False, indent=2, sort_keys=True)
    return _render_prompt(template_id, values)


def build_review_plan_prompt(
    *,
    mr_url: str,
    base_sha: str,
    head_sha: str,
    changed_files: list[str],
    repo_path: Path,
) -> str:
    return _render_prompt(
        "review-plan",
        {
            "mr_url": mr_url,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "changed_files": _changed_files(changed_files),
            "repo_path": str(repo_path),
        },
    )


def _render_prompt(template_id: str, values: dict[str, str]) -> RenderedPrompt:
    path = Path(__file__).resolve().parents[1] / "prompt_templates" / f"{template_id}.md"
    if not path.is_file():
        raise ValueError(f"prompt template not found: {template_id}")
    content = path.read_text(encoding="utf-8")
    missing = sorted(name for name, value in values.items() if not value)
    if missing:
        raise ValueError(f"missing template values: {', '.join(missing)}")
    try:
        rendered = Template(content).substitute(values)
    except (KeyError, ValueError) as exc:
        raise ValueError(f"invalid {template_id} prompt template: {exc}") from exc
    return RenderedPrompt(rendered, template_id, hashlib.sha256(content.encode("utf-8")).hexdigest()[:12])


def _changed_files(changed_files: list[str]) -> str:
    return "\n".join(f"- {path}" for path in changed_files) or "- <none>"


def parse_review_plan(raw_output: str) -> dict[str, object]:
    return _parse_json_object_output(raw_output, "review_plan", "review plan", _parse_review_plan_object)


def _parse_review_plan_object(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ValueError("review plan output must be a JSON object")
    list_fields = ("change_intent", "external_contracts", "state_invariants", "transaction_async_boundaries", "test_risks", "open_questions")
    expected_fields = {*list_fields, "critical_paths"}
    unexpected_fields = set(payload) - expected_fields
    if unexpected_fields:
        raise ValueError(f"review plan output contains unexpected fields: {sorted(unexpected_fields)}")
    result: dict[str, object] = {}
    for field in list_fields:
        value = payload.get(field)
        if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
            raise ValueError(f"{field} must be a list of non-empty strings")
        result[field] = value
    paths = payload.get("critical_paths")
    if not isinstance(paths, list):
        raise ValueError("critical_paths must be a list")
    parsed_paths = []
    for index, item in enumerate(paths):
        if not isinstance(item, dict) or set(item) != {"path", "reason", "verify"}:
            raise ValueError(f"critical_paths[{index}] must contain only path, reason and verify")
        if not isinstance(item["path"], str) or not item["path"].strip():
            raise ValueError(f"critical_paths[{index}].path must be a non-empty string")
        if not isinstance(item["reason"], str) or not item["reason"].strip():
            raise ValueError(f"critical_paths[{index}].reason must be a non-empty string")
        verify = item["verify"]
        if not isinstance(verify, list) or not verify or not all(isinstance(value, str) and value.strip() for value in verify):
            raise ValueError(f"critical_paths[{index}].verify must be a non-empty list of strings")
        parsed_paths.append(item)
    result["critical_paths"] = parsed_paths
    return result


def parse_structured_review_result(raw_output: str) -> dict[str, object]:
    return _parse_json_object_output(raw_output, "review_result", "review", _parse_structured_review_object)


def _parse_structured_review_object(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ValueError("review output must be a JSON object")
    unexpected_fields = set(payload) - {"findings", "notes", "test_gaps", "good"}
    if unexpected_fields:
        raise ValueError(f"review output contains unexpected fields: {sorted(unexpected_fields)}")
    findings = payload.get("findings")
    if not isinstance(findings, list):
        raise ValueError("findings must be a list")
    for index, finding in enumerate(findings):
        _validate_review_finding(finding, index)
    for field in ("notes", "test_gaps", "good"):
        value = payload.get(field, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"{field} must be a list of strings")
    return payload


def _validate_review_finding(finding: object, index: int) -> None:
    if not isinstance(finding, dict):
        raise ValueError(f"findings[{index}] must be an object")
    severity = _review_text(finding, "severity", index)
    confidence = _review_text(finding, "confidence", index)
    if severity not in ALLOWED_SEVERITIES:
        raise ValueError(f"findings[{index}].severity must be one of {sorted(ALLOWED_SEVERITIES)}")
    if confidence not in ALLOWED_CONFIDENCES:
        raise ValueError(f"findings[{index}].confidence must be one of {sorted(ALLOWED_CONFIDENCES)}")
    for field in ("rule_id", "old_path", "new_path", "title", "evidence", "impact", "suggestion"):
        _review_text(finding, field, index)
    for field in ("old_line", "new_line"):
        if not isinstance(finding.get(field), int):
            raise ValueError(f"findings[{index}].{field} must be an integer")


def _review_text(finding: dict, field: str, index: int) -> str:
    value = finding.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"findings[{index}].{field} must be a non-empty string")
    return value


def _parse_json_object_output(raw_output: str, output_type: str, error_label: str, parse_object):
    """便携 skill 自包含运行，需在自身边界重复主程序的安全恢复策略。"""
    try:
        return parse_object(json.loads(raw_output))
    except json.JSONDecodeError as strict_error:
        decoder = json.JSONDecoder()
        decoded_candidates = []
        valid_candidates = []
        validation_errors = []
        for start, character in enumerate(raw_output):
            if character != "{":
                continue
            try:
                payload, end = decoder.raw_decode(raw_output, start)
            except json.JSONDecodeError:
                continue
            decoded_candidates.append((start, end, payload))

        outermost_candidates = []
        for candidate in sorted(decoded_candidates, key=lambda item: (item[0], -item[1])):
            start, end, _ = candidate
            if any(parent_start <= start and end <= parent_end for parent_start, parent_end, _ in outermost_candidates):
                continue
            outermost_candidates.append(candidate)
        decoded_candidates = outermost_candidates
        for start, end, payload in decoded_candidates:
            try:
                parsed = parse_object(payload)
            except ValueError as exc:
                validation_errors.append(exc)
                continue
            valid_candidates.append((start, end, parsed))

        if len(valid_candidates) > 1:
            raise ValueError(f"{error_label} output contains multiple valid JSON objects") from strict_error
        if len(valid_candidates) == 1:
            start, end, parsed = valid_candidates[0]
            print(
                "warning: stage=structured_output_normalize "
                f"output={output_type} status=recovered prefix_chars={start} "
                f"suffix_chars={len(raw_output) - end} candidate_count={len(decoded_candidates)}",
                file=sys.stderr,
            )
            return parsed
        if len(decoded_candidates) == 1 and len(validation_errors) == 1:
            raise validation_errors[0] from strict_error
        if decoded_candidates:
            raise ValueError(
                f"{error_label} output does not contain a valid JSON object matching the required contract"
            ) from strict_error
        raise ValueError(f"{error_label} output must be valid JSON: {strict_error}") from strict_error


def run_two_step_review(
    agent_type: str,
    command: str,
    mr_url: str,
    base_sha: str,
    head_sha: str,
    changed_files: list[str],
    repo_path: Path,
) -> dict[str, object]:
    plan_raw = run_agent_review(
        agent_type,
        command,
        build_review_plan_prompt(
            mr_url=mr_url,
            base_sha=base_sha,
            head_sha=head_sha,
            changed_files=changed_files,
            repo_path=repo_path,
        ),
        repo_path,
    )
    review_plan = parse_review_plan(plan_raw)
    comment_body = run_agent_review(
        agent_type,
        command,
        build_review_prompt(
            mr_url=mr_url,
            base_sha=base_sha,
            head_sha=head_sha,
            changed_files=changed_files,
            repo_path=repo_path,
            review_plan=review_plan,
        ),
        repo_path,
    )
    comment_body = json.dumps(parse_structured_review_result(comment_body), ensure_ascii=False, separators=(",", ":"))
    return {
        "summary": None,
        "review_plan": review_plan,
        "comment_body": comment_body,
        "agent_call_count": 2,
        "local_report": render_local_report(review_plan, comment_body, base_sha, head_sha),
    }


def run_review(
    agent_type: str,
    command: str,
    mr_url: str,
    base_sha: str,
    head_sha: str,
    changed_files: list[str],
    repo_path: Path,
    title: str,
    routing: ReviewRoutingDecision,
) -> dict[str, object]:
    if routing.review_mode == "two-step":
        result = run_two_step_review(agent_type, command, mr_url, base_sha, head_sha, changed_files, repo_path)
    else:
        comment_body = run_agent_review(
            agent_type,
            command,
            build_review_prompt(
                mr_url=mr_url,
                base_sha=base_sha,
                head_sha=head_sha,
                changed_files=changed_files,
                repo_path=repo_path,
            ),
            repo_path,
        )
        comment_body = json.dumps(parse_structured_review_result(comment_body), ensure_ascii=False, separators=(",", ":"))
        result = {"summary": None, "review_plan": None, "comment_body": comment_body, "agent_call_count": 1}
    result["local_report"] = render_local_report(
        result.get("review_plan"),
        str(result["comment_body"]),
        base_sha,
        head_sha,
        title=title,
        routing=routing,
        changed_files=changed_files,
    )
    return result


def render_local_report(
    review_plan: dict[str, object] | None,
    review: str,
    base_sha: str,
    head_sha: str,
    *,
    title: str = "",
    routing: ReviewRoutingDecision | None = None,
    changed_files: list[str] | None = None,
) -> str:
    """Skill 独立运行时也使用与自动入口一致的本地报告骨架。"""
    structured = parse_structured_review_result(review)
    findings = structured.get("findings", [])
    good = structured.get("good", [])
    notes = structured.get("notes", [])
    test_gaps = structured.get("test_gaps", [])
    routing = routing or ReviewRoutingDecision("two-step", "title_prefix", DEEP_REVIEW_MARKER)
    changed_files = changed_files or []
    lines = [
        "# 代码检视报告", "", "## Discoveries", "",
        f"- MR title：{title or '<empty>'}",
        f"- 审查模式：{routing.review_mode}（{routing.routing_reason}）",
        f"- 审查范围：Base SHA = {base_sha}，Head SHA = {head_sha}",
        f"- 变更文件数：{len(changed_files)}",
    ]
    lines.extend(f"  - {path}" for path in changed_files)
    if review_plan is not None:
        lines.append("- 审查计划：")
        for field in ("change_intent", "external_contracts", "state_invariants", "transaction_async_boundaries", "test_risks", "open_questions"):
            values = review_plan.get(field, [])
            lines.append(f"  - {field}：{'；'.join(str(value) for value in values) if values else '无'}")
        for path in review_plan.get("critical_paths", []):
            lines.append(f"  - critical_path：{path['path']} — {path['reason']}；验证：{'；'.join(path['verify'])}")
    if notes:
        lines.append(f"- 检视备注：{'；'.join(notes)}")
    if test_gaps:
        lines.append(f"- 测试缺口：{'；'.join(test_gaps)}")
    lines.extend(["", "## 检视意见", ""])
    if not findings:
        lines.append("- 未发现可报告的问题。")
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        path = finding.get("new_path") or finding.get("old_path") or "<unknown>"
        line = finding.get("new_line") if finding.get("new_line", -1) != -1 else finding.get("old_line", "<unknown>")
        lines.extend([
            f"### [{finding.get('severity', 'suggestion')}] {finding.get('title', '<unknown>')}", "",
            f"**文件**: {path}:{line}", "", f"**证据**: {finding.get('evidence', '')}", "",
            f"**影响**: {finding.get('impact', '')}", "", "**MR评论状态**：已提交MR comment（仅 skill 模式）", "",
            f"**建议**: {finding.get('suggestion', '')}", "",
        ])
    counts = {severity: sum(1 for item in findings if isinstance(item, dict) and item.get("severity") == severity)
              for severity in ("fatal", "major", "minor", "suggestion")}
    lines.extend(["## 检视摘要", "", "| 严重程度 | 数量 | 状态 |", "|----------|------|------|"])
    for severity in ("fatal", "major", "minor", "suggestion"):
        state = "通过" if not counts[severity] else {"fatal": "阻止", "major": "警告", "minor": "警告", "suggestion": "备注"}[severity]
        lines.append(f"| {severity} | {counts[severity]} | {state} |")
    verdict = "阻止" if counts["fatal"] else "警告" if counts["major"] or counts["minor"] else "备注" if counts["suggestion"] else "通过"
    lines.extend(["", f"**裁决**：{verdict}"])
    if good:
        lines.extend(["", "## GOOD", ""])
        lines.extend(f"- {item}" for item in good)
    return "\n".join(lines) + "\n"


def clone_checkout(
    *,
    target_repo_url: str,
    source_repo_url: str,
    target_branch: str,
    source_branch: str,
    base_sha: str,
    head_sha: str,
    repo_path: Path,
    token: str,
) -> None:
    env = git_env(token)
    work_dir = repo_path.parent
    # token 通过 Git extraHeader 进入环境，避免出现在命令行和日志里。
    git_run(["git", "-c", "credential.helper=", "clone", "--no-checkout", target_repo_url, str(repo_path)], work_dir, env, token)
    source_remote = "origin"
    if source_repo_url != target_repo_url:
        git_run(["git", "remote", "add", "source", source_repo_url], repo_path, env, token)
        source_remote = "source"
    git_run(["git", "fetch", "origin", target_branch], repo_path, env, token)
    git_run(["git", "fetch", source_remote, source_branch], repo_path, env, token)
    git_run(["git", "checkout", head_sha], repo_path, env, token)
    git_run(["git", "cat-file", "-e", f"{base_sha}^{{commit}}"], repo_path, env, token)


def git_env(token: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic_auth_token(token)}",
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
        }
    )
    return env


def basic_auth_token(token: str) -> str:
    return base64.b64encode(f"oauth2:{token}".encode("utf-8")).decode("ascii")


def git_run(args: list[str], cwd: Path, env: dict[str, str], token: str) -> None:
    git_output(args, cwd, env=env, token=token)


def git_output(args: list[str], cwd: Path, env: dict[str, str] | None = None, token: str = "") -> str:
    result = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git command failed: {redact(result.stderr.strip(), token)}")
    return result.stdout


def run_agent_review(agent_type: str, command: str, prompt: str, repo_path: Path) -> str:
    args = shlex.split(command, posix=(os.name != "nt"))
    prompt_file: Path | None = None
    try:
        if agent_type == "opencode":
            with tempfile.NamedTemporaryFile(
                    mode="w",
                    prefix="mr-review-agent-prompt-",
                    suffix=".md",
                    delete=False,
                    encoding="utf-8",
            ) as file:
                file.write(prompt)
                prompt_file = Path(file.name)
            args += ["run", "Follow the instructions in the attached file.", "--file", str(prompt_file)]
            input_text = None
        elif agent_type == "claude-code":
            args += ["-p", "--output-format", "text"]
            input_text = prompt
        else:
            raise ValueError(f"unsupported agent type: {agent_type}")

        result = subprocess.run(
            prepare_command(args),
            cwd=repo_path,
            input=input_text,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"agent run failed: {result.stderr.strip()}")
        return result.stdout.strip()
    finally:
        if prompt_file:
            prompt_file.unlink(missing_ok=True)


def prepare_command(args: list[str]) -> list[str]:
    if os.name != "nt" or not args:
        return args
    executable = shutil.which(args[0])
    if not executable:
        return args
    if Path(executable).suffix.lower() not in {".bat", ".cmd"}:
        return args
    # Windows CreateProcess 直接运行批处理文件不稳定，call 能兼容带空格路径。
    return ["cmd.exe", "/d", "/c", "call", executable, *args[1:]]


class GitLabApi:
    def __init__(self, base_url: str, token: str):
        self.base_url = normalize_base_url(base_url)
        self.token = token

    def get_json(self, path: str) -> dict[str, object]:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            headers={"PRIVATE-TOKEN": self.token, "Accept": "application/json"},
        )
        return self._open_json(request)

    def get_project_http_url(self, project_id: int) -> str:
        project = self.get_json(f"/projects/{project_id}")
        repo_url = project.get("http_url_to_repo")
        if not isinstance(repo_url, str) or not repo_url:
            raise ValueError("GitLab project response does not include http_url_to_repo")
        return repo_url

    def post_form(self, path: str, fields: dict[str, str]) -> dict[str, object]:
        body = urllib.parse.urlencode(fields).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            method="POST",
            headers={
                "PRIVATE-TOKEN": self.token,
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            },
        )
        return self._open_json(request)

    def _open_json(self, request: urllib.request.Request) -> dict[str, object]:
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"GitLab API request failed: HTTP {exc.code}") from exc


def redact(text: str, token: str) -> str:
    if token:
        text = text.replace(token, "<redacted>")
        text = text.replace(basic_auth_token(token), "<redacted>")
    return text


def _parse_bool(value: str) -> bool:
    return value.strip().lower() not in {"0", "false", "no", "off"}


if __name__ == "__main__":
    raise SystemExit(main())
