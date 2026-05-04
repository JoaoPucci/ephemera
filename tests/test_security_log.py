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


def test_login_failure_unknown_user_emits_event_without_user_id_or_username(
    client: TestClient, audit_caplog: pytest.LogCaptureFixture
) -> None:
    """`unknown_user` is the only login.failure variant where the
    `username` field would carry the *user-submitted string* rather
    than the canonical username on a real users row. The audit posture
    is not to accumulate user-submitted strings as logged data: form-
    field stuffing (passwords, emails, junk in the username slot of a
    probe loop) shouldn't end up in journald. The defender's signal
    -- "an account is being probed" -- is preserved by the
    `client_ip` + `reason="unknown_user"` combination."""
    r = client.post(
        "/send/login",
        data={"username": "ghost", "password": "nope", "code": "000000"},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 401
    events = _events(audit_caplog)
    failures = [e for e in events if e["event"] == "login.failure"]
    assert failures and failures[0]["reason"] == "unknown_user"
    assert "user_id" not in failures[0]
    # Submitted-username-as-data is what we're *not* logging now.
    assert "username" not in failures[0]
    # Defender signal (IP + reason) is still there.
    assert "client_ip" in failures[0]


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


def test_reveal_success_does_not_emit_audit_event(
    client: TestClient,
    auth_headers: dict[str, str],
    audit_caplog: pytest.LogCaptureFixture,
) -> None:
    """A successful reveal is the product's happy path -- not a security
    incident -- so it deliberately does NOT emit a security_log event.
    Logging "secret X opened from IP Y at time T" with no accountability
    target (receivers are unauthenticated by design) would create a
    permanent who-opened-what record in journald that doesn't fit the
    audit log's accountability posture. The DB already records the
    lifecycle event in `secrets.viewed_at` (tracked rows) or the row's
    deletion (untracked rows). Symmetric with the deliberate absence
    of `secret.created`: log destructive / authentication / abuse-shaped
    events, not happy-path use."""
    sid, token, frag = _create_and_get_reveal_parts(client, auth_headers)
    r = client.post(
        f"/s/{token}/reveal",
        json={"key": frag},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 200
    events = _events(audit_caplog)
    assert not any(e["event"] == "reveal.success" for e in events)


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
    # Receivers are anonymous-by-design (no signup, no consent to
    # identity capture). The receiver's IP must NOT be logged on
    # reveal events; secret_id + attempts is the abuse-detection
    # signal. See the comment block at the wrong-passphrase emit
    # site in app/routes/receiver.py.
    assert "client_ip" not in wrongs[0]


def test_reveal_burned_event_does_not_carry_receiver_ip(
    client: TestClient,
    auth_headers: dict[str, str],
    audit_caplog: pytest.LogCaptureFixture,
) -> None:
    """Same posture as wrong_passphrase: receiver-side events are
    anonymous-by-design. Burn after N wrong attempts logs the
    secret_id (so the operator can correlate with the wrong_passphrase
    sequence that led up to it) but not the receiver's IP."""
    sid, token, frag = _create_and_get_reveal_parts(
        client, auth_headers, passphrase="right"
    )
    # Drive past the burn threshold (max_passphrase_attempts = 5).
    for _ in range(6):
        client.post(
            f"/s/{token}/reveal",
            json={"key": frag, "passphrase": "wrong"},
            headers={"Origin": "http://testserver"},
        )
    events = _events(audit_caplog)
    burns = [e for e in events if e["event"] == "reveal.burned"]
    assert burns and burns[0]["secret_id"] == sid
    assert "client_ip" not in burns[0]


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
