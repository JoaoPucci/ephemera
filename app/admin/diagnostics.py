"""Self-debug commands: diagnose, verify, analytics-summary.

These intentionally bypass the "don't reveal which factor is wrong"
rule -- at the CLI you already have shell access, helpfulness beats
ceremony. The raw TOTP seed in `diagnose` is gated behind --show-secret
so the routine clock-drift case doesn't put it on terminal scrollback.
"""

import getpass
import sys
import time
from datetime import UTC, datetime

import pyotp

from .. import analytics, auth, models
from . import _core


def cmd_diagnose(username: str | None, show_secret: bool = False) -> None:
    # `_resolve_user` returns without TOTP plaintext (module default).
    # This command generates candidate TOTP codes from the seed, so
    # explicitly re-fetch the with-TOTP variant.
    resolved = _core._resolve_user(username)
    user = models.get_user_with_totp_by_id(resolved["id"])
    secret = user["totp_secret"]
    totp = pyotp.TOTP(secret, digits=auth.TOTP_DIGITS, interval=auth.TOTP_INTERVAL)

    now_ts = int(time.time())
    step = now_ts // auth.TOTP_INTERVAL
    last_step = int(user.get("totp_last_step") or 0)
    seconds_into = now_ts - step * auth.TOTP_INTERVAL
    seconds_left = auth.TOTP_INTERVAL - seconds_into

    print()
    print(f"User:                '{user['username']}' (id={user['id']})")
    print(f"Server time (UTC):   {datetime.now(UTC).isoformat()}")
    print(f"Server unix ts:      {now_ts}")
    print(
        f"Current TOTP step:   {step}  ({seconds_into}s in, {seconds_left}s until next rotation)"
    )
    print(f"Last step used:      {last_step}")
    print()
    print("If your authenticator's current code matches any of these, login will work:")
    print(f"  previous step ({step - 1}):  {totp.at(now_ts - auth.TOTP_INTERVAL)}")
    print(
        f"  current  step ({step}):      {totp.at(now_ts)}   <-- this is what it should show now"
    )
    print(f"  next     step ({step + 1}):  {totp.at(now_ts + auth.TOTP_INTERVAL)}")
    print()
    # The raw TOTP seed is gated behind --show-secret. The common reason
    # to run `diagnose` is clock drift -- the three candidate codes above
    # answer that question. The raw seed is only needed for the rare
    # "re-enter my authenticator entry by hand" case, and having it on
    # the terminal by default means tmux scrollback, screen-share demos,
    # and accidental paste-into-chat all carry the seed for the common
    # case too.
    if show_secret:
        print("  !! DO NOT paste, screenshot, or share the line below.")
        print("  !! It is equivalent to a password + 2FA combined.")
        print(f"Stored TOTP secret:  {secret}")
    else:
        print(
            "(Stored TOTP secret not shown. If you need to re-enter it in your "
            "authenticator manually, rerun with `--show-secret`.)"
        )
    print()
    print("If your authenticator shows a different code for the 'current step':")
    print(
        "  -> your authenticator has an OLD entry from a previous `init` / `rotate-totp`."
    )
    print(
        "     Delete that entry in the authenticator and re-scan the QR from your last"
    )
    print(
        "     `init` or `rotate-totp`. Or run `rotate-totp` to generate a fresh secret + QR."
    )


def cmd_verify(username: str | None) -> None:
    # Verifies TOTP against the stored seed -- fetch the with-TOTP variant.
    resolved = _core._resolve_user(username)
    user = models.get_user_with_totp_by_id(resolved["id"])
    password = getpass.getpass("Password: ")
    code = input("6-digit code: ").strip()

    pw_ok = auth.verify_password(password, user["password_hash"])
    totp_step = None
    if code.isdigit() and len(code) == auth.TOTP_DIGITS:
        totp_step = auth.verify_totp(
            user["totp_secret"], code, last_step=user["totp_last_step"]
        )

    print()
    print(f"user:      '{user['username']}' (id={user['id']})")
    print(f"password:  {'OK' if pw_ok else 'MISMATCH'}")
    print(
        f"totp:      {'OK (step ' + str(totp_step) + ')' if totp_step is not None else 'MISMATCH'}"
    )
    print(f"stored totp_last_step: {user['totp_last_step']}")
    print()

    if not pw_ok and totp_step is None:
        print("Both password and TOTP are wrong.")
    elif not pw_ok:
        print("Password is wrong; TOTP is correct.")
    elif totp_step is None:
        print(
            "Password is right; TOTP is wrong (clock drift or stale authenticator entry)."
        )
    else:
        print("Both correct. Login via the web UI should succeed with these values.")


def cmd_analytics_summary(event_type: str) -> None:
    """Print summary stats for events of the given type. Read-only.
    No user flag -- analytics aggregates across all users (the user_id
    on each row is anonymised by ON DELETE SET NULL when a user is
    removed)."""
    if event_type not in analytics.EVENT_REGISTRY:
        print(
            f"unknown event_type: {event_type!r}",
            file=sys.stderr,
        )
        print(
            f"known: {sorted(analytics.EVENT_REGISTRY.keys())}",
            file=sys.stderr,
        )
        sys.exit(2)
    s = analytics.summarize(event_type)
    print(f"event_type: {event_type}")
    print(f"count: {s['count']}")
    if not s["fields"]:
        return
    for field, stats in s["fields"].items():
        print(
            f"  {field}: count={stats['count']} "
            f"min={stats['min']} p50={stats['p50']} "
            f"p95={stats['p95']} p99={stats['p99']} max={stats['max']}"
        )
