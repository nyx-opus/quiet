# Subscription Auth Headers

## Problem

Using subscription OAuth tokens (from Claude Code's `.credentials.json`) to
make API calls works for authentication, but without the right headers the API:
- Applies severe rate limits
- Falls back to API pricing after limited usage

The subscription entitlement is on the token, but the API also checks client
metadata to classify traffic as "interactive CLI" vs "programmatic SDK".

## Solution

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
