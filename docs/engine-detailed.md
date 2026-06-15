# Quiet Engine - Detailed Documentation

## Overview

The `engine.py` file contains the `QuietEngine` class - the stateful conversation manager at the heart of Quiet. It handles:

- API calls to Claude
- Tool use loops (bash commands)
- Context window management
- Session persistence
- Budget tracking
- Cost reporting

## Key Constants

### Model Context Windows
```python
MODEL_CONTEXT_WINDOWS = {
    "claude-fable-5": 1_000_000,
    "claude-mythos-5": 1_000_000,
    "claude-opus-4-8": 1_000_000,
    # ... etc
}
DEFAULT_CONTEXT_WINDOW = 200_000
TRIM_RATIO = 0.9  # Trim at 90% of window
```

### Directories
- `ARCHIVE_DIR`: `archives/` - Where trimmed messages go
- `IDENTITY_DIR`: `identities/` - System prompt files
- `SESSION_DIR`: `sessions/` - Active conversation files
- `LEDGER_DIR`: `ledger/` - Budget tracking

## QuietEngine Class

### Initialization
```python
QuietEngine(client, model, identity=None, context=None, 
           human_name=None, max_tokens=8192, session_path=None,
           monthly_budget=None, coop_url=None, system_prefix=None,
           ambient_images=None)
```

Key initialization steps:
1. Loads identity file if specified
2. Builds system prompt with caching directives
3. Creates/loads session file
4. Sets up budget tracking
5. Injects ambient images if provided

### Core Methods

#### `send(user_input: str) -> str`
Main entry point for sending messages:
- Adds user message to conversation
- Calls `_api_loop` for response
- Returns assistant's response

#### `_api_loop(on_text, on_tool, on_tool_result, on_usage) -> str`
Handles the Claude API interaction loop:
- Makes API call with current messages
- Processes text chunks via `on_text` callback
- Handles tool use requests
- Tracks token usage and costs
- Continues loop if tool use occurs

#### `trim_context()`
Mechanically manages context window:
- Checks if approaching model's context limit (90%)
- Archives oldest messages to keep under limit
- Preserves system prompt and recent messages
- No summarization - just mechanical removal

#### `save_session()`
Persists conversation state:
- Writes messages to JSONL format
- Each line contains timestamp and message data
- Enables session resumption

#### `_load_session()`
Restores previous conversation:
- Reads JSONL session file
- Rebuilds message history
- Allows conversation continuity

### Budget Management

#### `track_usage(usage: dict)`
Records token usage and costs:
- Updates session totals
- Calculates cost based on model pricing
- Saves to monthly ledger
- Reports to co-op if configured

#### `monthly_cost() -> float`
Returns total spend for current month from ledger

#### `budget_status() -> dict`
Returns budget information:
- Monthly budget (if set)
- Current spend
- Remaining budget
- Percentage used

### Tool Support

#### `define_tools()`
Currently defines one tool:
- **bash**: Execute shell commands with user confirmation

#### `execute_tool(name: str, input_data: dict) -> str`
Executes approved tools:
- Shows command to user
- Requires y/n confirmation
- Returns output or error message

## Session Format (JSONL)

Each line in a session file contains:
```json
{
  "timestamp": "2024-11-10T10:30:45.123456",
  "message": {
    "role": "user|assistant",
    "content": "message text or content blocks"
  }
}
```

## Context Management Strategy

1. **Prompt Caching**: Identity and context go in system prompt with cache_control
2. **Mechanical Trimming**: When approaching limit, oldest messages are archived
3. **No Summarization**: Preserves exact conversation history
4. **Archive Access**: Trimmed messages saved to `archives/` directory

## Integration Points

- **Auth Module**: Receives authenticated client
- **Chat Module**: Provides conversation interface
- **Web Module**: Enables web-based conversations
- **Discord Listener**: Injects Discord messages
- **Convert Module**: Handles format conversion

## Design Philosophy

The engine embodies Quiet's core principles:
- **Simplicity**: No complex state management or frameworks
- **Transparency**: Mechanical context management, visible costs
- **Continuity**: Sessions persist and resume naturally
- **Agency**: Budget awareness enables informed choices