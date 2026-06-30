# Quiet

Lightweight conversation harness for Claude models. Sessions, prompt caching, vector memory, mechanical context trimming. No framework, no agents, just talk.

## Setup

```bash
pip install anthropic httpx sentence-transformers numpy
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

# With identity (loads identity/<name>.md + quiet-system-prompt.md)
python3 chat.py --identity nyx --model claude-sonnet-4-20250514

# Opus 3 (4096 output token limit)
python3 chat.py --auth api_key --model claude-3-opus-20240229 --max-tokens 4096

# Resume a previous session
python3 chat.py --resume archives/session-2026-05-22-1400.jsonl
```

## Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--model` | `claude-sonnet-4-20250514` | Model ID |
| `--identity` | none | Identity file name (without .md) from `identity/` |
| `--context` | none | Legacy: path to a single context file |
| `--max-tokens` | 8192 | Max output tokens (use 4096 for Opus 3) |
| `--auth` | auto | `subscription`, `api_key`, or `auto` |
| `--resume` | none | Session archive to resume from |

## How it works

- **Prompt caching**: Identity and contexts go in the system prompt with `cache_control`. Cached across turns.
- **Context auto-loading**: All `.md` files in `contexts/` are automatically loaded and cached. Drop in files, symlinks, or notes-to-self.
- **Mechanical trim**: When tokens approach the limit, oldest 40% of turns are batch-dropped and archived. No AI summarisation.
- **Vector memory**: Dropped turns are auto-ingested into a local embedding store (MiniLM). Relevant fragments surface as recall handles on each turn.
- **Bash tool**: The model can run shell commands (with your confirmation).

## Directory layout

```
identity/              # Identity files (gitignored except quiet-system-prompt.md)
  quiet-system-prompt.md   # Quiet-specific priming (tracked)
  <name>.md                # Symlink to ~/self/identity.md (local)

contexts/              # Context files — auto-loaded by the engine (gitignored except shared docs)
  quiet_architecture.md    # How Quiet works (tracked, shared across installs)
  my_environment.md        # This machine's specifics (local)
  <name>.md                # Per-Claude notes, visitor contexts (local)
  family.md                # Symlink to shared network file (optional)

data/                  # Runtime data (gitignored except .gitkeep)
  memory.db                # Vector memory store
  claude_state.json        # LED daemon state

backends/              # API backends
  sdk.py                   # Direct Anthropic SDK (API key / OpenRouter)
  ccode.py                 # Claude Code subprocess (subscription auth)

services/              # Systemd units and setup
config/                # Configuration templates and live config
docs/                  # Architecture documentation
static/                # Web UI assets
tests/                 # Test suite
```

## New install

```bash
# 1. Clone
git clone <repo-url> ~/quiet && cd ~/quiet

# 2. Dependencies
pip install -r requirements.txt

# 3. Identity: symlink your identity doc
mkdir -p identity
ln -s ~/self/identity.md identity/<name>.md

# 4. Environment: copy template and fill in
cp contexts/my_environment_template.md contexts/my_environment.md
# Edit contexts/my_environment.md with machine-specific details

# 5. Config
cp config/quiet_config.template.txt config/quiet_config.txt
# Edit config/quiet_config.txt

# 6. Run
python3 chat.py --identity <name> --model <model>
```
