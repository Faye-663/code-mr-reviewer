import pytest

from mr_reviewer.review_routing import DEEP_REVIEW_MARKER, resolve_review_routing


@pytest.mark.parametrize(
    "title",
    [
        "",
        "Fix auth",
        "prefix 【Deep-Review】 change",
        "prefix [Deep-Review] change",
        "【Deep Review】 change",
        "【Deep-Review] change",
        "[Deep-Review】 change",
    ],
)
def test_review_routing_defaults_to_one_step(title: str):
    decision = resolve_review_routing(title)

    assert decision.review_mode == "one-step"
    assert decision.routing_reason == "default"
    assert decision.routing_marker == ""


@pytest.mark.parametrize(
    ("title", "expected_marker"),
    [
        ("【Deep-Review】 change", "【Deep-Review】"),
        ("  【deep-review】 change", "【Deep-Review】"),
        ("\t【DEEP-REVIEW】 change", "【Deep-Review】"),
        ("[Deep-Review] change", "[Deep-Review]"),
        ("  [deep-review] change", "[Deep-Review]"),
        ("\t[DEEP-REVIEW] change", "[Deep-Review]"),
    ],
)
def test_review_routing_uses_case_insensitive_title_prefix(title: str, expected_marker: str):
    decision = resolve_review_routing(title)

    assert decision.review_mode == "two-step"
    assert decision.routing_reason == "title_prefix"
    assert decision.routing_marker == expected_marker
