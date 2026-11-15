"""Sign-in with Google (OAuth 2.0 / OpenID Connect) and the storefront's own sessions.

Google proves who the shopper is; Pantree keeps the session. One button does both jobs:

* the Google account is already linked, or its verified email belongs to a customer -> sign in;
* otherwise -> a new customer is created from the Google profile (sign-up), and the shopper is
  sent to the profile page to add the rest of their details.

Configuration (environment variables):

* ``GOOGLE_CLIENT_ID`` / ``GOOGLE_CLIENT_SECRET``: an OAuth client ("Web application") in
  Google Cloud with the redirect URI ``<PUBLIC_APP_URL>/auth/google/callback``.
* ``SESSION_SECRET``: signs the short-lived cookie that carries the OAuth ``state``.
* ``APP_ENV=development`` without a Google client enables a local-only "developer sign-in"
  form so the shop can be tried without an OAuth client. It is never available in production.
"""
from __future__ import annotations

import datetime as dt
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

LOCAL_SESSION_SECRET = Path(__file__).resolve().parent / ".local" / "session.key"
SESSION_COOKIE = "sid"
SESSION_DAYS = 7
GOOGLE_METADATA_URL = "https://accounts.google.com/.well-known/openid-configuration"

_oauth = None


def app_env() -> str:
    return os.getenv("APP_ENV", "development").strip().lower()


def is_production() -> bool:
    return app_env() in {"production", "prod"}


def public_url() -> str:
    return os.getenv("PUBLIC_APP_URL", "http://127.0.0.1:8000").rstrip("/")


def secure_cookies() -> bool:
    return urlsplit(public_url()).scheme == "https"


def google_configured() -> bool:
    return bool(os.getenv("GOOGLE_CLIENT_ID") and os.getenv("GOOGLE_CLIENT_SECRET"))


def dev_login_enabled() -> bool:
    """Local-only sign-in without Google: development mode and no OAuth client configured."""
    return app_env() == "development" and not google_configured()


def session_secret() -> str:
    """Key for the signed OAuth-state cookie. Required in production; generated locally."""
    configured = os.getenv("SESSION_SECRET", "")
    if configured:
        if len(configured) < 32:
            raise RuntimeError("SESSION_SECRET must contain at least 32 characters.")
        return configured
    if is_production():
        raise RuntimeError("SESSION_SECRET is required in production.")
    LOCAL_SESSION_SECRET.parent.mkdir(parents=True, exist_ok=True)
    try:
        with LOCAL_SESSION_SECRET.open("x") as key_file:
            key_file.write(secrets.token_urlsafe(48))
    except FileExistsError:
        pass
    return LOCAL_SESSION_SECRET.read_text().strip()


def check_configuration() -> None:
    """Fail fast at start-up when production settings are incomplete."""
    url = urlsplit(public_url())
    if url.scheme not in {"http", "https"} or not url.netloc or url.path not in {"", "/"}:
        raise RuntimeError("PUBLIC_APP_URL must be an HTTP(S) origin without a path.")
    if is_production():
        missing = [name for name in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "SESSION_SECRET")
                   if not os.getenv(name)]
        if missing:
            raise RuntimeError("Production sign-in configuration is incomplete: " + ", ".join(missing) + ".")
        if url.scheme != "https":
            raise RuntimeError("PUBLIC_APP_URL must use HTTPS in production.")
    session_secret()


def oauth():
    """The authlib Google client, or None when Google sign-in is not configured."""
    global _oauth
    if _oauth is None and google_configured():
        from authlib.integrations.starlette_client import OAuth

        client = OAuth()
        client.register(
            "google",
            client_id=os.environ["GOOGLE_CLIENT_ID"],
            client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
            server_metadata_url=GOOGLE_METADATA_URL,
            client_kwargs={"scope": "openid email profile"},
        )
        _oauth = client
    return _oauth


def new_session_id() -> str:
    return secrets.token_urlsafe(32)


def session_expired(created_at: str | None) -> bool:
    if not created_at:
        return True
    try:
        created = dt.datetime.fromisoformat(str(created_at))
    except ValueError:
        return True
    return dt.datetime.now() - created > dt.timedelta(days=SESSION_DAYS)


def device_from_user_agent(user_agent: str | None) -> str:
    """Map the browser to the warehouse's device_type values."""
    agent = (user_agent or "").lower()
    if "mobi" in agent or "android" in agent or "iphone" in agent:
        return "Mobile Web"
    return "Desktop Web"
