"""Tests for app.config env-file discovery."""


def test_filter_readable_skips_missing_and_includes_existing(tmp_path):
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


def test_filter_readable_skips_unreadable_files(tmp_path):
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
            import pytest
            pytest.skip("running as root -- unreadable-file branch can't fire")

        result = _filter_readable((str(locked),))
        assert str(locked) not in result
    finally:
        # Restore perms so pytest's tmp_path cleanup can remove the file.
        os.chmod(locked, 0o600)


def test_env_file_candidates_layering_includes_system_and_dev_paths():
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
