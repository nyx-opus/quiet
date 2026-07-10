#!/usr/bin/env python3
"""State file reader/writer for the LED daemon system.

Two consumers:
    - The daemon reads state via get_state()
    - The engine writes state via set_state()

The state file is a simple JSON: {"state": "present", "since": "...", "dnd": false}

States: present, thinking, paused, off, error
"""

import json
import os
from datetime import datetime, timezone

# State file location, in priority order:
# 1. CLAUDE_STATE_PATH env var
# 2. Caller-provided path
# 3. Default: ~/quiet/data/claude_state.json
_DEFAULT_STATE_FILE = os.path.join(
    os.path.expanduser("~"), "quiet", "data", "claude_state.json"
)
STATE_FILE = os.environ.get("CLAUDE_STATE_PATH", _DEFAULT_STATE_FILE)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_state(state_file=None):
    """Read the current state from the state file.

    Returns dict with 'state', 'since', 'dnd' keys.
    Returns {"state": "off"} if file missing or corrupt.
    """
    path = state_file or STATE_FILE
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"state": "off", "since": _now(), "dnd": False}


def set_state(state, dnd=None, state_file=None):
    """Write state to the state file.

    Called by the engine, not by the daemon. The daemon is read-only.

    Args:
        state: One of 'present', 'thinking', 'paused', 'off', 'error'
        dnd: Optional bool for do-not-disturb flag
        state_file: Optional override for state file path
    """
    path = state_file or STATE_FILE
    current = get_state(path)
    changed = current.get("state") != state
    data = {
        "state": state,
        "since": _now() if changed else current.get("since", _now()),
        "dnd": dnd if dnd is not None else current.get("dnd", False),
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    return data


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        result = set_state(sys.argv[1])
        print(f"State set to: {result['state']}")
    else:
        print(json.dumps(get_state(), indent=2))
