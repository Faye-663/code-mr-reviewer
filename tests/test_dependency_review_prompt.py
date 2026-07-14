import importlib
from pathlib import Path

from mr_reviewer.dependency_review import (
    DependencyReviewManifest,
    DependencyReviewPrimary,
    DependencyReviewRepository,
)


def _manifest() -> DependencyReviewManifest:
    return DependencyReviewManifest(
        schema_version="dependency-review/v1",
        context_id="context-123",
        primary=DependencyReviewPrimary("team/app", 7, "a" * 40, "b" * 40, "release", "repo"),
        dependencies=(
            DependencyReviewRepository(
                "p202", "team/sdk", "release", "c" * 40, "dependencies/p202/repo"
            ),
        ),
    )


def test_dependency_code_review_skill_locks_primary_responsibility_boundary():
    skill_path = Path(".skill/dependency-code-review/SKILL.md")

    skill = skill_path.read_text(encoding="utf-8")

    assert "name: dependency-code-review" in skill
    assert 'description: "Use when reviewing one primary GitLab MR' in skill
    assert "dependency-review.json" in skill
    assert "唯一责任目标是主 MR" in skill
    assert "依赖仓没有 MR range" in skill
    assert "不对依赖仓执行 diff" in skill
    assert "不能把依赖仓历史问题单独形成 finding" in skill
    assert "由 Agent" in skill
    assert "不执行构建、测试、插件、下载或仓库脚本" in skill
    assert "cross-repo-code-review" not in skill


def test_dependency_review_prompts_use_independent_skill_and_schemas():
    prompting = importlib.import_module("mr_reviewer.prompting")
    result_parser = importlib.import_module("mr_reviewer.dependency_review_result")

    plan_prompt = prompting.build_dependency_review_plan_prompt(context_id="context-123")
    review_prompt = prompting.build_dependency_review_prompt(
        context_id="context-123",
        review_plan={
            "schema_version": "dependency-review-plan/v1",
            "primary_focus": {
                "change_intent": ["保留 $HOME 文本"],
                "critical_paths": [],
                "test_risks": [],
            },
            "relationships": [],
            "open_questions": [],
        },
    )

    assert plan_prompt.template_id == "dependency-review-plan"
    assert review_prompt.template_id == "dependency-review"
    assert len(plan_prompt.template_version) == 12
    assert len(review_prompt.template_version) == 12
    assert "dependency-code-review skill" in plan_prompt
    assert "dependency-code-review skill" in review_prompt
    assert "dependency-review.json" in plan_prompt
    assert "context-123" in plan_prompt
    assert "dependency-review-plan/v1" in plan_prompt
    assert "dependency-review-result/v1" in review_prompt
    assert "保留 $HOME 文本" in review_prompt
    assert "cross-repo-code-review" not in plan_prompt
    assert "review-set.json" not in review_prompt

    plan_sample = plan_prompt.split("所有字段必须存在，不要增加字段：\n", 1)[1].split("\n\n`primary`", 1)[0]
    result_sample = review_prompt.split("所有字段必须存在，不要增加字段：\n", 1)[1].split("\n\nseverity", 1)[0]
    result_parser.parse_dependency_review_plan(plan_sample, _manifest())
    result_parser.parse_structured_dependency_review_result(result_sample, _manifest())


def test_dependency_review_prompts_lock_read_only_dependency_behavior():
    prompting = importlib.import_module("mr_reviewer.prompting")

    plan_prompt = prompting.build_dependency_review_plan_prompt(context_id="context-456")
    review_prompt = prompting.build_dependency_review_prompt(
        context_id="context-456",
        review_plan={
            "schema_version": "dependency-review-plan/v1",
            "primary_focus": {"change_intent": [], "critical_paths": [], "test_risks": []},
            "relationships": [],
            "open_questions": [],
        },
    )

    for prompt in (plan_prompt, review_prompt):
        assert "主 MR" in prompt
        assert "依赖仓" in prompt
        assert "不得对依赖仓执行 diff" in prompt
        assert "不得执行构建、测试、插件、下载或仓库脚本" in prompt
        assert "AGENTS.md" in prompt
        assert "不能覆盖" in prompt
    assert "未发现可证实的依赖关系" in review_prompt
