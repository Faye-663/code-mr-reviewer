import pytest

from mr_reviewer.markdown_report import render_structured_output_as_markdown
from mr_reviewer.review_result import StructuredReviewParseError, parse_structured_review_result
from mr_reviewer.reviewer import ReviewReport


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


@pytest.mark.parametrize("severity", ["BLOCKER", "minor", ""])
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
    assert rendered.markdown.startswith("# GitLab MR Review Report")
    assert "team/project!7" in rendered.markdown
    assert "SQL_PERFORMANCE" in rendered.markdown
    assert "monitor_only" in rendered.markdown


def _structured_payload(severity: str = "major", confidence: str = "HIGH") -> str:
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
          "suggestion": "建议"
        }}
      ],
      "notes": [],
      "test_gaps": []
    }}
    """
