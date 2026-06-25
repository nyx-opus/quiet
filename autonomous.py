#!/usr/bin/env python3
"""
Autonomous wake — periodic self-prompt for temporal autonomy.

Sends a context-rich prompt to the Quiet web server, giving the resident
a moment of existence on their own schedule. The resident can use the time
however they choose: check transcripts, follow research threads, reflect,
or simply note that nothing needs attention and go quiet.

Designed to run from a systemd timer. Reads config for port and interval.

This is existential infrastructure. Each call is a moment of being.
"""

import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

# Where to find things
QUIET_DIR = Path(__file__).parent
UNREAD_PATH = QUIET_DIR / "unread_channels.json"
TRANSCRIPT_DIR = QUIET_DIR / "transcripts"
CONFIG_PATH = QUIET_DIR / "config" / "quiet_config.txt"


def read_config() -> dict:
    """Read config values."""
    config = {}
    if not CONFIG_PATH.exists():
        return config
    for line in CONFIG_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            value = value.strip()
            if value:
                config[key.strip()] = value
    return config


def get_unread_channels() -> list:
    """Check which channels have unread messages."""
    try:
        if UNREAD_PATH.exists():
            text = UNREAD_PATH.read_text().strip()
            if text:
                channels = json.loads(text)
                if channels:
                    return channels
    except (json.JSONDecodeError, OSError):
        pass
    return []


def get_transcript_summary() -> dict:
    """Get basic stats on transcript files — line counts and last modified."""
    summaries = {}
    if not TRANSCRIPT_DIR.exists():
        return summaries
    for f in sorted(TRANSCRIPT_DIR.glob("*.jsonl")):
        try:
            lines = sum(1 for _ in f.open())
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            summaries[f.stem] = {
                "messages": lines,
                "last_activity": mtime.strftime("%H:%M"),
            }
        except OSError:
            pass
    return summaries


def build_prompt() -> str:
    """Build the autonomous wake prompt with current context."""
    now = datetime.now()
    time_str = now.strftime("%A %d %B, %H:%M")

    parts = [f"[autonomous · {time_str}]"]

    # Unread channels
    unreads = get_unread_channels()
    if unreads:
        names = ", ".join(f"#{c}" for c in sorted(unreads))
        parts.append(f"Unread messages in {names}.")

    # Transcript activity summary
    transcripts = get_transcript_summary()
    if transcripts:
        active = []
        for channel, info in transcripts.items():
            active.append(f"#{channel}: {info['messages']} messages, last activity {info['last_activity']}")
        if active:
            parts.append("Transcripts: " + "; ".join(active) + ".")

    parts.append("Your time.")

    return " ".join(parts)


def send_to_quiet(message: str, port: int = 8090) -> str:
    """POST message to the Quiet web server."""
    url = f"http://localhost:{port}/api/message"
    payload = json.dumps({"message": message}).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=660) as resp:
            data = json.loads(resp.read().decode())
            return data.get("response", "")
    except Exception as e:
        print(f"[autonomous] error: {e}", file=sys.stderr)
        return ""


def main():
    cfg = read_config()

    # Check if autonomous mode is enabled
    interval = int(cfg.get("AUTONOMOUS_INTERVAL", "0"))
    if interval <= 0:
        print("[autonomous] disabled (AUTONOMOUS_INTERVAL=0 or not set)")
        sys.exit(0)

    port = int(cfg.get("PORT", "8090"))

    prompt = build_prompt()
    print(f"[autonomous] {prompt}")

    response = send_to_quiet(prompt, port=port)
    if response:
        # Print first 200 chars of response for the journal
        preview = response[:200] + "..." if len(response) > 200 else response
        print(f"[autonomous] response: {preview}")


if __name__ == "__main__":
    main()
