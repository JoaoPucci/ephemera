"""User-account endpoints (`/api/me*`).

Three routes live here today:

* `GET  /api/me`               -- minimal view of the authenticated caller.
* `PATCH /api/me/preferences`  -- generic preferences mutation (today: only
                                  `analytics_opt_in`); shaped for future
                                  user-scoped settings to join without a
                                  new endpoint.
* `PATCH /api/me/language`     -- BCP-47 preferred-language tag (separate
                                  from /preferences because anonymous
                                  callers also hit it from the picker
                                  and the auth posture differs).

These are grouped here rather than in `sender.py` because they're
account-level rather than secret-level: the same routes are consumed
by every authenticated surface (sender form, future admin page,
chrome-menu drawer toggle) and aren't part of the create / track / cancel
flow that owns `/api/secrets*`.
"""

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from .. import models, security_log
from ..dependencies import (
    current_user_id,
    verify_api_token_or_session,
    verify_same_origin,
)
from ..errors import http_error
from ..i18n import SUPPORTED
from ..limiter import read_rate_limit
from ..models import users as users_model
from ..schemas import ApiMeResponse, UpdatePreferencesBody

router = APIRouter()


class LanguagePatch(BaseModel):
    language: str | None = None


@router.get(
    "/api/me",
    response_model=ApiMeResponse,
    dependencies=[Depends(read_rate_limit)],
)
def api_me(user: dict = Depends(verify_api_token_or_session)):
    """Return a minimal view of the authenticated user (for header UI etc.)."""
    return ApiMeResponse(
        id=user["id"],
        username=user["username"],
        email=user.get("email"),
        analytics_opt_in=bool(user.get("analytics_opt_in")),
    )


@router.patch(
    "/api/me/preferences",
    response_model=ApiMeResponse,
    dependencies=[Depends(verify_same_origin), Depends(read_rate_limit)],
)
def update_preferences(
    body: UpdatePreferencesBody,
    request: Request,
    user: dict = Depends(verify_api_token_or_session),
):
    """Flip user-scoped preferences. Today's only knob is `analytics_opt_in`
    (per-user telemetry consent); the route is shaped as a generic
    preferences mutation so future user-scoped settings can join without
    a new endpoint.

    Each actual flip emits a `preferences.analytics_changed` security_log
    entry so an operator can answer "who consented when" without joining
    the aggregate-only analytics_events table (which deliberately carries
    no user_id). No-op PATCH (sending the current value) does not log.

    Concurrency: the change-detection happens in SQL via a conditional
    `UPDATE ... WHERE analytics_opt_in != ?`. A naive read-modify-write
    in Python would no-op a real change if two requests both observed
    the same pre-flip value (rapid on->off->on clicks, or multi-tab).
    The atomic UPDATE returns the new value when it actually fired, or
    None when no row changed; we drive both the security_log and the
    response off that return so audit and reply always reflect ground
    truth.
    """
    if body.analytics_opt_in is not None:
        desired = 1 if body.analytics_opt_in else 0
        persisted = models.set_analytics_opt_in(user["id"], desired)
        if persisted is not None:
            security_log.emit(
                "preferences.analytics_changed",
                user_id=user["id"],
                username=user["username"],
                enabled=bool(persisted),
                client_ip=security_log.client_ip(request),
            )
            user = {**user, "analytics_opt_in": persisted}
        else:
            # No-op (value already matched). The request-scoped `user`
            # snapshot may itself be stale relative to a concurrent
            # PATCH that just landed; re-read so the response carries
            # the actual persisted value, not the read-time copy.
            fresh = models.get_user_by_id(user["id"])
            if fresh is not None:
                user = {**user, "analytics_opt_in": fresh.get("analytics_opt_in", 0)}
    return ApiMeResponse(
        id=user["id"],
        username=user["username"],
        email=user.get("email"),
        analytics_opt_in=bool(user.get("analytics_opt_in")),
    )


@router.patch("/api/me/language", status_code=204)
def patch_language(
    body: LanguagePatch,
    request: Request,
    _origin=Depends(verify_same_origin),
    _rate=Depends(read_rate_limit),
) -> Response:
    """Persist the user's preferred UI language (BCP-47 tag). None clears
    the preference so resolution falls back to cookie / Accept-Language /
    default. Requires an authenticated session; anonymous callers get 401
    and are expected to rely on the cookie (which the picker widget has
    already set by the time this endpoint is contacted)."""
    # Auth first -- unauthenticated callers shouldn't learn whether a
    # particular language tag is valid (that's a 400 vs 401 distinction
    # that otherwise leaks the SUPPORTED set to anyone probing).
    uid = current_user_id(request)
    if uid is None:
        raise http_error(401, "not_authenticated")
    if body.language is not None and body.language not in SUPPORTED:
        raise http_error(400, "unsupported_language")
    users_model.set_preferred_language(uid, body.language)
    return Response(status_code=204)
