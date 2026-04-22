"""User preference endpoints.

PATCH /api/me/language stores a BCP-47 tag on the authenticated user's
row. For anonymous callers it's a 204 no-op: their picker state lives
in the ephemera_lang_v1 cookie plus localStorage, and the client-side
JS has already set both by the time this fires. The endpoint exists so
the picker widget can call it unconditionally without branching on
login state.
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
    default. Anonymous users get a silent 204 -- their preference is
    already in cookie + localStorage from the picker widget."""
    if body.language is not None and body.language not in SUPPORTED:
        raise http_error(400, "unsupported_language")
    uid = current_user_id(request)
    if uid is not None:
        users_model.set_preferred_language(uid, body.language)
    return Response(status_code=204)
