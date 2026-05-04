"""Tests for app.config env-file discovery."""

from pathlib import Path

import pytest


def test_filter_readable_skips_missing_and_includes_existing(tmp_path: Path) -> None:
    """Given a list of candidate .env-style paths, only the ones the
    current process can actually open should make it into the tuple
    pydantic-settings sees. Everything else (missing, unreadable) is
    silently skipped so pydantic doesn't crash trying to open a path
    that isn't there."""
    from app.config import _filter_readable

    exists = tmp_path / "exists.env"
    exists.write_text("EPHEMERA_FAKE=1\n")
    missing = tmp_path / "missing.env"

    result = _filter_readable((str(exists), str(missing)))
    assert str(exists) in result
    assert str(missing) not in result


def test_filter_readable_skips_unreadable_files(tmp_path: Path) -> None:
    """File exists but the current process can't read it. Must be
    skipped with no error, same as if the file didn't exist at all.
    Without this, pydantic-settings would raise PermissionError at
    import time on any host that has an env file the caller can't
    open -- we'd rather silently fall through to defaults."""
    import os

    from app.config import _filter_readable

    locked = tmp_path / "locked.env"
    locked.write_text("EPHEMERA_FAKE=1\n")
    os.chmod(locked, 0o000)
    try:
        if os.access(str(locked), os.R_OK):
            # Running as root defeats the test's premise; skip rather
            # than silently pass.
            pytest.skip("running as root -- unreadable-file branch can't fire")

        result = _filter_readable((str(locked),))
        assert str(locked) not in result
    finally:
        # Restore perms so pytest's tmp_path cleanup can remove the file.
        os.chmod(locked, 0o600)


def test_env_file_candidates_layering_includes_system_and_dev_paths() -> None:
    """The candidate list must include the system path the deployment
    docs publish (`/etc/ephemera/env`) and the XDG dev location. If
    anyone removes either, that removal should be a deliberate change,
    not a drive-by."""
    from app.config import _ENV_FILE_CANDIDATES

    assert "/etc/ephemera/env" in _ENV_FILE_CANDIDATES
    assert any(
        p.endswith(".local/share/ephemera-dev/.env") for p in _ENV_FILE_CANDIDATES
    )
    # Repo-root fallback still there for fresh clones.
    assert ".env" in _ENV_FILE_CANDIDATES


def test_env_file_precedence_system_wins_over_dev() -> None:
    """pydantic-settings loads `env_file` in tuple order, later entries
    overriding earlier ones. The candidate list must put `/etc/ephemera/env`
    LAST so a prod host with a stale dev-side XDG file doesn't have admin-
    CLI commands silently pick up the wrong config. Matches the UNIX
    convention: system-wide config outranks per-user config."""
    from app.config import _ENV_FILE_CANDIDATES

    system_index = _ENV_FILE_CANDIDATES.index("/etc/ephemera/env")
    # System must be the LAST index so it wins when multiple files exist.
    assert system_index == len(_ENV_FILE_CANDIDATES) - 1, (
        f"/etc/ephemera/env at position {system_index} of "
        f"{len(_ENV_FILE_CANDIDATES)}; must be last so it wins precedence"
    )
    # Dev XDG file must be earlier so system overrides it.
    xdg_index = next(
        i
        for i, p in enumerate(_ENV_FILE_CANDIDATES)
        if p.endswith(".local/share/ephemera-dev/.env")
    )
    assert xdg_index < system_index


def test_db_path_default_is_xdg_not_repo_root() -> None:
    """The code-level default for EPHEMERA_DB_PATH (used only when every
    higher-priority source is absent -- fresh clone, no .env anywhere)
    must resolve to the XDG data dir, not the repo root. Without this
    invariant a new contributor running `run.py` with no config would
    silently create ephemera.db + WAL/SHM sidecars next to source, which
    is the hygiene anti-pattern the .env.example header and the env_file
    tuple already steer people away from."""
    from app.config import Settings

    # Clean environment so only the field default resolves. BaseSettings
    # reads os.environ by default; override to an empty _env_file so any
    # EPHEMERA_DB_PATH we happen to have set while running tests doesn't
    # mask the default-under-test.
    # _env_file / _env_file_encoding are runtime-supported pydantic-settings
    # init kwargs that aren't on the model schema; mypy can't see them.
    s = Settings(_env_file=None, _env_file_encoding=None)  # type: ignore[call-arg]
    default_path = s.db_path
    assert ".local/share/ephemera-dev/ephemera.db" in default_path, (
        f"db_path default is {default_path!r}; should point at the XDG data dir"
    )
    assert not default_path.startswith("./")
    assert not default_path.startswith("ephemera.db")


def test_connect_creates_parent_directory_if_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_connect must mkdir -p the db_path's parent directory before
    sqlite3 tries to open the file inside it. Without this, a fresh
    clone with no env config would crash at first DB touch because
    ~/.local/share/ephemera-dev/ doesn't exist yet."""
    from app import config
    from app.models._core import _connect

    # Point at a path whose parent doesn't exist.
    nested = tmp_path / "deep" / "nested" / "does-not-exist-yet" / "ephemera.db"
    assert not nested.parent.exists()

    monkeypatch.setenv("EPHEMERA_DB_PATH", str(nested))
    config.get_settings.cache_clear()
    try:
        conn = _connect()
        try:
            # If we got here without OSError, mkdir worked and sqlite
            # created the file.
            assert nested.parent.exists()
            assert nested.exists()
        finally:
            conn.close()
    finally:
        config.get_settings.cache_clear()
