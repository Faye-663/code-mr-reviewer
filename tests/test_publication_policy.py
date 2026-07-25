from pathlib import Path

import pytest

from mr_reviewer.config import Config
from mr_reviewer.publication_policy import FindingPublicationPolicy


def test_default_publication_policy_uses_minor_and_high_thresholds():
    policy = FindingPublicationPolicy()

    assert policy.min_severity == "minor"
    assert policy.min_confidence == "HIGH"
    assert policy.filter_reason("minor", "HIGH") == ""
    assert policy.filter_reason("major", "HIGH") == ""
    assert policy.filter_reason("fatal", "HIGH") == ""
    assert policy.filter_reason("suggestion", "HIGH") == "below_min_severity"
    assert policy.filter_reason("minor", "MEDIUM") == "below_min_confidence"


def test_custom_publication_policy_applies_both_ordered_thresholds():
    policy = FindingPublicationPolicy(min_severity="suggestion", min_confidence="MEDIUM")

    assert policy.filter_reason("suggestion", "MEDIUM") == ""
    assert policy.filter_reason("suggestion", "HIGH") == ""
    assert policy.filter_reason("suggestion", "LOW") == "below_min_confidence"


@pytest.mark.parametrize(
    ("severity", "confidence", "message"),
    [
        (("min" + "jor").upper(), "HIGH", "unsupported publish minimum severity"),
        ("minor", "high", "unsupported publish minimum confidence"),
    ],
)
def test_config_rejects_invalid_publication_thresholds(
        tmp_path: Path,
        monkeypatch,
        severity: str,
        confidence: str,
        message: str,
):
    monkeypatch.delenv("MR_REVIEWER_PUBLISH_MIN_SEVERITY", raising=False)
    monkeypatch.delenv("MR_REVIEWER_PUBLISH_MIN_CONFIDENCE", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MR_REVIEWER_GITLAB_BASE_URL=https://gitlab.example.com\n"
        f"MR_REVIEWER_PUBLISH_MIN_SEVERITY={severity}\n"
        f"MR_REVIEWER_PUBLISH_MIN_CONFIDENCE={confidence}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=message):
        Config.from_env(env_file)


def test_config_reads_publication_thresholds(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MR_REVIEWER_PUBLISH_MIN_SEVERITY", raising=False)
    monkeypatch.delenv("MR_REVIEWER_PUBLISH_MIN_CONFIDENCE", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MR_REVIEWER_GITLAB_BASE_URL=https://gitlab.example.com\n"
        "MR_REVIEWER_PUBLISH_MIN_SEVERITY=suggestion\n"
        "MR_REVIEWER_PUBLISH_MIN_CONFIDENCE=MEDIUM\n",
        encoding="utf-8",
    )

    config = Config.from_env(env_file)

    assert config.publish_min_severity == "suggestion"
    assert config.publish_min_confidence == "MEDIUM"
    assert config.publication_policy == FindingPublicationPolicy("suggestion", "MEDIUM")
