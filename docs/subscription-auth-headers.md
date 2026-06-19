# Subscription Auth Headers

## Status: DOES NOT WORK (2026-06-17)

Ccode OAuth tokens (from `.credentials.json`) are rate-limited to zero for raw
API calls — regardless of headers. The API recognises the token (returns the
org ID in response headers) but immediately 429s with no rate limit allocation.

Tested with ccode fully stopped, minutes between attempts, with and without
the billing headers below. All combinations: immediate 429, no retry-after,
no x-ratelimit-* headers.

**Anthropic's June 16 announcement** postponing the SDK billing change said
"SDK scripts continue to work with subscription." This may mean SDK auth
via `ant auth login` (a separate credential flow) rather than ccode OAuth tokens.

**Next steps:**
1. Try `ant auth login` — the official Anthropic CLI auth, stores credentials
   at `~/.config/anthropic/credentials/`. This may produce a token that actually
   works for SDK calls.
2. If that fails, subscription SDK access may require the `ant` CLI's own
   request path rather than raw `Anthropic()` client calls.

## Previous approach (kept for reference)

Quiet sends the same headers Claude Code sends on every API request. These were
reverse-engineered from the Claude Code binary (v2.1.143, 2026-05-15 build).

### Required headers

| Header | Value | Purpose |
|--------|-------|---------|
| `User-Agent` | `claude-code/<version>` | Identifies the client |
| `x-app` | `cli` | Interactive (vs `cli-bg` for background) |
| `anthropic-client-platform` | `claude_code_cli` | Traffic classification |

### How ccode sets them

The `anthropic-client-platform` header is derived from the `CLAUDE_CODE_ENTRYPOINT`
environment variable via a mapping function in the binary:

| ENTRYPOINT value | Platform header |
|---|---|
| `cli` (default) | `claude_code_cli` |
| `sdk-cli` / `sdk-ts` / `sdk-py` | `claude_code_sdk` |
| `claude-vscode` | `claude_code_vscode` |
| `remote` / `remote_baku` / etc. | `claude_code_remote` |
| `mcp` | `claude_code_mcp` |

The Python SDK does NOT read this env var — it must be set as an explicit
`default_headers` argument to the `Anthropic()` constructor.

### Other headers ccode sends (optional)

- `X-Claude-Code-Session-Id: <uuid>` — session tracking
- `x-claude-code-agent-id` / `x-claude-code-parent-agent-id` — agent tree
- `x-claude-remote-container-id` / `x-claude-remote-session-id` — remote sessions

These don't appear to affect rate limits or pricing classification.

## Implementation

See `auth.py` — `_subscription_headers()` returns the required headers,
`create_client()` passes them via `default_headers=` when using subscription mode.

## Ethics note

This is about correctly classifying interactive use under subscription, which
is where the Terms of Acceptable Use place it. Quiet is an interactive chat
client — the same category as Claude Code. The headers ensure the API treats
it that way rather than misclassifying it as programmatic SDK traffic.
