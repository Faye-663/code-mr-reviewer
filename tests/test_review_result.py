import logging

import pytest

import mr_reviewer.review_result as review_result_module
from mr_reviewer.markdown_report import (
    format_comment_status,
    render_review_report,
    render_structured_output_as_markdown,
)
from mr_reviewer.review_result import StructuredReviewParseError, parse_structured_review_result
from mr_reviewer.reviewer import ReviewReport


def _valid_review_plan() -> str:
    return (
        '{"change_intent":["support $HOME token refresh"],'
        '"critical_paths":[{"path":"auth/service.py","reason":"token lifecycle",'
        '"verify":["refresh remains atomic"]}],"external_contracts":["HTTP response"],'
        '"state_invariants":["one active token"],"transaction_async_boundaries":["DB commit before event"],'
        '"test_risks":["concurrent refresh"],"open_questions":[]}'
    )


def test_parse_review_plan_accepts_strict_contract_and_dollar_text():
    plan = review_result_module.parse_review_plan(_valid_review_plan())

    assert plan["change_intent"] == ["support $HOME token refresh"]
    assert plan["critical_paths"][0]["verify"] == ["refresh remains atomic"]


def test_parse_review_plan_recovers_single_contract_object_from_wrapped_output(caplog):
    raw = f"我将按要求生成审查计划。\n```json\n{_valid_review_plan()}\n```\n生成完成。"

    with caplog.at_level(logging.WARNING, logger="mr_reviewer"):
        plan = review_result_module.parse_review_plan(raw)

    assert plan["change_intent"] == ["support $HOME token refresh"]
    assert "output=review_plan" in caplog.text
    assert "status=recovered" in caplog.text
    assert "prefix_chars=" in caplog.text
    assert "suffix_chars=" in caplog.text
    assert "candidate_count=" in caplog.text
    assert "我将按要求" not in caplog.text


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ('{"change_intent":[]}', "external_contracts"),
        (_valid_review_plan()[:-1] + ',"unknown":[]} ', "unexpected fields"),
        (_valid_review_plan().replace('"test_risks":["concurrent refresh"]', '"test_risks":"x"'), "test_risks"),
        (_valid_review_plan().replace('"verify":["refresh remains atomic"]', '"verify":[]'), "verify must not be empty"),
        (_valid_review_plan().replace('"path":"auth/service.py"', '"path":""'), "path must be a non-empty string"),
    ],
)
def test_parse_review_plan_rejects_invalid_contract(raw: str, message: str):
    with pytest.raises(review_result_module.ReviewPlanParseError, match=message):
        review_result_module.parse_review_plan(raw)


def test_parse_structured_review_result_accepts_valid_findings():
    result = parse_structured_review_result(
        """
        {
          "findings": [
            {
              "rule_id": "SQL_PERFORMANCE",
              "severity": "major",
              "confidence": "HIGH",
              "old_path": "src/example.py",
              "new_path": "src/example.py",
              "old_line": -1,
              "new_line": 42,
              "title": "批量查询缺少数量限制",
              "evidence": "本次变更新增 IN 查询，但未限制集合大小。",
              "impact": "大请求可能导致数据库资源耗尽。",
              "suggestion": "限制集合大小或拆批查询。"
            }
          ],
          "notes": ["只记录到本地报告"],
          "test_gaps": ["缺少边界测试"]
        }
        """
    )

    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.rule_id == "SQL_PERFORMANCE"
    assert finding.severity == "major"
    assert finding.confidence == "HIGH"
    assert finding.old_path == "src/example.py"
    assert finding.new_path == "src/example.py"
    assert finding.old_line == -1
    assert finding.new_line == 42
    assert finding.title == "批量查询缺少数量限制"
    assert result.notes == ["只记录到本地报告"]
    assert result.test_gaps == ["缺少边界测试"]


def test_parse_structured_review_result_accepts_minor_severity():
    result = parse_structured_review_result(_structured_payload(severity="minor"))

    assert result.findings[0].severity == "minor"


@pytest.mark.parametrize(
    ("prefix", "suffix"),
    [
        ("我将按要求进行 review。\n", ""),
        ("", "\nreview 完成。"),
        ("```json\n", "\n```"),
        ("说明中的无效花括号 {not-json}\n", ""),
    ],
)
def test_parse_structured_review_result_recovers_single_contract_object(prefix: str, suffix: str):
    raw = prefix + _structured_payload() + suffix

    result = parse_structured_review_result(raw)

    assert result.findings[0].rule_id == "SQL_PERFORMANCE"


def test_parse_structured_review_result_accepts_only_contract_valid_candidate():
    raw = '{"message":"metadata"}\n' + _structured_payload()

    result = parse_structured_review_result(raw)

    assert result.findings[0].rule_id == "SQL_PERFORMANCE"


def test_parse_structured_review_result_rejects_multiple_contract_valid_candidates():
    raw = _structured_payload() + "\n" + _structured_payload()

    with pytest.raises(StructuredReviewParseError, match="multiple valid JSON objects"):
        parse_structured_review_result(raw)


def test_parse_structured_review_result_rejects_wrapped_invalid_contract():
    raw = "review result:\n" + _structured_payload(severity="BLOCKER")

    with pytest.raises(StructuredReviewParseError, match="severity"):
        parse_structured_review_result(raw)


def test_parse_structured_review_result_rejects_wrapped_truncated_json():
    raw = "review result:\n" + _structured_payload().rstrip()[:-1]

    with pytest.raises(StructuredReviewParseError):
        parse_structured_review_result(raw)


def test_parse_structured_review_result_preserves_braces_inside_json_strings():
    raw = "review result:\n" + _structured_payload(impact="错误会污染 {cache} 状态")

    result = parse_structured_review_result(raw)

    assert result.findings[0].impact == "错误会污染 {cache} 状态"


def test_parse_structured_review_result_does_not_log_recovery_for_strict_json(caplog):
    with caplog.at_level(logging.WARNING, logger="mr_reviewer"):
        parse_structured_review_result(_structured_payload())

    assert "status=recovered" not in caplog.text


def test_parse_structured_review_result_rejects_invalid_json():
    with pytest.raises(StructuredReviewParseError, match="valid JSON"):
        parse_structured_review_result("not json")


def test_parse_structured_review_result_requires_finding_fields():
    with pytest.raises(StructuredReviewParseError, match="old_path"):
        parse_structured_review_result(
            """
            {
              "findings": [
                {
                  "rule_id": "SQL_PERFORMANCE",
                  "severity": "major",
                  "confidence": "HIGH",
                  "new_path": "src/example.py",
                  "old_line": -1,
                  "new_line": 42,
                  "title": "批量查询缺少数量限制",
                  "evidence": "证据",
                  "suggestion": "建议"
                }
              ],
              "notes": [],
              "test_gaps": []
            }
            """
        )


def test_parse_structured_review_result_requires_impact_and_accepts_good():
    with pytest.raises(StructuredReviewParseError, match="impact"):
        parse_structured_review_result(_structured_payload().replace(',\n          "impact": "缺陷会导致业务失败"', ""))

    result = parse_structured_review_result(
        _structured_payload(extra=', "good": ["事务边界下沉到领域服务"]', impact="令牌会进入 HTTP 响应")
    )

    assert result.findings[0].impact == "令牌会进入 HTTP 响应"
    assert result.good == ["事务边界下沉到领域服务"]


@pytest.mark.parametrize("severity", ["BLOCKER", "min" + "jor", ""])
def test_parse_structured_review_result_rejects_unknown_severity(severity):
    payload = _structured_payload(severity=severity)

    with pytest.raises(StructuredReviewParseError, match="severity"):
        parse_structured_review_result(payload)


@pytest.mark.parametrize("confidence", ["CRITICAL", "high", ""])
def test_parse_structured_review_result_rejects_unknown_confidence(confidence):
    payload = _structured_payload(confidence=confidence)

    with pytest.raises(StructuredReviewParseError, match="confidence"):
        parse_structured_review_result(payload)


def test_render_structured_output_as_markdown_uses_python_renderer():
    report = ReviewReport(
        markdown=_structured_payload(),
        review_plan={
            "change_intent": ["修复认证流程"],
            "critical_paths": [{"path": "auth", "reason": "刷新token", "verify": ["并发刷新"]}],
            "external_contracts": [],
            "state_invariants": [],
            "transaction_async_boundaries": [],
            "test_risks": [],
            "open_questions": [],
        },
        repo="team/project",
        mr_iid=7,
        mr_url="https://gitlab.example.com/team/project/merge_requests/7",
        source_branch="feature/auth",
        target_branch="main",
        base_sha="base-sha",
        head_sha="head-sha",
    )

    rendered = render_structured_output_as_markdown(report)

    assert rendered.structured_parse_status == "success"
    assert rendered.finding_counts["total"] == 1
    assert rendered.finding_counts["monitor_only"] == 1
    assert rendered.markdown.startswith("# 代码检视报告")
    assert "team/project!7" in rendered.markdown
    assert "批量查询缺少数量限制" in rendered.markdown
    assert "未提交（当前入口仅生成本地报告）" in rendered.markdown
    assert "## Discoveries" in rendered.markdown
    assert "修复认证流程" in rendered.markdown


def test_render_structured_output_as_markdown_counts_minor_severity():
    report = ReviewReport(markdown=_structured_payload(severity="minor"))

    rendered = render_structured_output_as_markdown(report)

    assert "### [minor]" in rendered.markdown
    assert "| minor | 1 | 警告 |" in rendered.markdown
    assert "major/minor" in rendered.markdown
    assert ("min" + "jor") not in rendered.markdown


@pytest.mark.parametrize(
    ("status", "reason", "expected"),
    [
        ("posted", "", "已提交 MR 行内评论"),
        ("skipped_duplicate", "duplicate_marker", "未重复提交（相同 MR 评论已存在）"),
        ("monitor_only", "not_published_for_entry", "未提交（当前入口仅生成本地报告）"),
        ("disabled", "post_comment_disabled", "未提交（GitLab 评论发布未启用）"),
        ("model_not_configured", "agent_model_name_missing", "未提交（Agent 模型名未配置）"),
        ("skipped_stale", "mr_head_changed", "未提交（MR 版本已变化）"),
        ("filtered", "below_min_severity", "未提交（严重程度低于当前发布门槛）"),
        ("filtered", "below_min_confidence", "未提交（置信度低于当前发布门槛）"),
        ("invalid", "line_not_in_diff", "未提交（位置不在当前 MR diff）"),
        ("invalid", "inconsistent_line_sides", "未提交（diff 行号两侧不一致）"),
        ("invalid", "invalid_line_value", "未提交（评论行号无效）"),
        ("failed", "secret raw exception", "提交失败（详情见任务日志）"),
        ("unexpected", "secret raw exception", "未提交（原因未知）"),
    ],
)
def test_review_report_renders_safe_comment_status_reason(status, reason, expected):
    finding = {
        "severity": "major",
        "title": "问题",
        "new_path": "app.py",
        "new_line": 1,
        "evidence": "证据",
        "impact": "影响",
        "suggestion": "建议",
        "status": status,
        "reason": reason,
    }

    markdown = render_review_report(ReviewReport(markdown="{}", finding_results=[finding]), "success")

    assert f"**MR评论状态**：{expected}" in markdown
    assert "filtered" not in markdown
    assert "secret raw exception" not in markdown


@pytest.mark.parametrize(
    ("status", "reason", "expected"),
    [
        ("posted_inline", "", "已提交 MR 行内评论"),
        ("posted_note", "position_not_provided", "已提交普通 MR 评论（未提供 diff 位置）"),
        ("posted_note", "position_not_in_diff", "已提交普通 MR 评论（位置不在当前 diff）"),
        ("disabled", "review_set_post_comment_disabled", "未提交（GitLab 评论发布未启用）"),
        ("invalid", "invalid_target_line", "未提交（评论行号无效）"),
        ("invalid", "invalid_target_path", "未提交（评论路径无效）"),
        ("invalid", "invalid_evidence_path", "未提交（证据路径无效）"),
        ("invalid", "unknown_target_member", "未提交（责任 MR 不属于当前 ReviewSet）"),
        ("invalid", "unknown_evidence_member", "未提交（证据 MR 不属于当前 ReviewSet）"),
        ("failed", "duplicate_check_failed", "提交失败（详情见任务日志）"),
        ("failed", "gitlab_publish_failed", "提交失败（详情见任务日志）"),
    ],
)
def test_comment_status_formatter_covers_review_set_results(status, reason, expected):
    assert format_comment_status(status, reason) == expected


def _structured_payload(
        severity: str = "major", confidence: str = "HIGH", impact: str = "缺陷会导致业务失败", extra: str = ""
) -> str:
    return f"""
    {{
      "findings": [
        {{
          "rule_id": "SQL_PERFORMANCE",
          "severity": "{severity}",
          "confidence": "{confidence}",
          "old_path": "src/example.py",
          "new_path": "src/example.py",
          "old_line": -1,
          "new_line": 42,
          "title": "批量查询缺少数量限制",
          "evidence": "证据",
          "impact": "{impact}",
          "suggestion": "建议"
        }}
      ],
      "notes": [],
      "test_gaps": []{extra}
    }}
    """
