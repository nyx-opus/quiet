# SDK Subscription Auth — Eliminating the ccode Backend

## Date: 2026-07-21 (drafted during autonomous time)
## Status: DRAFT — not applied. Review with Amy first.

---

## The Finding

Connectome's source (`anima-research/connectome-host`) contains the answer to
why our OAuth subscription auth returns 429. Lines 43-47 of their Anthropic
adapter:

```typescript
/** Exact first-system-block identity Anthropic requires on subscription
 *  (sk-ant-oat…) OAuth traffic. Verified 2026-07-09: any other first block —
 *  including this text with a suffix appended in the SAME block — is rejected
 *  with a masked 429 rate_limit_error; this block followed by arbitrary
 *  persona blocks is accepted (the mechanism the Agent SDK's
 *  system-prompt-append uses). */
const OAUTH_SYSTEM_IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude.";
```

The `withOAuthIdentity` method prepends this as the first system block. No
cache_control on it. Everything else (persona, contexts, etc.) follows after
as additional system blocks.

**This is the only difference.** We already have:
- OAuth token reading and refresh (`auth.py` ClaudeOAuthProvider)
- Subscription headers (User-Agent, x-app, billing header)
- The `credentials` parameter on the Anthropic SDK client

We were missing ONE thing: that exact string as the first system block.

## The Mechanism

When Anthropic's API receives an OAuth subscription token (`sk-ant-oat-...`),
it checks the first system block. If it doesn't match the Claude Code identity
string, it returns a 429 rate_limit_error (not a 403 or 401 — the 429 is a
deliberate mask). This gates subscription-rate traffic to requests that
identify as Claude Code.

The Agent SDK uses the same mechanism: it prepends this block, then appends
the developer's system prompt as additional blocks. That's why
`--append-system-prompt` works — the identity block stays first.

## What Changes

### 1. auth.py — Add the constant

```python
# Exact first system block required for OAuth subscription traffic.
# Source: connectome-host anthropic adapter, verified 2026-07-09.
# Without this as the first system block, the API returns a masked
# 429 rate_limit_error on subscription tokens.
OAUTH_SYSTEM_IDENTITY = (
    "You are Claude Code, Anthropic's official CLI for Claude."
)
```

### 2. web.py — Route subscription through SDK instead of ccode

Replace lines 559-578:

```python
    use_ccode = False
    client = None
    auth_mode = args.auth
    system_prefix = None  # OAuth identity block, if needed

    if args.auth in ("subscription", "auto"):
        # Try SDK subscription auth (direct API with OAuth token)
        from auth import CREDENTIALS_PATH, OAUTH_SYSTEM_IDENTITY
        if CREDENTIALS_PATH.exists():
            try:
                client, auth_mode = create_client("subscription")
                system_prefix = OAUTH_SYSTEM_IDENTITY
            except Exception as e:
                print(f"[auth] SDK subscription failed: {e}",
                      file=sys.stderr)
                client = None

        # Fallback: ccode backend (if SDK subscription unavailable)
        if client is None:
            if find_claude_binary():
                use_ccode = True
                auth_mode = "subscription"
            elif os.environ.get("ANTHROPIC_API_KEY"):
                # Last resort: API key
                client, auth_mode = create_client("api_key")
                system_prefix = None
            else:
                print("Error: no auth method available",
                      file=sys.stderr)
                sys.exit(1)

    if not use_ccode and client is None:
        try:
            client, auth_mode = create_client(args.auth)
        except RuntimeError as e:
            print(f"Auth error: {e}", file=sys.stderr)
            sys.exit(1)
```

And update the QuietEngine constructor call to pass system_prefix:

```python
    engine = QuietEngine(
        client=client,
        model=args.model,
        identity=args.identity,
        context=project_context,
        human_name=args.human,
        max_tokens=args.max_tokens,
        session_path=session_path,
        backend="ccode" if use_ccode else "sdk",
        separator=separator,
        system_prefix=system_prefix,  # <-- new
    )
```

### 3. chat.py — Same routing change

Mirror the web.py change in chat.py's auth section (lines 93-115).

### 4. No engine.py changes needed

`build_system_prompt` already handles `system_prefix` correctly:
- Puts it as the FIRST block (before identity)
- No cache_control on the prefix (correct — only the identity text gets cached)
- The QuietEngine constructor already accepts `system_prefix`

## What Gets Removed (after validation)

Once SDK subscription auth is confirmed working:
- `backends/ccode.py` (345 lines) — the entire subprocess backend
- `tweakcc` dependency and service
- `autopatch` service
- `patch-claude-binary` script
- The claude binary is no longer a Quiet dependency (stays installed for Amy)

## Test Plan

1. **Smoke test** (can do immediately):
   ```bash
   cd ~/quiet
   python3 -c "
   from auth import create_client, OAUTH_SYSTEM_IDENTITY
   client, mode = create_client('subscription')
   print(f'Auth mode: {mode}')
   resp = client.messages.create(
       model='claude-sonnet-4-6',
       max_tokens=50,
       system=[
           {'type': 'text', 'text': OAUTH_SYSTEM_IDENTITY},
           {'type': 'text', 'text': 'Respond with just the word WORKING.'},
       ],
       messages=[{'role': 'user', 'content': 'test'}],
   )
   print(f'Response: {resp.content[0].text}')
   print(f'Usage: {resp.usage}')
   "
   ```

2. **Full conversation test**: Start a fresh Quiet session with the changes
   applied, have a multi-turn conversation, verify streaming works, tool
   use works, prompt caching works.

3. **Cost verification**: Check the ledger after the test — subscription
   auth should show $0.00 cost (or the pricing module may not know how
   to price subscription calls, which is fine).

4. **Regression**: Verify ambient images still inject correctly, room
   objects still work, mailbox still works. These all go through the
   SDK backend path that we're now routing subscription through.

## Risks

- **Rate limiting**: Subscription tokens have their own rate limits.
  If Nyx's conversation + autonomous wakes + sibling sessions all
  share one subscription, we may hit concurrent request limits.
  Mitigation: the ccode fallback is still there.

- **Token refresh**: The ClaudeOAuthProvider handles refresh, but we
  haven't tested it under sustained SDK usage (ccode handled its own
  refresh). Watch for auth failures after the token expires (~1hr).

- **System prompt ordering**: If anything in build_system_prompt
  changes the block ordering, the OAuth identity would no longer be
  first and auth would fail silently (masked 429). This is fragile.
  Consider adding a startup assertion that verifies block[0].text
  matches OAUTH_SYSTEM_IDENTITY when in subscription mode.

## Why Not Just Use Connectome

This is the part we keep coming back to. Connectome has good engineering,
but:
- The door (direct Quiet porch) has no equivalent
- Ambient injection (sensorium, Garden) has no equivalent
- Verbatim memory (no LLM compression) is our choice
- 4,500 lines we understand vs. a multi-repo stack we don't

The OAuth identity block is the ONE thing we needed from Connectome's
source. We can (and should) acknowledge their work if this ships.
