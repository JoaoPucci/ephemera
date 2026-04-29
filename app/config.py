import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# pydantic-settings loads env files in order, later entries winning.
# Ordered from per-user (lowest priority) to system-wide (highest), so
# the system file is authoritative when present and per-user files are
# the expected override for single-user dev machines:
#   1. XDG dev file   -- contributor-local config for day-to-day dev
#   2. ./.env         -- repo-root fallback for fresh clones and legacy setups
#   3. /etc/ephemera/env -- the install-recipe path; wins when present
# On a prod host only the system file exists; on a dev box only a user
# file exists; in rare mixed scenarios (dev laptop rsync'd from prod,
# shared admin box with leftover dev config), the system file is the
# operator's authoritative statement and should win over user-level
# overrides -- matches the UNIX convention where system config outranks
# per-user config.
# _filter_readable drops paths the current process can't open so
# pydantic-settings doesn't raise on a file that exists-but-is-unreadable
# (mode 0640 is common for config files that include secrets).
# Deployment paths and perms are specified in docs/deployment.md, not
# restated here.
_ENV_FILE_CANDIDATES = (
    str(Path.home() / ".local" / "share" / "ephemera-dev" / ".env"),
    ".env",
    "/etc/ephemera/env",
)


def _filter_readable(paths: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(p for p in paths if os.access(p, os.R_OK))


# Default DB path for fresh clones with no env configuration. Points at the
# XDG data dir (not the repo root) so a new contributor running `run.py`
# with no .env doesn't sprinkle ephemera.db + WAL/SHM sidecars next to
# source -- the same hygiene the .env.example header and the env_file
# tuple steer people toward. Production overrides this via
# EPHEMERA_DB_PATH in /etc/ephemera/env; this default only resolves when
# every higher-priority config source is absent.
_DEFAULT_DB_PATH = str(
    Path.home() / ".local" / "share" / "ephemera-dev" / "ephemera.db"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EPHEMERA_",
        env_file=_filter_readable(_ENV_FILE_CANDIDATES),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    secret_key: str = Field(default="dev-secret-key-change-me")
    db_path: str = Field(default=_DEFAULT_DB_PATH)
    base_url: str = Field(default="http://localhost:8000")
    max_image_bytes: int = Field(default=10 * 1024 * 1024)
    allowed_origins: str = Field(default="http://localhost:8000")
    session_cookie_name: str = Field(default="ephemera_session")
    session_max_age: int = Field(default=60 * 60 * 8)
    # Default True: in production (HTTPS behind Caddy) the cookie must have
    # the Secure flag set. Browsers treat loopback (127.0.0.1, localhost) as a
    # secure context, so dev on `./venv/bin/python run.py` works with True
    # too. Override to False only if you have a legitimate reason (an old
    # browser, a tunnel that terminates TLS upstream in a way we don't control).
    session_cookie_secure: bool = Field(default=True)
    max_passphrase_attempts: int = Field(default=5)
    cleanup_interval_seconds: int = Field(default=60)
    tracked_retention_seconds: int = Field(default=30 * 24 * 60 * 60)
    # Label shown next to the account in authenticator apps (1Password, Aegis,
    # Google Authenticator, ...). Set a distinct value per environment
    # ("ephemera-dev", "ephemera-prod") if you run more than one instance
    # against the same authenticator so the entries don't collide.
    totp_issuer: str = Field(default="ephemera")
    # Aggregate-only product analytics (see app/analytics.py). Off by
    # default: a privacy-focused tool shouldn't collect any telemetry
    # without explicit operator consent. Flip to true via env var
    # EPHEMERA_ANALYTICS_ENABLED=true to opt in. The signal collected
    # is presence-only (`content.limit_hit` event with empty payload),
    # no user identity, no payload content -- enough to answer "did
    # anyone hit the cap, how often" but nothing more.
    analytics_enabled: bool = Field(default=False)
    # Suffix appended to the PWA manifest's `name`/`short_name` and used
    # to pick the apple-touch-icon when an operator runs more than one
    # ephemera instance (typical: a dev / staging box alongside prod).
    # Empty (default) is the prod posture: name="ephemera" and the
    # dark-bg/light-glyph apple-touch-icon. Any non-empty value
    # ("dev", "staging") flips both: name becomes "ephemera-{label}"
    # and the apple-touch-icon switches to the visually-light variant
    # so the two installs are at-a-glance distinguishable on the home
    # screen. Captured icons don't auto-refresh from the manifest, so
    # this only takes effect on a fresh install (or remove-and-re-add
    # of the home-screen entry).
    deployment_label: str = Field(default="")

    @property
    def origins(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
