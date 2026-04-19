import os
from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# pydantic-settings loads env files in order, later entries winning.
# Three candidate locations, ordered from most-general to most-specific:
# a system-wide config file (ignored on dev, only present where docs say
# to put one), the repo-root fallback for fresh clones, and the XDG dev
# file that wins when both are present. _filter_readable drops paths
# the current process can't open so pydantic-settings doesn't raise on
# a file that exists-but-is-unreadable (mode 0640 is common for config
# files that include secrets). Deployment paths and perms are specified
# in docs/deployment.md, not restated here.
_ENV_FILE_CANDIDATES = (
    "/etc/ephemera/env",
    ".env",
    str(Path.home() / ".local" / "share" / "ephemera-dev" / ".env"),
)


def _filter_readable(paths: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(p for p in paths if os.access(p, os.R_OK))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EPHEMERA_",
        env_file=_filter_readable(_ENV_FILE_CANDIDATES),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    secret_key: str = Field(default="dev-secret-key-change-me")
    db_path: str = Field(default="./ephemera.db")
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

    @property
    def origins(self) -> List[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
