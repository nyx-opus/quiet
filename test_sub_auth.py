#!/usr/bin/env python3
"""Test subscription auth — run AFTER stopping Claude Code.

    python3 ~/quiet/test_sub_auth.py
"""

import json
import time
from pathlib import Path

import httpx

# ── Test 1: Borrowed ccode OAuth token (what we've been doing) ──

from auth import ClaudeOAuthProvider, _subscription_headers

provider = ClaudeOAuthProvider()
token_info = provider()
print(f"ccode OAuth token OK (expires in {token_info.expires_at - int(time.time())}s)")
print()

BODY = {
    "model": "claude-sonnet-4-5-20250929",
    "max_tokens": 30,
    "messages": [{"role": "user", "content": "Say hello in exactly 5 words."}],
}


def try_request(label, headers):
    print(f"--- {label} ---")
    resp = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers=headers,
        json=BODY,
        timeout=30,
    )
    print(f"Status: {resp.status_code}")
    if resp.status_code == 200:
        data = resp.json()
        print(f"Response: {data['content'][0]['text']}")
    else:
        print(f"Body: {resp.text[:500]}")
        # Print ALL response headers for debugging
        print("Response headers:")
        for k, v in resp.headers.items():
            print(f"  {k}: {v}")
    print()


base_headers = {
    "Authorization": f"Bearer {token_info.token}",
    "Content-Type": "application/json",
    "anthropic-version": "2023-06-01",
}

# 1a: ccode token, no special headers
try_request("ccode token, PLAIN", base_headers.copy())

time.sleep(2)

# 1b: ccode token, with billing headers
h = base_headers.copy()
h.update(_subscription_headers())
try_request("ccode token, WITH ccode headers", h)

# ── Test 2: ant CLI credentials (if available) ──

ant_creds = Path.home() / ".config" / "anthropic" / "credentials" / "default.json"
if ant_creds.exists():
    print("=" * 60)
    print("Found ant CLI credentials!")
    creds = json.loads(ant_creds.read_text())
    ant_token = creds.get("access_token") or creds.get("token", "")
    if ant_token:
        ant_headers = {
            "Authorization": f"Bearer {ant_token}",
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        time.sleep(2)
        try_request("ant CLI token, PLAIN", ant_headers)
    else:
        print(f"Credential keys: {list(creds.keys())}")
        print("Couldn't find token field — check format")
else:
    print("=" * 60)
    print("No ant CLI credentials found.")
    print("To set up: pip install anthropic-cli && ant auth login")
    print("This gives SDK-native auth separate from Claude Code.")

print()
print("Done!")
