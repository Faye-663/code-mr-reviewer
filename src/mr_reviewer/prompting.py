from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from string import Template


class PromptTemplateError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PromptMetadata:
    template_id: str
    template_version: str


class RenderedPrompt(str):
    """保留字符串兼容性，同时携带可审计的模板元数据。"""

    template_id: str
    template_version: str

    def __new__(cls, content: str, template_id: str, template_version: str):
        value = super().__new__(cls, content)
        value.template_id = template_id
        value.template_version = template_version
        return value

    @property
    def content(self) -> str:
        return str(self)

    @property
    def metadata(self) -> PromptMetadata:
        return PromptMetadata(self.template_id, self.template_version)


def build_summary_prompt(
        *, mr_url: str, base_sha: str, head_sha: str, changed_files: list[str], repo_path: Path | None
) -> RenderedPrompt:
    return _render(
        "summary",
        {
            "mr_url": mr_url,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "changed_files": _changed_files(changed_files),
            "repo_path": str(repo_path) if repo_path is not None else None,
        },
    )


def build_review_prompt(
        *,
        skill_name: str,
        mr_url: str,
        base_sha: str,
        head_sha: str,
        changed_files: list[str],
        repo_path: Path | None,
        summary: dict[str, object],
) -> RenderedPrompt:
    return _render(
        "review",
        {
            "skill_name": skill_name,
            "mr_url": mr_url,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "changed_files": _changed_files(changed_files),
            "repo_path": str(repo_path) if repo_path is not None else None,
            "summary_json": json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True),
        },
    )


def _render(template_id: str, values: dict[str, str | None]) -> RenderedPrompt:
    content = _load_template(template_id)
    missing = sorted(name for name, value in values.items() if value is None or value == "")
    if missing:
        raise PromptTemplateError(f"missing template values: {', '.join(missing)}")
    try:
        rendered = Template(content).substitute(values)
    except (KeyError, ValueError) as exc:
        raise PromptTemplateError(f"invalid {template_id} prompt template: {exc}") from exc
    return RenderedPrompt(rendered, template_id, _template_version(content))


def _load_template(template_id: str) -> str:
    path = files("mr_reviewer").joinpath("prompt_templates", f"{template_id}.md")
    if not path.is_file():
        raise PromptTemplateError(f"prompt template not found: {template_id}")
    return path.read_text(encoding="utf-8")


def _changed_files(changed_files: list[str]) -> str:
    return "\n".join(f"- {path}" for path in changed_files) or "- <none>"


def _template_version(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
