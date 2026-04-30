"""Tests for app.validation: MIME whitelist, magic bytes, size limits."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app import validation


def test_png_magic_bytes_accepted(sample_png_bytes):
    mime = validation.validate_image(
        sample_png_bytes, "image/png", max_bytes=10_000_000
    )
    assert mime == "image/png"


def test_jpeg_magic_bytes_accepted(sample_jpeg_bytes):
    mime = validation.validate_image(
        sample_jpeg_bytes, "image/jpeg", max_bytes=10_000_000
    )
    assert mime == "image/jpeg"


def test_gif_magic_bytes_accepted(sample_gif_bytes):
    mime = validation.validate_image(
        sample_gif_bytes, "image/gif", max_bytes=10_000_000
    )
    assert mime == "image/gif"


def test_webp_magic_bytes_accepted(sample_webp_bytes):
    mime = validation.validate_image(
        sample_webp_bytes, "image/webp", max_bytes=10_000_000
    )
    assert mime == "image/webp"


def test_svg_rejected(sample_svg_bytes):
    with pytest.raises(validation.ValidationError):
        validation.validate_image(
            sample_svg_bytes, "image/svg+xml", max_bytes=10_000_000
        )


def test_content_type_header_must_match_magic_bytes(sample_png_bytes):
    with pytest.raises(validation.ValidationError):
        validation.validate_image(sample_png_bytes, "image/jpeg", max_bytes=10_000_000)


def test_file_larger_than_limit_rejected(sample_png_bytes):
    padded = sample_png_bytes + b"\x00" * 2000
    with pytest.raises(validation.ValidationError):
        validation.validate_image(padded, "image/png", max_bytes=1000)


def test_empty_file_rejected():
    with pytest.raises(validation.ValidationError):
        validation.validate_image(b"", "image/png", max_bytes=10_000_000)


def test_unknown_binary_blob_rejected():
    with pytest.raises(validation.ValidationError):
        validation.validate_image(b"\x00" * 64, "image/png", max_bytes=10_000_000)


def test_content_type_whitelist_is_enforced(sample_png_bytes):
    with pytest.raises(validation.ValidationError):
        validation.validate_image(
            sample_png_bytes, "application/octet-stream", max_bytes=10_000_000
        )


def test_detect_mime_returns_correct_values_for_all_formats(
    sample_png_bytes, sample_jpeg_bytes, sample_gif_bytes, sample_webp_bytes
):
    assert validation.detect_mime(sample_png_bytes) == "image/png"
    assert validation.detect_mime(sample_jpeg_bytes) == "image/jpeg"
    assert validation.detect_mime(sample_gif_bytes) == "image/gif"
    assert validation.detect_mime(sample_webp_bytes) == "image/webp"


def test_detect_mime_returns_none_for_svg(sample_svg_bytes):
    assert validation.detect_mime(sample_svg_bytes) is None


# ---------------------------------------------------------------------------
# Property-based tests
#
# Validation is exactly the kind of module where the magic-byte checks
# need to hold across the full byte range, not just the four crafted
# fixtures above. Hypothesis fuzzes detect_mime() against random short
# byte strings and asserts the contract: the function only ever returns
# a known MIME or None, and never raises. Catches "input shorter than
# the magic-byte prefix you sliced" and "byte sequence that looks like
# WEBP but isn't" classes of bug.
# ---------------------------------------------------------------------------


@given(data=st.binary(min_size=0, max_size=64))
@settings(max_examples=300)
def test_property_detect_mime_returns_known_or_none(data: bytes):
    """For any short byte string, detect_mime returns either a value in
    ALLOWED_MIME_TYPES or None. It does not raise, and it does not
    return arbitrary strings. Run with a higher example count because
    the input space is small (8-byte prefixes) and we want strong
    coverage of the prefix space."""
    result = validation.detect_mime(data)
    assert result is None or result in validation.ALLOWED_MIME_TYPES


@given(
    data=st.binary(min_size=12, max_size=512),
    cap=st.integers(min_value=1, max_value=64),
)
@settings(max_examples=50)
def test_property_validate_image_rejects_oversize_for_any_cap(data: bytes, cap: int):
    """For any byte payload whose length exceeds the cap, validation
    rejects with ValidationError regardless of declared MIME. Pins the
    cap as a hard ceiling: it isn't bypassed by valid magic bytes,
    matching the threat model where a malformed-but-large upload still
    can't slip through."""
    if len(data) <= cap:
        return  # not the case we're testing
    with pytest.raises(validation.ValidationError):
        validation.validate_image(data, "image/png", max_bytes=cap)
