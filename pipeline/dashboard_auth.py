"""Authentication for the local dashboard.

The dashboard serves minutes, Drive links, audio snippets and
`DELETE /api/meetings/{id}` with no authentication at all. That was survivable
only because it binds to 127.0.0.1 - the bind address was the entire security
boundary. `docs/VOICE_LABELLING_PLAN.md` stage 00 names this as the blocker for
phone access, and it is right to: the moment the dashboard is reachable over
Tailscale or a LAN, an unauthenticated delete endpoint is a real problem.

The rule here is that **security scales with exposure**:

- Bound to loopback with no token configured -> open, exactly as before. This is
  the existing single-user workflow and breaking it would buy nothing.
- Bound to anything else -> a token is mandatory, and the server refuses to start
  without one rather than quietly serving the archive to the network.
- Token configured -> always enforced, loopback included.

Two ways to present the token, because there are two kinds of caller. A browser
POSTs it once to `/api/login` and gets an HMAC-signed cookie; a script sends
`Authorization: Bearer <token>`. Nothing stores the token itself in the cookie,
so a stolen cookie expires on its own and cannot be replayed forever.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from http.cookies import SimpleCookie

from pipeline.config import DASHBOARD_TOKEN

COOKIE_NAME = "mmc_session"
# Long enough not to nag a single user through a working day, short enough that a
# leaked cookie stops working without any server-side revocation list.
SESSION_TTL_SEC = 12 * 60 * 60

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}

# Reachable without a session, or there would be no way to obtain one. Deliberately
# tiny: the login page and the assets it needs to render, nothing that reads the
# archive.
PUBLIC_PATHS = {"/login", "/api/login", "/style.css", "/favicon.ico"}
# Machine-to-machine context carries no raw transcript and is independently
# rejected by DashboardHandler when the server is not loopback-bound. Keeping
# these two paths local-service accessible avoids copying the dashboard secret
# into Product Manager, which would collapse the repositories' security seam.
LOOPBACK_SERVICE_PATHS = {"/api/context/search", "/api/context/health"}


class AuthError(RuntimeError):
    """Configuration that would expose the archive; raised at startup, not per request."""


def _secret() -> bytes:
    """Key for signing session cookies.

    Derived from the token rather than stored separately, so there is exactly one
    secret to manage. A rotated token therefore invalidates every existing
    session, which is the behaviour you want from rotating a credential.
    """
    return hashlib.sha256(("mmc-session-v1:" + DASHBOARD_TOKEN).encode("utf-8")).digest()


def is_loopback(host: str | None) -> bool:
    return (host or "").strip().lower() in LOOPBACK_HOSTS


def token_configured() -> bool:
    return bool(DASHBOARD_TOKEN)


def enforced(bind_host: str | None) -> bool:
    """Whether requests must carry credentials."""
    return token_configured() or not is_loopback(bind_host)


def check_startup(bind_host: str | None) -> None:
    """Refuse to serve the archive to a network without a credential.

    Failing at startup rather than per request: a dashboard that binds to 0.0.0.0
    and *then* rejects everything is a confusing outage, while one that binds and
    serves everything is a data leak. Neither is as good as not starting.
    """
    if not is_loopback(bind_host) and not token_configured():
        raise AuthError(
            f"Refusing to bind to {bind_host} without authentication. "
            "The dashboard exposes meeting minutes and DELETE /api/meetings/{id}. "
            "Set MMC_DASHBOARD_TOKEN in .env (any long random string; "
            '`python -c "import secrets;print(secrets.token_urlsafe(32))"` '
            "generates one), or bind to 127.0.0.1."
        )


def issue_session(now: float | None = None) -> str:
    """A signed, self-expiring cookie value: `<expiry>.<hmac>`."""
    expiry = int((now if now is not None else time.time()) + SESSION_TTL_SEC)
    payload = str(expiry).encode("ascii")
    signature = hmac.new(_secret(), payload, hashlib.sha256).digest()
    return f"{expiry}.{base64.urlsafe_b64encode(signature).decode('ascii').rstrip('=')}"


def valid_session(value: str | None, now: float | None = None) -> bool:
    if not value or "." not in value:
        return False
    expiry_text, _, signature = value.partition(".")
    if not expiry_text.isdigit():
        return False
    # Verify the signature before trusting the expiry, so an attacker cannot
    # extend a session just by editing the number in front of it.
    expected = issue_session_signature(int(expiry_text))
    if not hmac.compare_digest(signature, expected):
        return False
    return int(expiry_text) > (now if now is not None else time.time())


def issue_session_signature(expiry: int) -> str:
    signature = hmac.new(_secret(), str(expiry).encode("ascii"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")


def token_matches(candidate: str | None) -> bool:
    """Compare in constant time; a length-dependent early exit leaks the token."""
    if not candidate or not DASHBOARD_TOKEN:
        return False
    return hmac.compare_digest(candidate.strip(), DASHBOARD_TOKEN)


def _bearer(headers) -> str | None:
    raw = headers.get("Authorization") or ""
    prefix = "bearer "
    return raw[len(prefix) :].strip() if raw.lower().startswith(prefix) else None


def _cookie(headers) -> str | None:
    raw = headers.get("Cookie")
    if not raw:
        return None
    jar = SimpleCookie()
    try:
        jar.load(raw)
    except Exception:
        return None
    morsel = jar.get(COOKIE_NAME)
    return morsel.value if morsel else None


def authorized(path: str, headers, bind_host: str | None) -> bool:
    """Whether this request may proceed."""
    if path in LOOPBACK_SERVICE_PATHS and is_loopback(bind_host):
        return True
    if not enforced(bind_host):
        return True
    if path in PUBLIC_PATHS:
        return True
    # A script may present the token directly; a browser presents its session.
    return token_matches(_bearer(headers)) or valid_session(_cookie(headers))


def session_cookie_header(secure: bool = False) -> str:
    """Set-Cookie value for a fresh session.

    HttpOnly so page scripts cannot read it, SameSite=Strict so another site
    cannot ride it, Path=/ so it covers the API as well as the page.
    """
    parts = [
        f"{COOKIE_NAME}={issue_session()}",
        "Path=/",
        "HttpOnly",
        "SameSite=Strict",
        f"Max-Age={SESSION_TTL_SEC}",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def clear_cookie_header() -> str:
    return f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"


def generate_token() -> str:
    """A token worth using, for the message that tells the user to set one."""
    return secrets.token_urlsafe(32)


def describe() -> str:
    """One line for `pipeline doctor`."""
    if token_configured():
        return f"token set ({len(DASHBOARD_TOKEN)} chars); sessions expire after 12h"
    if os.environ.get("MMC_DASHBOARD_HOST", "127.0.0.1") not in LOOPBACK_HOSTS:
        return "NO TOKEN and bound off-loopback - the dashboard will refuse to start"
    return "no token; open on loopback only, which is the default single-user setup"
