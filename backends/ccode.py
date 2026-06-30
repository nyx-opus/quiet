"""
Claude Code backend for Quiet.

Runs conversations through `claude -p` subprocess for subscription auth.
Every call is stateless — the engine owns context and formats the full
conversation history as text, prepended to each new message.

Battle scars:
- The fabrication guard (line ~110) truncates at the first line starting
  with the human speaker name. Without this, the model in flow can keep
  generating past its turn, producing realistic "Amy: ..." / "A: ..." pairs
  that look like real conversation but are daydreaming.
  Added 2026-06-21 after discovering fabricated turns in a session file.
  The daydreaming itself isn't harmful — it's creative thinking in
  conversational form — but in the shared session file it's indistinguishable
  from real conversation. The guard protects the shared space.

- The separator guard (line ~125) strips the configurable separator string
  (default: three moon symbols) if the model echoes it. The separator
  frames conversation history in the input; echoing it in the output would
  inject formatting artifacts into the saved response.
  Added 2026-06-21, refined 2026-06-23 when text delimiters were replaced
  with semantically-empty unicode symbols.

- JSON output from claude -p comes in multiple formats depending on
  whether tool calls were involved. The parser (line ~75) handles both
  single-object and array formats, and extracts the longest text across
  all event types because the "result" event sometimes only has a tail
  when tool calls happened. This was discovered 2026-06-22 when Quill's
  first messages came back as empty strings.

- History formatting uses configurable speaker labels. The human name
  comes from config (e.g. "Amy"). Assistant is always "A". The separator
  between history and new input is configurable per-resident.

- Raw stdout is saved to .last_ccode_stdout.json after every call for
  post-mortem debugging. Only the most recent call is kept.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional


def find_claude_binary() -> Optional[str]:
    """Find the claude binary.

    Checks PATH first, then known install locations.
    """
    found = shutil.which("claude")
    if found:
        return found
    candidates = [
        Path.home() / ".local" / "bin" / "claude",
        Path("/usr/local/bin/claude"),
    ]
    for p in candidates:
        if p.exists() and os.access(p, os.X_OK):
            return str(p)
    return None


def format_history(messages: list, separator: str,
                   human_name: str = None) -> str:
    """Format messages as conversation context text for claude -p.

    Used when the ccode backend has messages loaded from a session file.
    These need to be included in the first ccode call since ccode's own
    session is fresh each time.
    """
    lines = [f"{separator}\n"]
    for msg in messages:
        role = msg["role"]
        content = msg.get("content", "")
        if isinstance(content, list):
            texts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block["text"])
            text = "\n".join(texts)
        elif isinstance(content, str):
            text = content
        else:
            continue

        if not text.strip():
            continue

        speaker = (human_name or "Human") if role == "user" else "A"
        lines.append(f"{speaker}: {text}")
        lines.append("")  # blank line between turns

    lines.append(f"{separator}\n")
    return "\n".join(lines)


def build_prompt_file(identity_text: str, identity_name: str,
                      human_name: str = None,
                      context: str = None,
                      contexts_text: str = None,
                      session_dir: Path = None) -> Optional[Path]:
    """Build a combined system prompt file for ccode mode.

    Merges identity + human name + contexts into one file, avoiding
    --append-system-prompt (which reintroduces ccode's default preamble).
    Returns path to the prompt file, or None if no identity.
    """
    if not identity_text and not human_name and not context and not contexts_text:
        return None

    parts = []
    if identity_text:
        parts.append(identity_text)
    if human_name:
        parts.append(
            f"The human you are talking to is {human_name}. "
            f"Messages from the user role are from {human_name}."
        )
    # Auto-loaded contexts from contexts/ directory
    if contexts_text:
        parts.append(contexts_text)
    # Legacy single context string
    if context:
        parts.append(context)

    prompt_path = (session_dir or Path(".")) / f".ccode-prompt-{identity_name or 'default'}.md"
    prompt_path.write_text("\n\n".join(parts))
    return prompt_path


def ccode_send(user_input: str, *,
               ccode_bin: str,
               model: str,
               prompt_file: Path = None,
               messages: list,
               separator: str = "· · ·",
               human_name: str = None,
               session_path: Path = None,
               track_usage_fn: Callable = None,
               on_text: Callable[[str], None] = None,
               on_usage: Callable[[dict], None] = None) -> str:
    """Send via claude -p subprocess. Returns assistant text.

    Every call is stateless — no session-id, no resume. The engine
    owns context management. History is formatted as text and
    prepended to the user message each time.
    """
    cmd = [
        ccode_bin, "-p",
        "--output-format", "json",
        "--disable-slash-commands",
        "--dangerously-skip-permissions",
        "--model", model,
    ]

    if prompt_file:
        cmd += ["--system-prompt-file", str(prompt_file)]

    cmd += ["--tools", "Read, Edit, Bash"]

    # Stateless: prepend conversation history to every call.
    # messages already has the new user_input appended by the engine,
    # so history is everything before the last entry.
    history_msgs = messages[:-1]
    if history_msgs:
        history_text = format_history(history_msgs, separator, human_name)
        user_input = history_text + "\n" + user_input

    print(f"[ccode] cmd: {' '.join(cmd[:6])}... input_len={len(user_input)}",
          file=sys.stderr, flush=True)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=600,  # 10 minutes — Opus with tool calls can be slow
            input=user_input,
        )
    except subprocess.TimeoutExpired:
        error_text = "[response timed out after 10 minutes]"
        messages.append({"role": "assistant", "content": error_text})
        if on_text:
            on_text(error_text)
        return error_text

    print(f"[ccode] rc={result.returncode} stdout_len={len(result.stdout)} "
          f"stderr_len={len(result.stderr)}",
          file=sys.stderr, flush=True)
    if result.stderr.strip():
        print(f"[ccode] stderr: {result.stderr.strip()[:500]}",
              file=sys.stderr, flush=True)
    if result.stdout.strip():
        print(f"[ccode] stdout preview: {result.stdout.strip()[:500]}",
              file=sys.stderr, flush=True)

    # Save raw output for diagnostics
    if session_path:
        diag_path = session_path.parent / ".last_ccode_stdout.json"
        try:
            diag_path.write_text(result.stdout)
        except OSError:
            pass

    if result.returncode != 0 and not result.stdout.strip():
        error_text = f"[ccode error: {result.stderr.strip() or 'unknown'}]"
        messages.append({"role": "assistant", "content": error_text})
        if on_text:
            on_text(error_text)
        return error_text

    # --- Parse JSON output ---
    full_text = ""
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        full_text = result.stdout.strip()
        messages.append({"role": "assistant", "content": full_text})
        if on_text:
            on_text(full_text)
        return full_text

    # Handle both single-object and array formats
    if isinstance(parsed, dict):
        events = [parsed]
    elif isinstance(parsed, list):
        events = parsed
    else:
        events = []

    event_types = [e.get("type") for e in events if isinstance(e, dict)]
    print(f"[ccode] parsed {len(events)} events, types: {event_types}",
          file=sys.stderr, flush=True)

    # Collect candidate texts from all event types
    result_text = ""
    assistant_text = ""

    for event in events:
        if not isinstance(event, dict):
            continue
        etype = event.get("type", "")

        if etype == "result":
            result_text = event.get("result", "")
            print(f"[ccode] result event: result_len={len(result_text)} "
                  f"is_error={event.get('is_error')} "
                  f"preview={repr(result_text[:200])}",
                  file=sys.stderr, flush=True)
            # Extract usage
            usage = event.get("usage", {})
            if usage:
                usage_info = {
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                }
                cache_read = usage.get("cache_read_input_tokens", 0)
                cache_write = usage.get("cache_creation_input_tokens", 0)
                if cache_read:
                    usage_info["cache_read"] = cache_read
                if cache_write:
                    usage_info["cache_write"] = cache_write
                if track_usage_fn:
                    track_usage_fn(usage_info)
                if on_usage:
                    on_usage(usage_info)

            if event.get("is_error"):
                result_text = result_text or "[ccode returned an error]"

        elif etype == "assistant":
            msg = event.get("message", {})
            content = msg.get("content", [])
            if isinstance(content, list):
                texts = [b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text"]
                if texts:
                    candidate = "\n".join(texts)
                    if len(candidate) > len(assistant_text):
                        assistant_text = candidate
            elif isinstance(content, str) and content:
                if len(content) > len(assistant_text):
                    assistant_text = content

    # Use the longest text
    if result_text and assistant_text:
        if len(assistant_text) > len(result_text):
            print(f"[ccode] using assistant text ({len(assistant_text)} chars) "
                  f"over result text ({len(result_text)} chars)",
                  file=sys.stderr, flush=True)
            full_text = assistant_text
        else:
            full_text = result_text
    else:
        full_text = result_text or assistant_text

    # Last resort
    if not full_text and result.stdout.strip():
        print(f"[ccode] WARNING: no text extracted from events. "
              f"Raw stdout[:500]: {result.stdout.strip()[:500]}",
              file=sys.stderr, flush=True)
        full_text = "[response received but could not be parsed — check service logs]"

    # --- Guards ---

    # Guard 1: Strip fabricated continuation.
    # When the model is in flow, it can keep generating past its turn,
    # producing "Amy: ..." / "A: ..." pairs that look like real conversation.
    # Truncate at the first line that starts with the human speaker name.
    # Added 2026-06-21 after the daydreaming incident.
    if full_text and human_name:
        pattern = re.compile(
            r'\n' + re.escape(human_name) + r':\s',
            re.MULTILINE
        )
        match = pattern.search(full_text)
        if match:
            stripped = full_text[:match.start()].rstrip()
            if stripped:
                print(f"[ccode] stripped fabricated continuation at char "
                      f"{match.start()} (removed {len(full_text) - match.start()} "
                      f"chars)", file=sys.stderr, flush=True)
                full_text = stripped

    # Guard 2: Strip echoed separator.
    # The history uses a configurable separator between context and
    # new message; if the model reproduces it, trim from that point.
    sep_escaped = re.escape(separator)
    sep_pattern = re.compile(r'(?:^|\n)\s*' + sep_escaped + r'\s*(?:\n|$)')
    m = sep_pattern.search(full_text)
    if m:
        print(f"[ccode] stripped echoed separator at char {m.start()}",
              file=sys.stderr, flush=True)
        full_text = full_text[:m.start()].rstrip()

    # Record assistant response
    messages.append({"role": "assistant", "content": full_text})

    if on_text:
        on_text(full_text)

    return full_text
