"""Dashboard authentication.

The property under test is that security scales with exposure: loopback with no
token stays open (the existing single-user setup), and anything else demands a
credential rather than serving the archive to the network.
"""

from __future__ import annotations

import time

import pytest

from pipeline import dashboard_auth as auth


class Headers:
    """Minimal stand-in for http.client.HTTPMessage."""

    def __init__(self, **pairs):
        self._pairs = {k.replace("_", "-").title(): v for k, v in pairs.items()}

    def get(self, name, default=None):
        return self._pairs.get(name.title(), default)


@pytest.fixture()
def token(monkeypatch):
    value = "s3cret-token-value-long-enough"
    monkeypatch.setattr(auth, "DASHBOARD_TOKEN", value)
    return value


# ── Exposure rules ────────────────────────────────────────────────────


def test_loopback_without_a_token_stays_open(monkeypatch):
    monkeypatch.setattr(auth, "DASHBOARD_TOKEN", "")
    assert auth.enforced("127.0.0.1") is False
    assert auth.authorized("/api/meetings", Headers(), "127.0.0.1") is True


def test_off_loopback_without_a_token_refuses_to_start(monkeypatch):
    monkeypatch.setattr(auth, "DASHBOARD_TOKEN", "")
    with pytest.raises(auth.AuthError, match="Refusing to bind"):
        auth.check_startup("0.0.0.0")
    # Loopback is still fine.
    auth.check_startup("127.0.0.1")


def test_a_configured_token_is_enforced_even_on_loopback(token):
    assert auth.enforced("127.0.0.1") is True
    assert auth.authorized("/api/meetings", Headers(), "127.0.0.1") is False


def test_context_service_paths_need_no_shared_secret_on_loopback(token):
    for path in auth.LOOPBACK_SERVICE_PATHS:
        assert auth.authorized(path, Headers(), "127.0.0.1") is True
        assert auth.authorized(path, Headers(), "0.0.0.0") is False


def test_off_loopback_with_a_token_starts(token):
    auth.check_startup("0.0.0.0")


# ── Credentials ───────────────────────────────────────────────────────


def test_bearer_token_authorizes(token):
    headers = Headers(authorization=f"Bearer {token}")
    assert auth.authorized("/api/meetings", headers, "0.0.0.0") is True


def test_bearer_scheme_is_case_insensitive(token):
    headers = Headers(authorization=f"bearer {token}")
    assert auth.authorized("/api/meetings", headers, "0.0.0.0") is True


def test_a_wrong_token_is_rejected(token):
    headers = Headers(authorization="Bearer not-the-token")
    assert auth.authorized("/api/meetings", headers, "0.0.0.0") is False


def test_session_cookie_authorizes(token):
    headers = Headers(cookie=f"{auth.COOKIE_NAME}={auth.issue_session()}")
    assert auth.authorized("/api/meetings", headers, "0.0.0.0") is True


def test_the_login_route_is_reachable_without_credentials(token):
    for path in ("/login", "/api/login"):
        assert auth.authorized(path, Headers(), "0.0.0.0") is True, path
    # ...but nothing that reads the archive is.
    assert auth.authorized("/api/meetings", Headers(), "0.0.0.0") is False


# ── Session integrity ─────────────────────────────────────────────────


def test_an_expired_session_is_rejected(token):
    stale = auth.issue_session(now=time.time() - auth.SESSION_TTL_SEC - 60)
    assert auth.valid_session(stale) is False


def test_a_session_cannot_be_extended_by_editing_its_expiry(token):
    """The signature covers the expiry, so moving the clock forward invalidates it."""
    value = auth.issue_session()
    _, _, signature = value.partition(".")
    forged = f"{int(time.time()) + 999_999}.{signature}"
    assert auth.valid_session(forged) is False


def test_malformed_session_values_are_rejected(token):
    for value in ("", "garbage", "nodot", ".", "abc.def", f"{int(time.time()) + 600}."):
        assert auth.valid_session(value) is False, value


def test_rotating_the_token_invalidates_existing_sessions(monkeypatch, token):
    value = auth.issue_session()
    assert auth.valid_session(value) is True
    monkeypatch.setattr(auth, "DASHBOARD_TOKEN", "a-completely-different-token")
    assert auth.valid_session(value) is False


def test_a_session_signed_with_another_secret_is_rejected(monkeypatch):
    monkeypatch.setattr(auth, "DASHBOARD_TOKEN", "attacker-chosen")
    forged = auth.issue_session()
    monkeypatch.setattr(auth, "DASHBOARD_TOKEN", "the-real-token")
    assert auth.valid_session(forged) is False


# ── Cookie hardening ──────────────────────────────────────────────────


def test_the_cookie_is_httponly_samesite_and_scoped(token):
    header = auth.session_cookie_header()
    assert "HttpOnly" in header
    assert "SameSite=Strict" in header
    assert "Path=/" in header
    # The token itself must never travel in the cookie.
    assert token not in header


def test_secure_flag_is_opt_in(token):
    assert "Secure" not in auth.session_cookie_header()
    assert "Secure" in auth.session_cookie_header(secure=True)


def test_clearing_the_cookie_expires_it(token):
    assert "Max-Age=0" in auth.clear_cookie_header()


# ── Misc ──────────────────────────────────────────────────────────────


def test_empty_candidates_never_match(monkeypatch):
    monkeypatch.setattr(auth, "DASHBOARD_TOKEN", "")
    assert auth.token_matches("") is False
    assert auth.token_matches(None) is False
    # An unset token must not be satisfiable by sending an empty token.
    assert auth.token_matches("anything") is False


def test_generated_tokens_are_long_and_unique():
    a, b = auth.generate_token(), auth.generate_token()
    assert a != b
    assert len(a) >= 32
