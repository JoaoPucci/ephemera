from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# pydantic-settings loads env files in order; later entries win. We prefer
# a dev .env living outside the repo tree (~/.local/share/ephemera-dev/.env)
# so accidental tar/rsync/IDE-search/backup tooling on the project folder
# doesn't scoop the dev SECRET_KEY along with the code. Falls back to a
# repo-root .env so fresh clones and legacy setups keep working untouched;
# production sets env vars via systemd's EnvironmentFile and doesn't rely
# on either path.
_DEV_ENV_FILES = (
    ".env",
    str(Path.home() / ".local" / "share" / "ephemera-dev" / ".env"),
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EPHEMERA_",
        env_file=_DEV_ENV_FILES,
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
