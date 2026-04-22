"""User preference endpoints.

PATCH /api/me/language stores a BCP-47 tag on the authenticated user's
row. Anonymous callers are rejected with 401 -- their picker state lives
entirely in the ephemera_lang_v1 cookie + localStorage, which the
client-side JS writes before issuing the request. The picker widget in
i18n.js short-circuits the network call when no session is present, so
the 401 path is only taken on malicious or misconfigured callers.
"""
from typing import Optional

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from ..dependencies import current_user_id, verify_same_origin
from ..errors import http_error
from ..i18n import SUPPORTED
from ..models import users as users_model


router = APIRouter()


class LanguagePatch(BaseModel):
    language: Optional[str] = None


@router.patch("/api/me/language", status_code=204)
def patch_language(
    body: LanguagePatch,
    request: Request,
    _origin=Depends(verify_same_origin),
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
