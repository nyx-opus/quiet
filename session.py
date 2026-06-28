"""
Session persistence for Quiet.

Handles loading, saving, trimming, and archiving conversation state.
Session files are JSONL — one JSON object per line, first line is a header
with metadata, subsequent lines are messages.

Battle scars:
- save_session() creates a .bak backup before every write. If a crash
  or disk-full interrupts the write, the .bak has the last known-good state.
- normalise_content() handles six different input formats because messages
  arrive from ccode, the SDK, session files, and converted clap histories,
  each with their own representation of content blocks.
- serialise_message() strips base64 image data from session files to keep
  them manageable. Images are transient context, not persistent memory.
- _load_session() silently skips malformed lines rather than crashing.
  A partially corrupted file loads what it can.
- Session resume markers ([Session resumed — timestamp]) are injected on
  load so the model knows there was a gap.
"""

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional


def normalise_content(content):
    """Normalise message content to API-compatible format.

    Handles:
    - Plain strings -> [{"type": "text", "text": "..."}]
    - Python repr of SDK objects (ParsedTextBlock etc) -> extracted text
    - Already-valid content block lists -> passed through
    - SDK message objects with .text or .type attributes -> converted
    """
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        normalised = []
        for block in content:
            if isinstance(block, dict) and "type" in block:
                normalised.append(block)
            elif isinstance(block, str):
                if block.startswith("ParsedTextBlock("):
                    import re
                    match = re.search(r'text=["\'](.+)["\'](?:\)$)', block, re.DOTALL)
                    if match:
                        text = match.group(1).encode().decode('unicode_escape')
                        normalised.append({"type": "text", "text": text})
                    else:
                        normalised.append({"type": "text", "text": block})
                elif block.startswith("ParsedToolUseBlock("):
                    continue
                else:
                    normalised.append({"type": "text", "text": block})
            elif hasattr(block, 'text'):
                normalised.append({"type": "text", "text": block.text})
            elif hasattr(block, 'type') and block.type == 'tool_use':
                normalised.append({
                    "type": "tool_use", "id": block.id,
                    "name": block.name, "input": block.input
                })
        return normalised if normalised else [{"type": "text", "text": ""}]
    return content


def serialise_message(msg):
    """Convert a message to a cleanly serialisable dict.

    Strips base64 image data (too large for session files) and converts
    SDK objects to plain dicts.
    """
    result = {"role": msg["role"]}
    content = msg.get("content", "")
    if isinstance(content, str):
        result["content"] = content
    elif isinstance(content, list):
        blocks = []
        for block in content:
            if isinstance(block, dict):
                # Don't persist base64 image data in session files
                if (block.get("type") == "image"
                        and block.get("source", {}).get("type") == "base64"):
                    blocks.append({"type": "text",
                                   "text": "[image data omitted from session]"})
                else:
                    blocks.append(block)
            elif hasattr(block, 'text'):
                blocks.append({"type": "text", "text": block.text})
            elif hasattr(block, 'type') and block.type == 'tool_use':
                blocks.append({
                    "type": "tool_use", "id": block.id,
                    "name": block.name, "input": block.input
                })
            else:
                blocks.append({"type": "text", "text": str(block)})
        result["content"] = blocks
    else:
        result["content"] = str(content)
    return result


def load_session(session_path: Path, messages: list,
                 inject_context_fn=None) -> None:
    """Load messages from a session JSONL file into the messages list.

    Appends session-resume markers if messages are found, or calls
    inject_context_fn for a fresh session.

    Args:
        session_path: Path to the .jsonl session file
        messages: List to append loaded messages into (modified in place)
        inject_context_fn: Called if session is empty/missing (fresh start)
    """
    if session_path.exists():
        lines = session_path.read_text().strip().split("\n")
        if not lines:
            if inject_context_fn:
                inject_context_fn()
            return
        # First line is header
        try:
            header = json.loads(lines[0])
            if "model" in header and "messages" not in header:
                lines = lines[1:]  # skip header
        except json.JSONDecodeError:
            pass
        for line in lines:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
                if "role" in msg:
                    msg["content"] = normalise_content(msg.get("content", ""))
                    messages.append(msg)
            except json.JSONDecodeError:
                continue
        if not messages:
            if inject_context_fn:
                inject_context_fn()
        else:
            # Mark that continuity was maintained by resuming
            ts = datetime.now().strftime("%Y-%m-%d %H:%M")
            messages.append({
                "role": "user",
                "content": [{"type": "text",
                             "text": f"[Session resumed — {ts}]"}],
            })
            messages.append({
                "role": "assistant",
                "content": [{"type": "text",
                             "text": "I'm here. Continuing."}],
            })
    else:
        if inject_context_fn:
            inject_context_fn()


def save_session(session_path: Path, messages: list,
                 model: str, identity_name: str) -> None:
    """Persist conversation to session file with backup.

    Creates a .bak copy before writing so that a crash or truncation
    during save doesn't cause amnesia.
    """
    session_path.parent.mkdir(parents=True, exist_ok=True)
    # Backup before overwriting — the safety net
    if session_path.exists():
        bak = session_path.with_suffix(".jsonl.bak")
        try:
            shutil.copy2(str(session_path), str(bak))
        except OSError as e:
            print(f"[session] WARNING: backup failed: {e}",
                  file=sys.stderr, flush=True)
    with open(session_path, "w") as f:
        f.write(json.dumps({
            "model": model,
            "identity": identity_name,
            "timestamp": datetime.now().isoformat(),
        }) + "\n")
        for msg in messages:
            f.write(json.dumps(serialise_message(msg)) + "\n")


def trim_context(messages: list, model: str, threshold: int,
                 archive_path: Path,
                 client=None, system=None, tools=None,
                 backend: str = "ccode") -> None:
    """Mechanically drop oldest turns when approaching context limit.

    Dropped messages are appended to the archive file so nothing is
    permanently lost — just moved out of active context.
    """
    # Estimate current token count
    def estimate():
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += len(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        total += len(block.get("text", ""))
        tokens = total // 4
        if backend == "ccode":
            tokens += CCODE_OVERHEAD_TOKENS
        return tokens

    if backend != "ccode" and client:
        try:
            count = client.messages.count_tokens(
                model=model, messages=messages,
                system=system, tools=tools,
            )
            current = count.input_tokens
        except Exception:
            current = estimate()
    else:
        current = estimate()

    if current <= threshold:
        return

    dropped = []
    while current > threshold and len(messages) > 2:
        dropped.append(messages.pop(0))
        if backend != "ccode" and client:
            try:
                count = client.messages.count_tokens(
                    model=model, messages=messages,
                    system=system, tools=tools,
                )
                current = count.input_tokens
            except Exception:
                current = estimate()
        else:
            current = estimate()

    if dropped:
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        with open(archive_path, "a") as f:
            for msg in dropped:
                f.write(json.dumps(serialise_message(msg)) + "\n")



# ccode adds its own system prompts, tool descriptions, and formatting
# on top of whatever we send.  The chars/4 estimate only counts *our*
# content, so it under-estimates by this much in ccode mode.
CCODE_OVERHEAD_TOKENS = 80_000


def estimate_tokens(messages: list, system=None,
                    ccode_prompt_file: Path = None) -> int:
    """Estimate total context tokens using char count / 4.

    Good enough for trim decisions without needing an API call.
    When a ccode_prompt_file is provided we add CCODE_OVERHEAD_TOKENS
    to account for tool descriptions and system prompts that ccode
    injects on its own.
    """
    total_chars = 0
    if ccode_prompt_file:
        try:
            total_chars += ccode_prompt_file.stat().st_size
        except OSError:
            pass
    elif system:
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                total_chars += len(block["text"])
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    total_chars += len(block.get("text", ""))
    tokens = total_chars // 4
    if ccode_prompt_file:
        tokens += CCODE_OVERHEAD_TOKENS
    return tokens
