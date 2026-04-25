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

What NEVER goes in a field value: passphrase, plaintext, client_half,
password, totp_code, server_key, ciphertext, recovery codes, api-token
plaintext. Field values are logged verbatim; the filter is at the
call-site.
"""

import json
import logging
import sys
from datetime import UTC, datetime

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
    _handler._ephemera_installed = True
    _EPHEMERA_ROOT.addHandler(_handler)


_logger = logging.getLogger("ephemera.security")


def emit(event: str, **fields) -> None:
    """Write one structured security event as a single JSON line."""
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
