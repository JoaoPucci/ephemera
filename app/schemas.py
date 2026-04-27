"""Pydantic request/response models for the HTTP API.

Every 2xx route response is declared via a model here and wired into the
endpoint with `response_model=...`. FastAPI then:

  * validates the outbound body against the declared shape (extra fields
    are stripped, missing required fields raise 500);
  * populates OpenAPI docs with accurate schemas;
  * gives callers (and future TypeScript clients) a single source of truth
    for the wire contract.

Keep this module small and import-only. No I/O, no DB access. Route-specific
logic stays in `app/routes/`.
"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

# Exposed here because it drives validation of CreateTextSecret.expires_in
# (and the equivalent multipart path in the create route handles the same set).
EXPIRY_PRESETS: set[int] = {300, 1800, 3600, 14400, 43200, 86400, 259200, 604800}


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class CreateTextSecret(BaseModel):
    """JSON body for POST /api/secrets when content_type=text."""

    # Cap is 100,000 *characters* (Python code points / `len(str)`), not
    # bytes. Matches the textarea's HTML maxlength so the client-side and
    # server-side limits agree on the user-visible unit. UX-driven by
    # design: "100,000 characters" is a coherent unit across every locale
    # we ship (English, Japanese, Chinese, Korean, ...). A bytes cap would
    # be unfair to Japanese/CJK users -- they'd hit the limit at ~33K
    # visible characters because each is 3 UTF-8 bytes.
    #
    # Worst case server-side after this layer accepts:
    #   plaintext bytes  = up to 100,000 chars * 4 bytes/char (supplementary
    #                      plane: emoji, rare CJK ext) = ~400 KB
    #   ciphertext bytes = ~534 KB (Fernet adds 57 bytes framing + AES
    #                      padding to 16-byte blocks, then base64 4/3x)
    #   request body     = ~410 KB worst case (content + JSON envelope +
    #                      passphrase/label/etc.) -- well within Caddy's
    #                      11 MB body cap (docs/deployment.md).
    # For typical content (ASCII, mostly-BMP CJK) the numbers are 1-3x
    # smaller. The 1 MB ceiling that used to live here was an arbitrary
    # safety stop from before there was a deliberate product cap.
    content: str = Field(min_length=1, max_length=100_000)
    content_type: Literal["text"]
    expires_in: int = Field(
        description="Seconds from now. Must be one of EXPIRY_PRESETS."
    )
    passphrase: str | None = Field(default=None, max_length=200)
    track: bool = False
    label: str | None = Field(default=None, max_length=60)
    # Optional telemetry hint from the sender form. Set true when the user's
    # intended content (typed or pasted, pre-truncation) crossed ~95% of the
    # cap during the compose session -- a signal the backend can't infer
    # post-hoc, since the textarea silently truncates over-cap pastes and
    # edit-down erases the high-water mark. The route emits a presence-only
    # `content.limit_hit` analytics event when this is true AND analytics is
    # enabled in settings (see app/analytics.py + app/config.py). Aggregate-
    # only by design: no size, no paste-vs-typed, no user identity persisted.
    near_cap: bool = False

    @field_validator("expires_in")
    @classmethod
    def _valid_preset(cls, v: int) -> int:
        if v not in EXPIRY_PRESETS:
            raise ValueError("expires_in must be one of the presets")
        return v


class RevealBody(BaseModel):
    """JSON body for POST /s/{token}/reveal."""

    key: str = Field(
        min_length=1,
        max_length=256,
        description="Client half of the Fernet key (base64url).",
    )
    passphrase: str | None = Field(default=None, max_length=200)


# ---------------------------------------------------------------------------
# Responses -- auth
# ---------------------------------------------------------------------------


class LoginResponse(BaseModel):
    ok: Literal[True] = True
    username: str


class LogoutResponse(BaseModel):
    ok: Literal[True] = True


class ApiMeResponse(BaseModel):
    id: int
    username: str
    email: str | None = None


# ---------------------------------------------------------------------------
# Responses -- secrets (sender-side)
# ---------------------------------------------------------------------------


class CreateSecretResponse(BaseModel):
    url: str = Field(description="Full URL including the #fragment client key.")
    id: str = Field(description="Server UUID for status lookups.")
    expires_at: str


class SecretStatusResponse(BaseModel):
    status: Literal["pending", "viewed", "burned", "canceled", "expired"]
    created_at: str
    expires_at: str
    viewed_at: str | None = None


class TrackedSecretItem(BaseModel):
    id: str
    content_type: Literal["text", "image"]
    mime_type: str | None = None
    label: str | None = None
    status: Literal["pending", "viewed", "burned", "canceled", "expired"]
    created_at: str
    expires_at: str
    viewed_at: str | None = None


class TrackedListResponse(BaseModel):
    items: list[TrackedSecretItem]


class ClearTrackedResponse(BaseModel):
    cleared: int


# ---------------------------------------------------------------------------
# Responses -- secrets (receiver-side)
# ---------------------------------------------------------------------------


class LandingMetaResponse(BaseModel):
    passphrase_required: bool


class RevealTextResponse(BaseModel):
    content_type: Literal["text"]
    content: str


class RevealImageResponse(BaseModel):
    content_type: Literal["image"]
    mime_type: str
    content: str = Field(description="Base64-encoded image bytes.")


# Discriminated union: the client switches on content_type to know which
# fields to expect. FastAPI serializes this correctly at the boundary.
RevealResponse = RevealTextResponse | RevealImageResponse
