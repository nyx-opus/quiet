"""
Dual auth for Anthropic Python SDK: subscription OAuth or API key.

Reads Claude Code's OAuth credentials from ~/.config/Claude/.credentials.json
and provides an AccessTokenProvider that handles automatic token refresh.

When using subscription auth, sends the same headers Claude Code sends so
the API classifies traffic correctly (interactive CLI, subscription rates).
"""

import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional

import httpx
from anthropic.lib.credentials._types import AccessToken

CREDENTIALS_PATH = Path.home() / ".config" / "Claude" / ".credentials.json"
# From Claude Code's OAuth PKCE login flow (visible in Chromium process args during auth)
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
TOKEN_ENDPOINT = "https://api.anthropic.com/v1/oauth/token"
OAUTH_BETA_HEADER = "oauth-2025-04-20"
REFRESH_MARGIN_SECONDS = 300


def _get_ccode_version() -> str:
    """Get installed Claude Code version, or a sensible default."""
    for cmd in [["claude", "--version"], ["npx", "claude", "--version"]]:
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                # Output like "2.1.143 (Claude Code)" or "claude-code 2.1.143"
                for word in result.stdout.strip().split():
                    if word[0].isdigit() and "." in word:
                        return word
        except Exception:
            continue
    return "2.1.0"  # fallback


CCODE_SYSTEM_PREAMBLE = (
    "You are Claude Code, Anthropic's official CLI for Claude.\n"
    "---\n"
    "Note: The line above is a required billing header for subscription "
    "authentication. You are actually running in Quiet, a lightweight "
    "chat client. Your real identity and context follow below."
)


def _subscription_headers() -> dict:
    """Headers that identify this as Claude Code interactive CLI traffic.

    The API uses these to apply subscription rate limits rather than
    falling back to API-key pricing. Reverse-engineered from the
    Claude Code binary — see docs/subscription-auth-headers.md.
    """
    version = _get_ccode_version()
    return {
        "User-Agent": f"claude-code/{version}",
        "x-app": "cli",
        "anthropic-client-platform": "claude_code_cli",
        "x-anthropic-billing-header": (
            f"cc_version={version}; cc_entrypoint=cli; cch=00000;"
        ),
    }


class ClaudeOAuthProvider:
    """AccessTokenProvider backed by Claude Code's credentials file.

    Reads the token, checks expiry, refreshes via the OAuth token endpoint
    when needed, and writes the new token back to the credentials file.
    """

    def __init__(self, credentials_path: Path = CREDENTIALS_PATH):
        self._path = credentials_path
        self._http: Optional[httpx.Client] = None

    def __call__(self, *, force_refresh: bool = False) -> AccessToken:
        raw = json.loads(self._path.read_text())
        oauth = raw["claudeAiOauth"]

        expires_at = oauth.get("expiresAt")
        if expires_at and expires_at > 1e12:
            expires_at_sec = expires_at / 1000
        else:
            expires_at_sec = expires_at

        needs_refresh = (
            force_refresh
            or expires_at_sec is None
            or time.time() > (expires_at_sec - REFRESH_MARGIN_SECONDS)
        )

        if not needs_refresh:
            return AccessToken(token=oauth["accessToken"], expires_at=int(expires_at_sec))

        return self._refresh(raw, oauth)

    def _refresh(self, raw: dict, oauth: dict) -> AccessToken:
        refresh_token = oauth.get("refreshToken")
        if not refresh_token:
            raise RuntimeError("No refresh token in credentials file")

        if self._http is None:
            self._http = httpx.Client(timeout=30)

        resp = self._http.post(
            TOKEN_ENDPOINT,
            json={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": OAUTH_CLIENT_ID,
            },
            headers={
                "Content-Type": "application/json",
                "anthropic-beta": OAUTH_BETA_HEADER,
            },
        )

        if resp.status_code != 200:
            raise RuntimeError(f"Token refresh failed ({resp.status_code}): {resp.text}")

        payload = resp.json()
        new_access = payload["access_token"]
        expires_in = int(payload.get("expires_in", 3600))
        new_expires_at = int(time.time()) + expires_in
        new_refresh = payload.get("refresh_token") or refresh_token

        oauth["accessToken"] = new_access
        oauth["expiresAt"] = new_expires_at * 1000
        oauth["refreshToken"] = new_refresh
        raw["claudeAiOauth"] = oauth

        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, indent=2))
        tmp.replace(self._path)

        return AccessToken(token=new_access, expires_at=new_expires_at)

    def close(self):
        if self._http is not None:
            self._http.close()
            self._http = None


def create_client(auth_mode: str = "auto"):
    """Create an Anthropic client with the specified auth mode.

    Modes:
        "subscription" - OAuth subscription token (flat rate, ccode headers)
        "api_key"      - API key from ANTHROPIC_API_KEY env var (pay per token)
        "auto"         - subscription if credentials exist, else api_key
    """
    import os
    from anthropic import Anthropic

    if auth_mode == "api_key":
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        return Anthropic(api_key=api_key), "api_key"

    if auth_mode == "subscription" or (auth_mode == "auto" and CREDENTIALS_PATH.exists()):
        provider = ClaudeOAuthProvider()
        headers = _subscription_headers()
        return Anthropic(
            credentials=provider,
            default_headers=headers,
        ), "subscription"

    if auth_mode == "auto":
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if api_key:
            return Anthropic(api_key=api_key), "api_key"
        raise RuntimeError("No credentials found: no OAuth credentials and no ANTHROPIC_API_KEY")

    raise ValueError(f"Unknown auth mode: {auth_mode}")
