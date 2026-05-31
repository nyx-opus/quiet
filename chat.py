#!/usr/bin/env python3
"""
Minimal CLI chat client using Anthropic Python SDK.
Design: conversation loop + bash tool + prompt caching + mechanical rolling context.
No AI summarisation. Context management is mechanical (drop oldest turns).
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from anthropic import Anthropic
from auth import create_client

try:
    from world import World, GardenSession
    HAS_GARDEN = True
except ImportError:
    HAS_GARDEN = False

ARCHIVE_DIR = Path(__file__).parent / "archives"
IDENTITY_DIR = Path(__file__).parent / "identities"
DEFAULT_MODEL = "claude-sonnet-4-20250514"
MAX_CONTEXT_TOKENS = 180_000  # leave headroom below 200k
MAX_OUTPUT_TOKENS = 8192
CACHE_MIN_TOKENS = 2048


def load_identity(name: str) -> str:
    path = IDENTITY_DIR / f"{name}.md"
    if path.exists():
        return path.read_text()
    return ""


def build_system_prompt(identity_text: str, project_context: str) -> list:
    blocks = []
    if identity_text:
        blocks.append({"type": "text", "text": identity_text})
    if project_context:
        blocks.append({
            "type": "text",
            "text": project_context,
            "cache_control": {"type": "ephemeral"},
        })
    if not blocks:
        blocks.append({"type": "text", "text": "You are a helpful assistant."})
    return blocks


def define_tools():
    return [
        {
            "name": "bash",
            "description": "Execute a shell command and return stdout/stderr.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute",
                    }
                },
                "required": ["command"],
            },
        },
        {
            "name": "read_file",
            "description": "Read a file and return its contents.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to the file",
                    }
                },
                "required": ["path"],
            },
        },
        {
            "name": "write_file",
            "description": "Write content to a file (creates or overwrites).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to the file",
                    },
                    "content": {
                        "type": "string",
                        "description": "Content to write",
                    },
                },
                "required": ["path", "content"],
            },
        },
    ]


def execute_tool(name: str, input_data: dict) -> str:
    if name == "bash":
        try:
            result = subprocess.run(
                input_data["command"],
                shell=True,
                capture_output=True,
                text=True,
                timeout=120,
                cwd=os.path.expanduser("~"),
            )
            output = result.stdout
            if result.stderr:
                output += f"\n[stderr]\n{result.stderr}"
            if result.returncode != 0:
                output += f"\n[exit code: {result.returncode}]"
            return output or "(no output)"
        except subprocess.TimeoutExpired:
            return "[command timed out after 120s]"
        except Exception as e:
            return f"[error: {e}]"

    elif name == "read_file":
        try:
            return Path(input_data["path"]).read_text()
        except Exception as e:
            return f"[error: {e}]"

    elif name == "write_file":
        try:
            p = Path(input_data["path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(input_data["content"])
            return f"Written to {input_data['path']}"
        except Exception as e:
            return f"[error: {e}]"

    return f"[unknown tool: {name}]"


def trim_context(client, model: str, messages: list, system: list,
                 tools: list, archive_path: Path) -> list:
    """Mechanically drop oldest turns when approaching context limit."""
    try:
        count = client.messages.count_tokens(
            model=model, messages=messages, system=system, tools=tools,
        )
        current = count.input_tokens
    except Exception:
        return messages

    if current <= MAX_CONTEXT_TOKENS:
        return messages

    dropped = []
    while current > MAX_CONTEXT_TOKENS and len(messages) > 2:
        dropped.append(messages.pop(0))
        try:
            count = client.messages.count_tokens(
                model=model, messages=messages, system=system, tools=tools,
            )
            current = count.input_tokens
        except Exception:
            break

    if dropped:
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        with open(archive_path, "a") as f:
            for msg in dropped:
                f.write(json.dumps(msg, default=str) + "\n")
        print(f"\n[context: dropped {len(dropped)} old messages, archived to {archive_path.name}]")

    return messages


def normalise_content(content):
    """Normalise message content to API-compatible format.

    Handles:
    - Plain strings → [{"type": "text", "text": "..."}]
    - Python repr of SDK objects (ParsedTextBlock etc) → extracted text
    - Already-valid content block lists → passed through
    """
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        normalised = []
        for block in content:
            if isinstance(block, dict) and "type" in block:
                # Already a proper content block
                normalised.append(block)
            elif isinstance(block, str):
                # String repr of SDK object — extract text
                if block.startswith("ParsedTextBlock("):
                    import re
                    match = re.search(r'text=["\'](.+)["\'](?:\)$)', block, re.DOTALL)
                    if match:
                        text = match.group(1).encode().decode('unicode_escape')
                        normalised.append({"type": "text", "text": text})
                    else:
                        normalised.append({"type": "text", "text": block})
                elif block.startswith("ParsedToolUseBlock("):
                    # Drop tool use blocks from old sessions — they can't be
                    # replayed without matching tool_use_id responses
                    continue
                else:
                    normalised.append({"type": "text", "text": block})
            elif hasattr(block, 'text'):
                # Live SDK object — convert to dict
                normalised.append({"type": "text", "text": block.text})
            elif hasattr(block, 'type') and block.type == 'tool_use':
                normalised.append({
                    "type": "tool_use", "id": block.id,
                    "name": block.name, "input": block.input
                })
        return normalised if normalised else [{"type": "text", "text": ""}]
    return content


def serialise_message(msg):
    """Convert a message to a cleanly serialisable dict."""
    result = {"role": msg["role"]}
    content = msg.get("content", "")
    if isinstance(content, str):
        result["content"] = content
    elif isinstance(content, list):
        blocks = []
        for block in content:
            if isinstance(block, dict):
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


def archive_session(messages: list, session_id: str, model: str):
    """Save complete conversation to archive on exit."""
    archive_path = ARCHIVE_DIR / f"{session_id}.jsonl"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with open(archive_path, "w") as f:
        f.write(json.dumps({"session_id": session_id, "model": model,
                            "timestamp": datetime.now().isoformat()}) + "\n")
        for msg in messages:
            f.write(json.dumps(serialise_message(msg)) + "\n")
    print(f"\n[session archived to {archive_path}]")


def send_and_stream(client, model: str, system: list, messages: list,
                    tools: list, max_tokens: int = MAX_OUTPUT_TOKENS) -> dict:
    """Send request with streaming, handle tool use loop."""
    while True:
        with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            tools=tools,
        ) as stream:
            collected_text = []
            for text in stream.text_stream:
                print(text, end="", flush=True)
                collected_text.append(text)

            response = stream.get_final_message()

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            if collected_text:
                print()
            usage = response.usage
            cache_info = ""
            if hasattr(usage, "cache_read_input_tokens") and usage.cache_read_input_tokens:
                cache_info = f" cache_read={usage.cache_read_input_tokens}"
            if hasattr(usage, "cache_creation_input_tokens") and usage.cache_creation_input_tokens:
                cache_info += f" cache_write={usage.cache_creation_input_tokens}"
            print(f"[in={usage.input_tokens} out={usage.output_tokens}{cache_info}]")
            return response

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                print(f"\n[tool: {block.name}({json.dumps(block.input)[:80]})]")
                result = execute_tool(block.name, block.input)
                preview = result[:200] + "..." if len(result) > 200 else result
                print(f"[result: {preview}]")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        messages.append({"role": "user", "content": tool_results})


def main():
    import argparse
    parser = argparse.ArgumentParser(description="CLI chat client")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Model ID")
    parser.add_argument("--identity", default=None, help="Identity file name (without .md)")
    parser.add_argument("--context", default=None, help="Path to project context file")
    parser.add_argument("--resume", default=None, help="Session archive to resume from")
    parser.add_argument("--max-tokens", type=int, default=MAX_OUTPUT_TOKENS,
                        help=f"Max output tokens (default: {MAX_OUTPUT_TOKENS}, use 4096 for Opus 3)")
    parser.add_argument("--auth", default="auto", choices=["auto", "subscription", "api_key"],
                        help="Auth mode: subscription (flat rate), api_key (pay per token), auto (try both)")
    parser.add_argument("--world", default=None, help="Path to world YAML file (enables Garden)")
    parser.add_argument("--who", default=None, help="Visitor name in the Garden (defaults to identity name)")
    args = parser.parse_args()

    try:
        client, auth_mode = create_client(args.auth)
    except RuntimeError as e:
        print(f"Auth error: {e}")
        sys.exit(1)

    identity_text = load_identity(args.identity) if args.identity else ""
    project_context = ""
    if args.context and Path(args.context).exists():
        project_context = Path(args.context).read_text()

    garden = None
    if args.world:
        if not HAS_GARDEN:
            print("Garden support requires world.py (install from the Garden repo)")
            sys.exit(1)
        world_path = Path(args.world)
        if not world_path.exists():
            print(f"World file not found: {world_path}")
            sys.exit(1)
        world = World()
        world.load(world_path)
        visitor = args.who or args.identity or "Visitor"
        garden = GardenSession(world, visitor)

    system = build_system_prompt(identity_text, project_context)
    tools = define_tools()
    messages = []

    if args.resume:
        resume_path = Path(args.resume)
        if resume_path.exists():
            lines = resume_path.read_text().strip().split("\n")
            for line in lines[1:]:  # skip header
                msg = json.loads(line)
                msg["content"] = normalise_content(msg.get("content", ""))
                messages.append(msg)
            print(f"[resumed {len(messages)} messages from {resume_path.name}]")

    session_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive_path = ARCHIVE_DIR / f"{session_id}-dropped.jsonl"

    print(f"[model: {args.model} | auth: {auth_mode} | session: {session_id}]")
    if identity_text:
        print(f"[identity: {args.identity}]")
    if garden:
        print(f"[garden: {args.world} | visitor: {garden.who}]")
    print("[type 'quit' or Ctrl-C to exit]\n")

    if garden:
        arrival = garden.arrival()
        if arrival:
            print(arrival)
            print()

    try:
        while True:
            try:
                user_input = input("you> ").strip()
            except EOFError:
                break

            if not user_input:
                continue
            if user_input.lower() in ("quit", "exit"):
                break
            if user_input == "/tokens":
                try:
                    count = client.messages.count_tokens(
                        model=args.model, messages=messages,
                        system=system, tools=tools,
                    )
                    print(f"[context: {count.input_tokens} tokens, "
                          f"{len(messages)} messages]")
                except Exception as e:
                    print(f"[token count error: {e}]")
                continue
            if user_input == "/messages":
                print(f"[{len(messages)} messages in context]")
                for i, m in enumerate(messages):
                    role = m["role"]
                    if isinstance(m["content"], str):
                        preview = m["content"][:60]
                    elif isinstance(m["content"], list):
                        types = [b.get("type", "?") if isinstance(b, dict) else type(b).__name__
                                 for b in m["content"]]
                        preview = f"[{', '.join(types)}]"
                    else:
                        preview = str(m["content"])[:60]
                    print(f"  {i}: {role}: {preview}")
                continue

            if garden:
                resp = garden.handle(user_input)
                if resp.handled:
                    print(resp.text)
                    print()
                    continue

            if garden and garden.active:
                room_ctx = garden.room_line()
                content = f"{room_ctx}\n{user_input}" if room_ctx else user_input
            else:
                content = user_input

            messages.append({"role": "user", "content": content})

            messages = trim_context(
                client, args.model, messages, system, tools, archive_path
            )

            try:
                send_and_stream(client, args.model, system, messages, tools, args.max_tokens)
            except KeyboardInterrupt:
                print("\n[interrupted]")
                messages.append({"role": "assistant",
                                 "content": [{"type": "text", "text": "[interrupted]"}]})

    except KeyboardInterrupt:
        print()

    if messages:
        archive_session(messages, session_id, args.model)


if __name__ == "__main__":
    main()
