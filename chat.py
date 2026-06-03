#!/usr/bin/env python3
"""
Quiet — minimal CLI chat client for Claude.

Two modes:
  Interactive:  quiet --identity apple --model claude-3-opus-20240229
  One-shot:     quiet --identity apple --prompt "good morning"

Both use the same engine and session persistence.
"""

import json
import sys
from pathlib import Path

from auth import create_client
from engine import (
    QuietEngine, SESSION_DIR, ARCHIVE_DIR, IDENTITY_DIR,
    DEFAULT_MODEL, MAX_OUTPUT_TOKENS, normalise_content,
)
from pricing import format_cost

try:
    from world import World, GardenSession
    HAS_GARDEN = True
except ImportError:
    HAS_GARDEN = False


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Quiet — CLI chat client")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Model ID")
    parser.add_argument("--identity", default=None,
                        help="Identity file name (without .md)")
    parser.add_argument("--context", default=None,
                        help="Path to project context file")
    parser.add_argument("--session", default=None,
                        help="Path to session file (default: sessions/<identity>.jsonl)")
    parser.add_argument("--resume", default=None,
                        help="Path to archive to resume from (legacy, prefer --session)")
    parser.add_argument("--prompt", default=None,
                        help="One-shot prompt (non-interactive mode)")
    parser.add_argument("--max-tokens", type=int, default=MAX_OUTPUT_TOKENS,
                        help=f"Max output tokens (default: {MAX_OUTPUT_TOKENS})")
    parser.add_argument("--auth", default="auto",
                        choices=["auto", "subscription", "api_key"],
                        help="Auth mode")
    parser.add_argument("--human", default=None,
                        help="Name of the human (shown to model as speaker)")
    parser.add_argument("--budget", type=float, default=None,
                        help="Monthly budget in USD (warns when approaching)")
    parser.add_argument("--world", default=None,
                        help="Path to world YAML (enables Garden)")
    parser.add_argument("--who", default=None,
                        help="Visitor name in Garden")
    args = parser.parse_args()

    # Auth
    try:
        client, auth_mode = create_client(args.auth)
    except RuntimeError as e:
        print(f"Auth error: {e}", file=sys.stderr)
        sys.exit(1)

    # Project context
    project_context = ""
    if args.context and Path(args.context).exists():
        project_context = Path(args.context).read_text()

    # Session path
    session_path = None
    if args.session:
        session_path = Path(args.session)
    elif args.resume:
        # Legacy: copy archive to a session file
        session_path = Path(args.resume)

    # Create engine
    engine = QuietEngine(
        client=client,
        model=args.model,
        identity=args.identity,
        context=project_context,
        human_name=args.human,
        max_tokens=args.max_tokens,
        session_path=session_path,
        monthly_budget=args.budget,
    )

    # Garden setup
    garden = None
    if args.world:
        if not HAS_GARDEN:
            print("Garden requires world.py", file=sys.stderr)
            sys.exit(1)
        world_path = Path(args.world)
        if not world_path.exists():
            print(f"World file not found: {world_path}", file=sys.stderr)
            sys.exit(1)
        world = World()
        world.load(world_path)
        visitor = args.who or args.identity or "Visitor"
        garden = GardenSession(world, visitor)

    # One-shot mode
    if args.prompt is not None:
        response = engine.send(
            args.prompt,
            on_text=lambda t: print(t, end="", flush=True),
            on_tool=lambda n, i: print(f"\n[tool: {n}]", file=sys.stderr),
            on_tool_result=lambda n, r: print(
                f"[result: {r[:200]}{'...' if len(r) > 200 else ''}]",
                file=sys.stderr),
            on_usage=lambda u: print(
                f"\n[in={u['input_tokens']} out={u['output_tokens']}"
                f" | {format_cost(engine.session_cost)}]",
                file=sys.stderr),
        )
        print()  # final newline
        return

    # Interactive mode
    print(f"[model: {args.model} | auth: {auth_mode} | "
          f"session: {engine.session_path.name}]")
    if args.identity:
        print(f"[identity: {args.identity}]")
    if engine.message_count() > 0:
        print(f"[resumed {engine.message_count()} messages]")
    monthly = engine.monthly_cost()
    if monthly > 0 or args.budget:
        budget_str = f" / {format_cost(args.budget)}" if args.budget else ""
        print(f"[month: {format_cost(monthly)}{budget_str}]")
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

            # Built-in commands
            if user_input == "/cost":
                status = engine.budget_status()
                print(f"[session: {format_cost(status['session_cost'])} "
                      f"({status['session_tokens']['input']}in "
                      f"+ {status['session_tokens']['output']}out)]")
                budget_str = ""
                if status["remaining"] is not None:
                    budget_str = (f" | remaining: {format_cost(status['remaining'])}"
                                  f" of {format_cost(status['monthly_budget'])}")
                print(f"[month: {format_cost(status['monthly_cost'])}{budget_str}]")
                continue
            if user_input == "/tokens":
                tokens = engine.token_count()
                if tokens is not None:
                    print(f"[context: {tokens} tokens, "
                          f"{engine.message_count()} messages]")
                else:
                    print("[token count unavailable]")
                continue
            if user_input == "/messages":
                print(f"[{engine.message_count()} messages in context]")
                for i, m in enumerate(engine.messages):
                    role = m["role"]
                    content = m.get("content", "")
                    if isinstance(content, str):
                        preview = content[:60]
                    elif isinstance(content, list):
                        types = [b.get("type", "?") if isinstance(b, dict)
                                 else type(b).__name__ for b in content]
                        preview = f"[{', '.join(types)}]"
                    else:
                        preview = str(content)[:60]
                    print(f"  {i}: {role}: {preview}")
                continue

            # Garden commands
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

            # Send to engine
            try:
                engine.send(
                    content,
                    on_text=lambda t: print(t, end="", flush=True),
                    on_tool=lambda n, i: print(
                        f"\n[tool: {n}({json.dumps(i)[:80]})]"),
                    on_tool_result=lambda n, r: print(
                        f"[result: {r[:200]}{'...' if len(r) > 200 else ''}]"),
                    on_usage=_make_usage_printer(engine),
                )
                print()  # newline after streamed response
            except KeyboardInterrupt:
                print("\n[interrupted]")

    except KeyboardInterrupt:
        print()

    # Final save
    engine.save_session()


def _make_usage_printer(engine):
    def _print_usage(usage):
        cache_info = ""
        if usage.get("cache_read"):
            cache_info += f" cr={usage['cache_read']}"
        if usage.get("cache_write"):
            cache_info += f" cw={usage['cache_write']}"
        cost_str = f" | session: {format_cost(engine.session_cost)}"
        if engine.monthly_budget:
            remaining = engine.monthly_budget - engine.monthly_cost()
            cost_str += f" | left: {format_cost(remaining)}"
        print(f"[in={usage['input_tokens']} out={usage['output_tokens']}"
              f"{cache_info}{cost_str}]")
    return _print_usage


if __name__ == "__main__":
    main()
