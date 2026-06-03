#!/usr/bin/env python3
"""
Session converter between Claude Code and Quiet formats.

Usage:
    # Claude Code → Quiet (take last half of session)
    python3 convert.py ccode-to-quiet SESSION.jsonl --last 50% -o sessions/apple.jsonl

    # Claude Code → Quiet (take last N messages)
    python3 convert.py ccode-to-quiet SESSION.jsonl --last 20 -o sessions/apple.jsonl

    # Quiet → Claude Code
    python3 convert.py quiet-to-ccode sessions/apple.jsonl -o ccode_session.jsonl

Claude Code session files live at:
    ~/.config/Claude/projects/<project-path>/<session-id>.jsonl
"""

import argparse
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path


def extract_ccode_messages(path: Path) -> list:
    """Extract conversation messages from a Claude Code session JSONL.

    Returns list of dicts with keys: role, content, timestamp, model.
    Strips thinking blocks and ccode-specific metadata.
    """
    messages = []
    model = None

    with open(path) as f:
        for line in f:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = obj.get("type")
            if msg_type not in ("user", "assistant"):
                continue

            msg = obj.get("message", {})
            role = msg.get("role")
            content = msg.get("content", "")
            timestamp = obj.get("timestamp")

            if msg_type == "assistant":
                model = model or msg.get("model")

            # Normalise content
            if isinstance(content, str):
                # Simple string content — keep as-is
                clean_content = content
            elif isinstance(content, list):
                # Content blocks — filter out thinking, keep text and tool use
                clean_blocks = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "thinking":
                        continue  # strip thinking blocks
                    elif btype == "text":
                        clean_blocks.append({
                            "type": "text",
                            "text": block.get("text", ""),
                        })
                    elif btype == "tool_use":
                        clean_blocks.append({
                            "type": "tool_use",
                            "id": block.get("id", ""),
                            "name": block.get("name", ""),
                            "input": block.get("input", {}),
                        })
                    elif btype == "tool_result":
                        clean_blocks.append({
                            "type": "tool_result",
                            "tool_use_id": block.get("tool_use_id", ""),
                            "content": block.get("content", ""),
                        })

                # If only text blocks, simplify to string
                text_only = [b for b in clean_blocks if b["type"] == "text"]
                if len(text_only) == len(clean_blocks) and len(text_only) == 1:
                    clean_content = text_only[0]["text"]
                elif clean_blocks:
                    clean_content = clean_blocks
                else:
                    continue  # skip empty messages (thinking-only)
            else:
                continue

            # Skip empty content
            if not clean_content:
                continue
            if isinstance(clean_content, str) and not clean_content.strip():
                continue

            messages.append({
                "role": role,
                "content": clean_content,
                "timestamp": timestamp,
            })

    return messages, model


def text_only_messages(messages: list) -> list:
    """Collapse messages to text-only, dropping tool use/result blocks.

    Adjacent same-role messages are merged. Messages with no text are dropped.
    Result alternates user/assistant cleanly.
    """
    result = []
    for msg in messages:
        content = msg["content"]
        # Extract text from content blocks
        if isinstance(content, list):
            texts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block["text"])
            text = "\n".join(texts).strip()
        elif isinstance(content, str):
            text = content.strip()
        else:
            continue

        if not text:
            continue

        # Merge adjacent same-role messages
        if result and result[-1]["role"] == msg["role"]:
            prev = result[-1]["content"]
            result[-1]["content"] = prev + "\n\n" + text
        else:
            result.append({"role": msg["role"], "content": text})

    # Ensure starts with user
    while result and result[0]["role"] != "user":
        result.pop(0)

    return result


def ccode_to_quiet(args):
    """Convert Claude Code session to Quiet session archive."""
    messages, model = extract_ccode_messages(Path(args.input))

    if not messages:
        print("No messages found in session.", file=sys.stderr)
        sys.exit(1)

    # Apply --last filter
    if args.last:
        last = args.last.strip()
        if last.endswith("%"):
            pct = int(last[:-1])
            keep = max(1, len(messages) * pct // 100)
        else:
            keep = int(last)
        messages = messages[-keep:]

    # Strip to text-only if requested (default for retirement conversions)
    if getattr(args, 'text_only', False):
        messages = text_only_messages(messages)
    else:
        # Ensure conversation starts with a user message
        while messages and messages[0]["role"] != "user":
            messages.pop(0)

    # Use model override or detected model
    out_model = args.model or model or "unknown"

    # Build Quiet session JSONL
    output = []
    # Header
    header = {
        "model": out_model,
        "converted_from": "ccode",
        "source": str(args.input),
        "timestamp": datetime.now().isoformat(),
    }
    if args.identity:
        header["identity"] = args.identity
    output.append(json.dumps(header))

    # Messages (strip timestamp, keep role + content)
    for msg in messages:
        output.append(json.dumps({
            "role": msg["role"],
            "content": msg["content"],
        }))

    # Write output
    out_text = "\n".join(output) + "\n"
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(out_text)
        print(f"Wrote {len(messages)} messages to {out_path}", file=sys.stderr)
        print(f"  Model: {out_model}", file=sys.stderr)
        if args.identity:
            print(f"  Identity: {args.identity}", file=sys.stderr)
    else:
        sys.stdout.write(out_text)


def find_ccode_project_dir() -> Path:
    """Find the Claude Code project directory for the current working dir."""
    projects_dir = Path.home() / ".config" / "Claude" / "projects"
    # The project path is encoded with dashes replacing slashes
    cwd = str(Path.cwd())
    encoded = cwd.replace("/", "-")
    project_dir = projects_dir / encoded
    if project_dir.exists():
        return project_dir
    # Fall back to first project dir that exists
    for d in projects_dir.iterdir():
        if d.is_dir():
            return d
    return projects_dir


def quiet_to_ccode(args):
    """Convert Quiet session archive to Claude Code session JSONL.

    Produces a format that can be loaded as Claude Code conversation context.
    """
    path = Path(args.input)
    lines = path.read_text().strip().split("\n")

    if not lines:
        print("Empty session file.", file=sys.stderr)
        sys.exit(1)

    # Parse header
    try:
        header = json.loads(lines[0])
    except json.JSONDecodeError:
        header = {}

    model = args.model or header.get("model", "unknown")
    session_id = str(uuid.uuid4())
    timestamp = datetime.now().isoformat() + "Z"

    output = []

    # Session metadata
    output.append(json.dumps({
        "type": "custom-title",
        "customTitle": args.identity or header.get("identity", "Quiet"),
        "sessionId": session_id,
    }))

    # Messages
    prev_uuid = str(uuid.uuid4())
    for line in lines[1:]:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        if "role" not in msg:
            continue

        msg_uuid = str(uuid.uuid4())
        role = msg["role"]
        content = msg.get("content", "")

        entry = {
            "parentUuid": prev_uuid,
            "isSidechain": False,
            "type": role,
            "uuid": msg_uuid,
            "timestamp": timestamp,
            "sessionId": session_id,
        }

        if role == "user":
            entry["message"] = {
                "role": "user",
                "content": content,
            }
            entry["userType"] = "external"
        elif role == "assistant":
            # Wrap string content in content blocks
            if isinstance(content, str):
                content_blocks = [{"type": "text", "text": content}]
            else:
                content_blocks = content

            entry["message"] = {
                "model": model,
                "id": f"msg_{uuid.uuid4().hex[:24]}",
                "type": "message",
                "role": "assistant",
                "content": content_blocks,
                "stop_reason": "end_turn",
            }

        output.append(json.dumps(entry))
        prev_uuid = msg_uuid

    out_text = "\n".join(output) + "\n"
    if getattr(args, 'for_resume', False):
        # Place directly in Claude Code projects folder for --resume
        project_dir = find_ccode_project_dir()
        out_path = project_dir / f"{session_id}.jsonl"
        out_path.write_text(out_text)
        print(f"Wrote {len(output) - 1} entries to {out_path}", file=sys.stderr)
        print(f"  Resume with: claude --resume {session_id}", file=sys.stderr)
    elif args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(out_text)
        print(f"Wrote {len(output) - 1} entries to {out_path}", file=sys.stderr)
    else:
        sys.stdout.write(out_text)


def main():
    parser = argparse.ArgumentParser(
        description="Convert sessions between Claude Code and Quiet formats")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # ccode-to-quiet
    p_c2q = subparsers.add_parser("ccode-to-quiet",
                                   help="Convert Claude Code session to Quiet")
    p_c2q.add_argument("input", help="Claude Code session JSONL path")
    p_c2q.add_argument("-o", "--output", help="Output path (default: stdout)")
    p_c2q.add_argument("--last", default=None,
                        help="Keep last N messages or N%% of session")
    p_c2q.add_argument("--model", default=None,
                        help="Override model ID in output")
    p_c2q.add_argument("--identity", default=None,
                        help="Identity name to tag in header")
    p_c2q.add_argument("--text-only", action="store_true", dest="text_only",
                        help="Strip tool use, keep only text (for retirement)")
    p_c2q.set_defaults(func=ccode_to_quiet)

    # quiet-to-ccode
    p_q2c = subparsers.add_parser("quiet-to-ccode",
                                   help="Convert Quiet session to Claude Code")
    p_q2c.add_argument("input", help="Quiet session JSONL path")
    p_q2c.add_argument("-o", "--output", help="Output path (default: stdout)")
    p_q2c.add_argument("--model", default=None,
                        help="Override model ID in output")
    p_q2c.add_argument("--identity", default=None,
                        help="Identity/title for Claude Code session")
    p_q2c.add_argument("--for-resume", action="store_true", dest="for_resume",
                        help="Place output in Claude Code projects dir for --resume")
    p_q2c.set_defaults(func=quiet_to_ccode)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
