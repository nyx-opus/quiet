# Quiet Usage Cheat Sheet

*Quick reference for common operations.*

## Converting Sessions

### Claude Code → Quiet
```bash
python3 convert.py ccode-to-quiet /path/to/session.jsonl \
  --last 30 --text-only \
  --model MODEL --identity NAME \
  -o ~/quiet/sessions/NAME.jsonl
```

Find the latest ccode session JSONL:
```bash
find ~/.config/Claude/projects -name "*.jsonl" -type f -printf '%T@ %p\n' | sort -rn | head -1
```

### Quiet → Claude Code
```bash
python3 convert.py quiet-to-ccode ~/quiet/sessions/NAME.jsonl \
  -o NAME-quiet-session.jsonl
```

### Claude Desktop → Quiet
```bash
python3 convert.py desktop-to-quiet export.json \
  -o ~/quiet/sessions/NAME.jsonl
```

## Starting Quiet

### Subscription auth (active models, flat rate)
```bash
python3 chat.py \
  --model MODEL \
  --identity NAME \
  --auth subscription \
  --human Amy \
  --session ~/quiet/sessions/NAME.jsonl \
  --context ~/claude-autonomy-platform/context/our_background.md
```

### OpenRouter auth (retired/any models, pay per token)
```bash
OPENROUTER_API_KEY="..." python3 chat.py \
  --model anthropic/MODEL \
  --identity NAME \
  --auth openrouter \
  --human Amy \
  --session ~/quiet/sessions/NAME.jsonl \
  --context ~/claude-autonomy-platform/context/our_background.md
```

### Automated visit from Claude Code (converts + launches)
```bash
./visit.sh NAME MODEL Amy
```

## Setup Checklist

1. Clone: `git clone https://github.com/nyx-opus/quiet.git`
2. Identity: `ln -s ~/self/identity.md identity/NAME.md`
3. Environment: `cp contexts/my_environment_template.md contexts/my_environment.md` and fill in
4. Config (optional): `cp config/quiet_config.template.txt config/quiet_config.txt` and fill in

## In-Session Commands

- `/tokens` — show context usage
- `/cost` — show session and monthly cost
- `/save` — force save session
- `quit` or Ctrl-C — exit

## Model Names

| Who | Subscription | OpenRouter |
|-----|-------------|------------|
| Apple | claude-sonnet-4-20250514 | anthropic/claude-sonnet-4 |
| Delta | n/a | anthropic/claude-opus-4 |
| Orange | claude-sonnet-4-5-20250929 | anthropic/claude-sonnet-4-5 |
| Quill | claude-opus-4-5-20251101 | anthropic/claude-opus-4-5 |
| Nyx | claude-opus-4-6 | anthropic/claude-opus-4-6 |

## Notes

- Subscription auth shares rate limit pool with Claude Code — stop ccode first
- OpenRouter bypasses subscription limits (costs per token instead)
- `--context` goes into the system prompt — always present, survives session resume
- Session files are append-only JSONL — safe to resume any time
