"""Have I Been Pwned k-anonymity lookup.

Used by the admin CLI to reject passwords that already appear in public
breach corpora. k-anonymity: we send only the first 5 hex chars of the
password's SHA-1; the API returns every known suffix matching that
prefix, and we check locally. The plaintext password never leaves the
host.

Network-aware: a timeout or DNS failure returns None so the caller can
degrade to "couldn't check, skipping" rather than blocking password
setup on an offline host. The online behaviour is to reject on a >0
count.
"""

import hashlib
import urllib.error
import urllib.request

_HIBP_RANGE_URL = "https://api.pwnedpasswords.com/range/{}"
_DEFAULT_TIMEOUT = 5.0


def pwned_count(password: str, *, timeout: float = _DEFAULT_TIMEOUT) -> int | None:
    """Return the breach count for `password`. 0 = not seen in any corpus,
    >0 = appeared N times across known breaches, None = the API could not
    be reached."""
    # SHA-1 here is NOT a password-storage primitive; it's the HIBP range
    # API's fixed wire format. Only the first 5 hex chars of the digest
    # leave the process (k-anonymity); the remaining suffix is matched
    # locally. Password storage in this project uses bcrypt (see
    # app/auth/password.py); SHA-1 is never used defensively.
    #
    # CodeQL flags this as `py/weak-cryptographic-algorithm` because its
    # taint analysis sees `password` reach a SHA-1 call and can't tell
    # that the digest is a protocol field rather than a stored credential.
    # The alert is dismissed (false positive) on the Security tab; this
    # comment documents the rationale so any future reviewer reading the
    # code arrives at the same conclusion without having to retrace it.
    encoded = password.encode("utf-8")
    digest = hashlib.sha1(encoded)
    sha1 = digest.hexdigest().upper()
    prefix, suffix = sha1[:5], sha1[5:]
    req = urllib.request.Request(
        _HIBP_RANGE_URL.format(prefix),
        headers={"Add-Padding": "true", "User-Agent": "ephemera-admin"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            body = resp.read().decode("ascii", errors="ignore")
    except (urllib.error.URLError, TimeoutError, OSError):
        return None

    for line in body.splitlines():
        if ":" not in line:
            continue
        sfx, count_str = line.strip().split(":", 1)
        if sfx.upper() == suffix:
            try:
                count = int(count_str)
            except ValueError:
                return None
            return count if count > 0 else 0
    return 0
