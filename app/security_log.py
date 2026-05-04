"""Structured audit log for security-relevant events.

Each `emit(event, **fields)` call writes one JSON line to the
`ephemera.security` logger at INFO. Lines land wherever Python logging
is configured to land -- under systemd that's `journalctl -u ephemera`,
at the CLI that's stderr. Operators can filter with
`journalctl -u ephemera -o cat | grep '"event":"login.failure"' | jq`.

Events logged today (keep this list in sync with real call-sites):

  login.success           user_id, username, client_ip
  login.failure           username, client_ip, reason
  login.lockout           user_id, username, client_ip, until
  reveal.success          secret_id, client_ip
  reveal.wrong_passphrase secret_id, client_ip, attempts
  reveal.burned           secret_id, client_ip
  secret.canceled         user_id, username, secret_id
  secret.cleared          user_id, username, count
  apitoken.created        user_id, username, token_name
  apitoken.revoked        user_id, username, token_name
  user.added              user_id, username
  user.removed            user_id, username, forced
  password.reset          user_id, username
  totp.rotated            user_id, username
  recovery.regenerated    user_id, username
  preferences.analytics_changed   user_id, username, enabled, client_ip

What NEVER goes in a field value: passphrase, plaintext, client_half,
password, totp_code, server_key, ciphertext, recovery codes, api-token
plaintext. Field values are logged verbatim; the filter is at the
call-site.
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from fastapi import Request

# Uvicorn's default LOGGING_CONFIG only configures `uvicorn`, `uvicorn.error`,
# and `uvicorn.access`; it leaves the root logger with no handler. Records
# from ephemera.* loggers propagate to root and then fall through to Python's
# lastResort handler, which filters at WARNING -- silently dropping every
# INFO-level event we care about (security events, cleanup-purged lines).
#
# Attach one INFO-level stderr handler to the `ephemera` parent logger so all
# ephemera.* children inherit it. systemd captures stderr -> journald in
# production; the dev terminal sees the same stream. Keep propagate=True so
# pytest's caplog (which attaches at root) still sees records in tests.
_EPHEMERA_ROOT = logging.getLogger("ephemera")
_EPHEMERA_ROOT.setLevel(logging.INFO)
if not any(getattr(h, "_ephemera_installed", False) for h in _EPHEMERA_ROOT.handlers):
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    # Tag the handler so subsequent imports of this module (pytest's
    # `client` fixture creates a fresh app each test, which re-runs
    # `app/__init__.py` and indirectly this module) skip re-installing
    # it. Dynamic attribute on the StreamHandler instance; mypy's
    # logging stubs don't model arbitrary attrs, hence the ignore.
    _handler._ephemera_installed = True  # type: ignore[attr-defined]
    _EPHEMERA_ROOT.addHandler(_handler)


_logger = logging.getLogger("ephemera.security")


def emit(event: str, **fields: Any) -> None:
    """Write one structured security event as a single JSON line.

    Field-shape conventions (the audit log's posture, applied at every
    call site -- see docs/threat_model.md for the rationale):

    - **Authenticated subject events** carry both `user_id` (canonical
      int identifier) AND `username` (the user-facing handle).
      user_id alone would force an operator to re-resolve identity from
      the DB on every triage read; username alone would lose the
      forward-compat handle if the schema ever permits username
      rotation. Both means an immediate-readable line and a stable
      identifier, accepting the small redundancy at log-write time.
    - **Receiver-side events** (reveal.* family) carry the `secret_id`
      but NOT `client_ip`. Receivers are anonymous-by-design in this
      product (didn't sign up, didn't consent to identity capture);
      logging the IP would create a "this address reached this
      secret" correlation in journald that doesn't sit anywhere else
      in the system.
    - **`unknown_user` login failures** carry `client_ip` + `reason`
      but NOT the user-submitted username string. Form-field stuffing
      in a probe loop shouldn't accumulate as logged data the project
      never asked for.
    - **No plaintext-equivalent fields ever** -- never pass
      passphrase / client_half / password / totp_code / server_key /
      ciphertext into a field. The emit-site is the boundary; the
      caller-side filter is documented at every call site.

    Adding a new event type: pick fields per the rules above. If a
    new field is borderline (user-submitted strings, identifying
    metadata about a non-user, etc.), that's a posture decision and
    belongs in docs/threat_model.md, not silently in a single emit
    call."""
    payload = {
        "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event": event,
        **fields,
    }
    _logger.info(json.dumps(payload, default=str, separators=(",", ":")))


def client_ip(request: Request | None) -> str:
    """Best-effort client IP for an HTTP event. Returns 'cli' for off-request
    contexts (admin CLI) and 'unknown' if the request has no client tuple."""
    if request is None:
        return "cli"
    return request.client.host if request.client else "unknown"
