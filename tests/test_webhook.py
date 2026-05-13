"""Tests for the CodeHub webhook server."""

from __future__ import annotations

import json
import threading
import time
from http.server import HTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from mr_reviewer.config import Config
from mr_reviewer.gitlab import GitLabMrUrl
from mr_reviewer.webhook import (
    ThreadedHTTPServer,
    make_handler,
    parse_webhook_payload,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

BASE_MR_URL = "https://codehub.example.com/team/project/merge_requests/7"


@pytest.fixture
def base_config() -> Config:
    return Config(
        gitlab_base_url="https://codehub.example.com",
        gitlab_token="glpat-xxxx",
        webhook_secret="",
        webhook_host="127.0.0.1",
        webhook_port=0,
        webhook_post_comment=True,
    )


def _payload(**overrides: object) -> dict:
    """Build a minimal merge-request webhook body, with optional overrides."""
    data: dict = {
        "object_kind": "merge_request",
        "object_attributes": {
            "action": "open",
            "conflict": False,
            "url": BASE_MR_URL,
        },
        "project": {
            "path_with_namespace": "team/project",
        },
    }
    for key, value in overrides.items():
        *path, leaf = key.split(".")
        target: dict = data
        for segment in path:
            target = target.setdefault(segment, {})
        target[leaf] = value
    return data


# ---------------------------------------------------------------------------
# parse_webhook_payload
# ---------------------------------------------------------------------------


def test_parse_open_triggers(base_config: Config):
    mr = parse_webhook_payload(_payload(), base_config)
    assert mr == GitLabMrUrl("https://codehub.example.com", "team/project", 7)


def test_parse_reopen_triggers(base_config: Config):
    mr = parse_webhook_payload(
        _payload(**{"object_attributes.action": "reopen"}), base_config
    )
    assert mr is not None
    assert mr.mr_iid == 7


def test_parse_update_source_triggers(base_config: Config):
    mr = parse_webhook_payload(
        _payload(
            **{
                "object_attributes.action": "update",
                "object_attributes.update_reason": "source update",
                "object_attributes.oldrev": "b557cc51763d31926746cda3b03f3e2a76a02b4b",
            }
        ),
        base_config,
    )
    assert mr is not None
    assert mr.project_path == "team/project"
    assert mr.mr_iid == 7


def test_parse_update_mr_ignored(base_config: Config):
    assert (
        parse_webhook_payload(
            _payload(
                **{
                    "object_attributes.action": "update",
                    "object_attributes.update_reason": "mr update",
                }
            ),
            base_config,
        )
        is None
    )


def test_parse_update_no_reason_ignored(base_config: Config):
    assert (
        parse_webhook_payload(
            _payload(**{"object_attributes.action": "update"}),
            base_config,
        )
        is None
    )


def test_parse_merge_ignored(base_config: Config):
    assert (
        parse_webhook_payload(
            _payload(**{"object_attributes.action": "merge"}),
            base_config,
        )
        is None
    )


def test_parse_close_ignored(base_config: Config):
    assert (
        parse_webhook_payload(
            _payload(**{"object_attributes.action": "close"}),
            base_config,
        )
        is None
    )


def test_parse_stop_ignored(base_config: Config):
    assert (
        parse_webhook_payload(
            _payload(**{"object_attributes.action": "stop"}),
            base_config,
        )
        is None
    )


def test_parse_conflict_ignored(base_config: Config):
    assert (
        parse_webhook_payload(
            _payload(**{"object_attributes.conflict": True}),
            base_config,
        )
        is None
    )


def test_parse_non_mr_ignored(base_config: Config):
    assert (
        parse_webhook_payload(
            _payload(**{"object_kind": "push"}),
            base_config,
        )
        is None
    )


def test_parse_missing_url(base_config: Config):
    data = _payload()
    del data["object_attributes"]["url"]
    assert parse_webhook_payload(data, base_config) is None


def test_parse_rejects_non_matching_host(base_config: Config):
    config = Config(
        gitlab_base_url="https://other.example.com",
        webhook_secret="",
    )
    assert (
        parse_webhook_payload(
            _payload(**{"object_attributes.url": BASE_MR_URL}), config
        )
        is None
    )


def test_parse_allowed_repos_rejects(base_config: Config):
    config = Config(
        gitlab_base_url="https://codehub.example.com",
        allowed_repos={"other/project"},
        webhook_secret="",
    )
    assert parse_webhook_payload(_payload(), config) is None


def test_parse_allowed_repos_accepts(base_config: Config):
    config = Config(
        gitlab_base_url="https://codehub.example.com",
        allowed_repos={"team/project"},
        webhook_secret="",
    )
    assert parse_webhook_payload(_payload(), config) is not None


def test_parse_nested_project_path(base_config: Config):
    mr = parse_webhook_payload(
        _payload(
            **{
                "object_attributes.url": "https://codehub.example.com/a/b/c/merge_requests/42",
            }
        ),
        base_config,
    )
    assert mr is not None
    assert mr.project_path == "a/b/c"
    assert mr.mr_iid == 42


# ---------------------------------------------------------------------------
# HTTP server integration
# ---------------------------------------------------------------------------


def test_webhook_no_secret_accepted(tmp_path: Path):
    config = Config(
        gitlab_base_url="https://codehub.example.com",
        gitlab_token="token",
        webhook_secret="",
        webhook_host="127.0.0.1",
        webhook_port=0,
    )
    server = _start_server(config)
    port = server.server_address[1]

    resp = _post_webhook(port, _payload())
    assert resp.status == 200
    assert resp.read() == b"accepted"
    server.shutdown()


def test_webhook_valid_secret_accepted(tmp_path: Path):
    config = Config(
        gitlab_base_url="https://codehub.example.com",
        gitlab_token="token",
        webhook_secret="s3cret",
        webhook_secret_header="X-CodeHub-Token",
        webhook_host="127.0.0.1",
        webhook_port=0,
    )
    server = _start_server(config)
    port = server.server_address[1]

    resp = _post_webhook(
        port, _payload(), headers={"X-CodeHub-Token": "s3cret"}
    )
    assert resp.status == 200
    assert resp.read() == b"accepted"
    server.shutdown()


def test_webhook_custom_header_accepted(tmp_path: Path):
    config = Config(
        gitlab_base_url="https://codehub.example.com",
        gitlab_token="token",
        webhook_secret="s3cret",
        webhook_secret_header="X-My-Token",
        webhook_host="127.0.0.1",
        webhook_port=0,
    )
    server = _start_server(config)
    port = server.server_address[1]

    resp = _post_webhook(port, _payload(), headers={"X-My-Token": "s3cret"})
    assert resp.status == 200
    server.shutdown()


def test_webhook_invalid_secret_rejected(tmp_path: Path):
    config = Config(
        gitlab_base_url="https://codehub.example.com",
        gitlab_token="token",
        webhook_secret="correct",
        webhook_secret_header="X-CodeHub-Token",
        webhook_host="127.0.0.1",
        webhook_port=0,
    )
    server = _start_server(config)
    port = server.server_address[1]

    with pytest.raises(HTTPError) as excinfo:
        _post_webhook(port, _payload(), headers={"X-CodeHub-Token": "wrong"})
    assert excinfo.value.code == 403
    server.shutdown()


def test_webhook_ignored_for_non_mr_event(tmp_path: Path):
    config = Config(
        gitlab_base_url="https://codehub.example.com",
        gitlab_token="token",
        webhook_secret="",
        webhook_host="127.0.0.1",
        webhook_port=0,
    )
    server = _start_server(config)
    port = server.server_address[1]

    resp = _post_webhook(port, _payload(**{"object_kind": "push"}))
    assert resp.status == 200
    assert resp.read() == b"ignored"
    server.shutdown()


def test_webhook_ignored_for_update_mr_update(tmp_path: Path):
    config = Config(
        gitlab_base_url="https://codehub.example.com",
        gitlab_token="token",
        webhook_secret="",
        webhook_host="127.0.0.1",
        webhook_port=0,
    )
    server = _start_server(config)
    port = server.server_address[1]

    resp = _post_webhook(
        port,
        _payload(
            **{
                "object_attributes.action": "update",
                "object_attributes.update_reason": "mr update",
            }
        ),
    )
    assert resp.status == 200
    assert resp.read() == b"ignored"
    server.shutdown()


def test_webhook_bad_json(tmp_path: Path):
    config = Config(
        gitlab_base_url="https://codehub.example.com",
        webhook_secret="",
        webhook_host="127.0.0.1",
        webhook_port=0,
    )
    server = _start_server(config)
    port = server.server_address[1]

    with pytest.raises(HTTPError) as excinfo:
        _post_raw(port, b"not json")
    assert excinfo.value.code == 400
    server.shutdown()


def test_webhook_empty_body(tmp_path: Path):
    config = Config(
        gitlab_base_url="https://codehub.example.com",
        webhook_secret="",
        webhook_host="127.0.0.1",
        webhook_port=0,
    )
    server = _start_server(config)
    port = server.server_address[1]

    with pytest.raises(HTTPError) as excinfo:
        _post_raw(port, b"")
    assert excinfo.value.code == 400
    server.shutdown()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _start_server(config: Config) -> HTTPServer:
    from mr_reviewer.gitlab import GitLabClient
    from mr_reviewer.reviewer import ReviewService

    gitlab = GitLabClient(config.gitlab_base_url, config.gitlab_token)
    service = ReviewService(gitlab, None, None)  # type: ignore[arg-type]

    handler = make_handler(config, service, gitlab)
    server = ThreadedHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.05)
    return server


def _post_webhook(port: int, payload: dict, *, headers: dict | None = None) -> object:
    body = json.dumps(payload).encode("utf-8")
    url = f"http://127.0.0.1:{port}/"
    req = Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    return urlopen(req, timeout=5)


def _post_raw(port: int, data: bytes) -> object:
    url = f"http://127.0.0.1:{port}/"
    req = Request(url, data=data, method="POST")
    return urlopen(req, timeout=5)
