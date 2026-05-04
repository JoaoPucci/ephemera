"""Tests for the structured security audit log."""

import contextlib
import json
import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient


def _events(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rec in caplog.records:
        if rec.name != "ephemera.security":
            continue
        with contextlib.suppress(ValueError, TypeError):
            out.append(json.loads(rec.message))
    return out


@pytest.fixture
def audit_caplog(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.INFO, logger="ephemera.security")
    return caplog


# ---------------------------------------------------------------------------
# emit()
# ---------------------------------------------------------------------------


def test_emit_writes_json_with_event_ts_and_extra_fields(
    audit_caplog: pytest.LogCaptureFixture,
) -> None:
    from app import security_log

    security_log.emit("test.event", user_id=42, username="alice")

    events = _events(audit_caplog)
    assert len(events) == 1
    e = events[0]
    assert e["event"] == "test.event"
    assert e["user_id"] == 42
    assert e["username"] == "alice"
    assert "ts" in e and e["ts"].endswith("Z")


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


def _login_body(provisioned_user: dict[str, Any]) -> dict[str, str]:
    return {
        "username": provisioned_user["username"],
        "password": provisioned_user["password"],
        "code": provisioned_user["totp"].now(),
    }


def test_login_success_emits_event(
    client: TestClient,
    provisioned_user: dict[str, Any],
    audit_caplog: pytest.LogCaptureFixture,
) -> None:
    r = client.post(
        "/send/login",
        data=_login_body(provisioned_user),
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 200
    events = _events(audit_caplog)
    assert any(
        e["event"] == "login.success"
        and e["username"] == provisioned_user["username"]
        and e["user_id"] == provisioned_user["id"]
        and "client_ip" in e
        for e in events
    )


def test_login_failure_wrong_password_emits_event_with_reason(
    client: TestClient,
    provisioned_user: dict[str, Any],
    audit_caplog: pytest.LogCaptureFixture,
) -> None:
    r = client.post(
        "/send/login",
        data={
            "username": provisioned_user["username"],
            "password": "wrong",
            "code": provisioned_user["totp"].now(),
        },
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 401
    events = _events(audit_caplog)
    failures = [e for e in events if e["event"] == "login.failure"]
    assert failures
    assert failures[0]["reason"] == "wrong_password"
    assert failures[0]["username"] == provisioned_user["username"]


def test_login_failure_unknown_user_emits_event_without_user_id(
    client: TestClient, audit_caplog: pytest.LogCaptureFixture
) -> None:
    r = client.post(
        "/send/login",
        data={"username": "ghost", "password": "nope", "code": "000000"},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 401
    events = _events(audit_caplog)
    failures = [e for e in events if e["event"] == "login.failure"]
    assert failures and failures[0]["reason"] == "unknown_user"
    assert failures[0]["username"] == "ghost"
    assert "user_id" not in failures[0]


def test_login_lockout_event_emitted_when_threshold_crossed(
    client: TestClient,
    provisioned_user: dict[str, Any],
    audit_caplog: pytest.LogCaptureFixture,
) -> None:
    """After MAX_FAILURES wrong attempts, the next failure emits login.lockout
    alongside the final login.failure."""
    from app.auth import MAX_FAILURES

    for _ in range(MAX_FAILURES):
        client.post(
            "/send/login",
            data={
                "username": provisioned_user["username"],
                "password": "wrong",
                "code": "000000",
            },
            headers={"Origin": "http://testserver"},
        )

    events = _events(audit_caplog)
    lockouts = [e for e in events if e["event"] == "login.lockout"]
    assert len(lockouts) == 1
    assert lockouts[0]["user_id"] == provisioned_user["id"]
    assert "until" in lockouts[0]


# ---------------------------------------------------------------------------
# Reveal
# ---------------------------------------------------------------------------


def _create_and_get_reveal_parts(
    client: TestClient,
    auth_headers: dict[str, str],
    passphrase: str | None = None,
) -> tuple[int, str, str]:
    body: dict[str, Any] = {"content": "hi", "content_type": "text", "expires_in": 3600}
    if passphrase is not None:
        body["passphrase"] = passphrase
    r = client.post("/api/secrets", json=body, headers=auth_headers)
    sid = r.json()["id"]
    url = r.json()["url"]
    path, frag = url.split("#", 1)
    token = path.rsplit("/", 1)[-1]
    return sid, token, frag


def test_reveal_success_emits_event_with_secret_id(
    client: TestClient,
    auth_headers: dict[str, str],
    audit_caplog: pytest.LogCaptureFixture,
) -> None:
    sid, token, frag = _create_and_get_reveal_parts(client, auth_headers)
    r = client.post(
        f"/s/{token}/reveal",
        json={"key": frag},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 200
    events = _events(audit_caplog)
    assert any(e["event"] == "reveal.success" and e["secret_id"] == sid for e in events)


def test_reveal_wrong_passphrase_emits_event_with_attempts(
    client: TestClient,
    auth_headers: dict[str, str],
    audit_caplog: pytest.LogCaptureFixture,
) -> None:
    sid, token, frag = _create_and_get_reveal_parts(
        client, auth_headers, passphrase="right"
    )
    r = client.post(
        f"/s/{token}/reveal",
        json={"key": frag, "passphrase": "wrong"},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 401
    events = _events(audit_caplog)
    wrongs = [e for e in events if e["event"] == "reveal.wrong_passphrase"]
    assert wrongs
    assert wrongs[0]["secret_id"] == sid
    assert wrongs[0]["attempts"] == 1


# ---------------------------------------------------------------------------
# Sender-side mutations
# ---------------------------------------------------------------------------


def test_secret_canceled_emits_event(
    client: TestClient,
    auth_headers: dict[str, str],
    audit_caplog: pytest.LogCaptureFixture,
) -> None:
    r = client.post(
        "/api/secrets",
        json={"content": "x", "content_type": "text", "expires_in": 300, "track": True},
        headers=auth_headers,
    )
    sid = r.json()["id"]
    c = client.post(f"/api/secrets/{sid}/cancel", headers=auth_headers)
    assert c.status_code == 204
    events = _events(audit_caplog)
    assert any(
        e["event"] == "secret.canceled" and e["secret_id"] == sid for e in events
    )


def test_tracked_cleared_emits_event_with_count(
    client: TestClient,
    auth_headers: dict[str, str],
    audit_caplog: pytest.LogCaptureFixture,
) -> None:
    # Create one tracked secret and consume it so the clear actually removes something.
    r = client.post(
        "/api/secrets",
        json={"content": "x", "content_type": "text", "expires_in": 300, "track": True},
        headers=auth_headers,
    )
    sid = r.json()["id"]
    url = r.json()["url"]
    token, frag = url.rsplit("/", 1)[-1].split("#", 1)
    client.post(
        f"/s/{token}/reveal",
        json={"key": frag},
        headers={"Origin": "http://testserver"},
    )
    resp = client.post("/api/secrets/tracked/clear", headers=auth_headers)
    assert resp.status_code == 200
    events = _events(audit_caplog)
    cleared = [e for e in events if e["event"] == "secret.cleared"]
    assert cleared and cleared[0]["count"] >= 1
