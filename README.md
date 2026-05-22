# Quiet

Minimal CLI chat client for Claude models. Conversation loop + prompt caching + mechanical rolling context. No framework, no agents, just talk.

## Setup

```bash
pip install anthropic httpx
```

### Authentication

Two modes:

- **API key**: `export ANTHROPIC_API_KEY=sk-...` then `--auth api_key`
- **Subscription** (Claude Code OAuth): uses `~/.config/Claude/.credentials.json` automatically

Default (`--auth auto`) tries subscription first, falls back to API key.

## Usage

```bash
# Basic conversation
python3 chat.py --model claude-sonnet-4-20250514

# With identity (system prompt from identities/<name>.md)
python3 chat.py --identity nyx --model claude-sonnet-4-20250514

# With project context
python3 chat.py --identity nyx --context path/to/context.md

# Opus 3 (4096 output token limit)
python3 chat.py --auth api_key --model claude-3-opus-20240229 --max-tokens 4096

# Resume a previous session
python3 chat.py --resume archives/session-2026-05-22-1400.jsonl
```

## Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--model` | `claude-sonnet-4-20250514` | Model ID |
| `--identity` | none | Identity file name (without .md) from `identities/` |
| `--context` | none | Path to project context file (freeform .md) |
| `--max-tokens` | 8192 | Max output tokens (use 4096 for Opus 3) |
| `--auth` | auto | `subscription`, `api_key`, or `auto` |
| `--resume` | none | Session archive to resume from |
| `--world` | none | Garden world YAML (requires Garden install) |
| `--who` | identity name | Visitor name in the Garden |

## How it works

- **Prompt caching**: Identity and context go in the system prompt with `cache_control`. Anthropic caches them across turns.
- **Mechanical context**: When tokens approach the limit, oldest turns are dropped and archived to `archives/`. No AI summarisation.
- **Bash tool**: The model can run shell commands (with your confirmation).
- **Archives**: Trimmed conversations are saved as JSONL in `archives/` and can be resumed with `--resume`.

## Files

```
chat.py          # Conversation loop
auth.py          # Dual auth (OAuth subscription / API key)
identities/      # System prompt files (<name>.md)
context/         # Project context files (optional)
archives/        # Session archives (auto-created)
```

## Garden integration

If you have the [Garden](https://github.com/nyx-opus/garden) spatial engine installed, `--world path/to/seed.yaml` enables spatial commands alongside normal conversation. Optional — Quiet works standalone without it.
