"""Security-headers middleware + the constants that drive it.

Lifted out of `app/__init__.py` so the policy is reviewable in isolation
from the create_app plumbing. The headers and the middleware that
attaches them are byte-identical to the previous inline shape; the
test_security.py cross-route invariant test pins this at the test level.

The middleware is the unconditional authority on these headers: any
future route that tried to set a conflicting value (say, a looser CSP
for a debug endpoint) gets silently overwritten here rather than
quietly winning. That enforces the "deny by default, uniformly"
stance at the structural level.
"""

from fastapi import Request

# CSP: deny-by-default then explicitly enumerate what ephemera actually uses.
# Nothing is fetched cross-origin (no CDN, no web fonts, no analytics). The
# two non-'self' sources are (1) `data:` images for the reveal payload and
# the inline SVG chevrons in forms.css/chrome.css, and (2) base-uri/form-action pinned
# to 'self' to blunt <base href> and form-repoint attacks.
CSP = "; ".join(
    [
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "font-src 'self'",
        "manifest-src 'self'",
        "frame-ancestors 'none'",
        "form-action 'self'",
        "base-uri 'self'",
        "object-src 'none'",
    ]
)

# Camera/mic/geo/payment/USB/sensors aren't used anywhere. An empty allow-list
# is the cheapest defence-in-depth against a future regression that quietly
# adds such an API.
PERMISSIONS_POLICY = ", ".join(
    [
        "camera=()",
        "microphone=()",
        "geolocation=()",
        "payment=()",
        "usb=()",
        "accelerometer=()",
        "gyroscope=()",
        "magnetometer=()",
        "interest-cohort=()",
    ]
)

SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": PERMISSIONS_POLICY,
    # Start conservative -- HSTS is sticky, so if the cert ever breaks, browsers
    # that saw a long max-age will refuse HTTP for that long. Once the deployment
    # has been stable through at least one Let's Encrypt renewal, bump this to
    # `max-age=31536000; includeSubDomains; preload` and consider HSTS preload.
    "Strict-Transport-Security": "max-age=86400",
}


async def add_security_headers(request: Request, call_next):
    """ASGI middleware that stamps every response with the project's
    security-headers dict.

    Registered via `app.middleware("http")(add_security_headers)` in
    create_app. Unconditional set is deliberate: a conflicting value set
    by a route handler gets silently overwritten here. The cross-route
    invariant test in tests/test_security.py pins the contract at the
    test level."""
    response = await call_next(request)
    for k, v in SECURITY_HEADERS.items():
        response.headers[k] = v
    return response
