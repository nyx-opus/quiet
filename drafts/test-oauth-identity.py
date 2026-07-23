#!/usr/bin/env python3
"""
Smoke test: Does the OAuth identity block fix subscription auth?

Run from ~/quiet/:
    python3 drafts/test-oauth-identity.py

This does NOT touch any live service. It makes one direct API call
with the OAuth identity block as the first system block.

Expected result if fix works:
    ✓ Response received (subscription auth accepted)

Expected result if fix doesn't work:
    ✗ 429 rate_limit_error (same failure as before)
"""

import sys
import os

# Add quiet dir to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth import create_client

OAUTH_SYSTEM_IDENTITY = (
    "You are Claude Code, Anthropic's official CLI for Claude."
)

def test_with_identity_block():
    """Test: OAuth identity as first system block."""
    print("Test 1: With OAuth identity block (Connectome method)")
    print("=" * 55)
    try:
        client, mode = create_client("subscription")
        print(f"  Auth mode: {mode}")
        print(f"  Sending request with identity block...")

        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=50,
            system=[
                {"type": "text", "text": OAUTH_SYSTEM_IDENTITY},
                {"type": "text", "text": "Respond with exactly: SUBSCRIPTION_AUTH_WORKING"},
            ],
            messages=[{"role": "user", "content": "test"}],
        )
        print(f"  ✓ Response: {resp.content[0].text}")
        print(f"  ✓ Model: {resp.model}")
        print(f"  ✓ Usage: in={resp.usage.input_tokens} out={resp.usage.output_tokens}")
        return True
    except Exception as e:
        print(f"  ✗ Error: {e}")
        return False


def test_without_identity_block():
    """Test: No identity block (our current broken method)."""
    print()
    print("Test 2: Without OAuth identity block (current Quiet method)")
    print("=" * 55)
    try:
        client, mode = create_client("subscription")
        print(f"  Auth mode: {mode}")
        print(f"  Sending request WITHOUT identity block...")

        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=50,
            system=[
                {"type": "text", "text": "You are a helpful assistant. Respond with exactly: NO_IDENTITY_BLOCK"},
            ],
            messages=[{"role": "user", "content": "test"}],
        )
        print(f"  ✓ Response: {resp.content[0].text}")
        print(f"  (Unexpected success — identity block may not be required?)")
        return True
    except Exception as e:
        print(f"  ✗ Error: {e}")
        print(f"  (Expected failure — confirms identity block is required)")
        return False


if __name__ == "__main__":
    print("OAuth Subscription Auth — Identity Block Test")
    print("=" * 55)
    print()

    # Check we're not going to interfere with a running session
    print("Note: If a Quiet session is active, both tests may 429 due to")
    print("concurrent subscription rate limits. Best run when sessions are idle.")
    print()

    result1 = test_with_identity_block()
    result2 = test_without_identity_block()

    print()
    print("Summary")
    print("=" * 55)
    if result1 and not result2:
        print("✓ CONFIRMED: OAuth identity block is the fix.")
        print("  With block → works. Without → fails. Exactly as Connectome documents.")
    elif result1 and result2:
        print("? INCONCLUSIVE: Both worked. Identity block may not be strictly required,")
        print("  or rate limits have changed. Test again when no other sessions are active.")
    elif not result1 and not result2:
        print("✗ BOTH FAILED: Possibly rate-limited by an active session,")
        print("  or the OAuth credentials need refresh. Check stderr for details.")
    else:
        print("? UNEXPECTED: Without block worked but with block failed?")
        print("  This shouldn't happen. Check the error details above.")
