"""MIME detection and image validation for ephemera."""

ALLOWED_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})


class ValidationError(Exception):
    pass


def detect_mime(data: bytes) -> str | None:
    if len(data) < 12:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def validate_image(data: bytes, declared_mime: str, max_bytes: int) -> str:
    if not data:
        raise ValidationError("empty file")
    if len(data) > max_bytes:
        raise ValidationError("file too large")
    if declared_mime not in ALLOWED_MIME_TYPES:
        raise ValidationError(f"disallowed content type: {declared_mime}")
    detected = detect_mime(data)
    if detected is None:
        raise ValidationError("unrecognized file format")
    if detected != declared_mime:
        raise ValidationError(
            f"content type mismatch: declared {declared_mime}, detected {detected}"
        )
    return detected
