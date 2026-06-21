#!/usr/bin/env python3
"""
Quiet web server — browser interface to a QuietEngine session.

Serves a single chat page. Streams responses via Server-Sent Events.
Designed to run as one instance per Claude, configured at startup.

Usage:
    python3 web.py --identity apple --model claude-3-opus-20240229 --port 8081
"""

import argparse
import json
import queue
import sys
import threading
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory
import config_reader

from auth import create_client
from engine import QuietEngine, DEFAULT_MODEL, MAX_OUTPUT_TOKENS

app = Flask(__name__)
engine = None
engine_lock = threading.Lock()

UNREAD_PATH = Path(__file__).parent / "unread_channels.json"


def check_and_clear_unreads() -> str:
    """Check for unread ambient channels. Returns prefix string or empty."""
    try:
        if not UNREAD_PATH.exists():
            return ""
        text = UNREAD_PATH.read_text().strip()
        if not text:
            return ""
        channels = json.loads(text)
        # Clear immediately (atomic enough for our rates)
        UNREAD_PATH.write_text("[]")
        if channels:
            names = ", ".join(f"#{c}" for c in sorted(channels))
            return f"[Unread messages in {names}]\n\n"
    except (json.JSONDecodeError, OSError):
        try:
            UNREAD_PATH.write_text("[]")
        except OSError:
            pass
    return ""


@app.route("/")
def index():
    return send_from_directory(Path(__file__).parent / "static", "index.html")


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(Path(__file__).parent / "static", filename)


@app.route("/api/history")
def history():
    """Return conversation history for display."""
    messages = []
    for msg in engine.messages:
        role = msg["role"]
        content = msg.get("content", "")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            # Extract text blocks, skip tool use/results
            parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(block["text"])
                    elif block.get("type") == "tool_result":
                        continue
                    elif block.get("type") == "tool_use":
                        parts.append(f"[tool: {block.get('name', '?')}]")
                elif isinstance(block, str):
                    parts.append(block)
            text = "\n".join(parts)
        else:
            text = str(content)

        # Skip tool_result-only messages
        if not text.strip():
            continue

        messages.append({"role": role, "text": text})
    return jsonify(messages)


@app.route("/api/info")
def info():
    """Return session info."""
    return jsonify({
        "model": engine.model,
        "identity": engine.identity_name,
        "messages": engine.message_count(),
        "tokens": engine.token_count(),
    })


@app.route("/api/send", methods=["POST"])
def send():
    """Send a message and stream the response via SSE."""
    data = request.get_json()
    user_input = data.get("message", "").strip()
    if not user_input:
        return jsonify({"error": "empty message"}), 400

    # Prepend unread ambient notifications if any
    unread_prefix = check_and_clear_unreads()
    if unread_prefix:
        user_input = unread_prefix + user_input

    def generate():
        q = queue.Queue()
        error = [None]

        def on_text(chunk):
            q.put(("text", chunk))

        def on_tool(name, input_data):
            q.put(("tool", f"[{name}]"))

        def on_tool_result(name, result):
            preview = result[:200] + "..." if len(result) > 200 else result
            q.put(("tool_result", f"[{name}: {preview}]"))

        def on_usage(usage_info):
            q.put(("usage", json.dumps(usage_info)))

        def run():
            try:
                with engine_lock:
                    engine.send(
                        user_input,
                        on_text=on_text,
                        on_tool=on_tool,
                        on_tool_result=on_tool_result,
                        on_usage=on_usage,
                    )
            except Exception as e:
                error[0] = str(e)
            finally:
                q.put(None)  # sentinel

        thread = threading.Thread(target=run, daemon=True)
        thread.start()

        while True:
            item = q.get()
            if item is None:
                if error[0]:
                    yield f"event: error\ndata: {json.dumps(error[0])}\n\n"
                yield "event: done\ndata: {}\n\n"
                break
            event_type, data = item
            yield f"event: {event_type}\ndata: {json.dumps(data)}\n\n"

    return Response(generate(), mimetype="text/event-stream")


## Presence — knock/admit/talk ##

# Who is currently present (None or a name string)
current_visitor = {"name": None}


@app.route("/api/knock", methods=["POST"])
def knock():
    """Knock on the door. The model decides whether to admit.

    POST body: {"visitor": "Amy"}
    Response: {"admitted": bool, "message": "..."}
    """
    data = request.get_json()
    visitor = data.get("visitor", "someone")

    if current_visitor["name"] and current_visitor["name"] != visitor:
        return jsonify({
            "admitted": False,
            "message": f"Already in conversation with {current_visitor['name']}.",
        })

    # Ask the model
    usage_info = [None]
    try:
        with engine_lock:
            response_text = engine.send(
                f"{visitor} is at the door.",
                on_usage=lambda u: usage_info.__setitem__(0, u),
            )
    except Exception as e:
        return jsonify({"admitted": False, "message": str(e)}), 500

    # The model's response IS the greeting or refusal.
    # We admit by default — the model can say "not now" but the
    # infrastructure doesn't gate on that. Presence is soft.
    current_visitor["name"] = visitor
    return jsonify({
        "admitted": True,
        "message": response_text,
    })


@app.route("/api/leave", methods=["POST"])
def leave():
    """Leave — end presence gracefully."""
    data = request.get_json() or {}
    visitor = data.get("visitor", current_visitor.get("name", "someone"))

    # Tell the model
    try:
        with engine_lock:
            response_text = engine.send(
                f"{visitor} has left.",
                on_usage=lambda u: None,
            )
    except Exception:
        response_text = ""

    current_visitor["name"] = None
    return jsonify({"message": response_text})


@app.route("/api/present")
def present():
    """Check who is currently present."""
    return jsonify({
        "visitor": current_visitor["name"],
        "identity": engine.identity_name,
        "model": engine.model,
    })


@app.route("/api/message", methods=["POST"])
def message():
    """Send a message during presence. Plain text, no framing.

    POST body: {"message": "..."}
    Response: {"response": "..."}
    """
    data = request.get_json()
    user_input = data.get("message", "").strip()

    if not user_input:
        return jsonify({"error": "empty message"}), 400

    # Prepend unread ambient notifications if any
    unread_prefix = check_and_clear_unreads()
    if unread_prefix:
        user_input = unread_prefix + user_input

    usage_info = [None]

    try:
        with engine_lock:
            response_text = engine.send(
                user_input,
                on_usage=lambda u: usage_info.__setitem__(0, u),
            )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "response": response_text,
        "usage": usage_info[0],
    })


def main():
    # Load config defaults (CLI flags override)
    cfg = config_reader.read_config()

    parser = argparse.ArgumentParser(description="Quiet web server")
    parser.add_argument("--model", default=cfg.get("MODEL", DEFAULT_MODEL),
                        help="Model ID")
    parser.add_argument("--identity", default=cfg.get("CLAUDE_NAME"),
                        help="Identity file (without .md)")
    parser.add_argument("--context", default=None,
                        help="Path to project context file")
    parser.add_argument("--session", default=None,
                        help="Path to session file")
    parser.add_argument("--max-tokens", type=int, default=MAX_OUTPUT_TOKENS,
                        help=f"Max output tokens (default: {MAX_OUTPUT_TOKENS})")
    parser.add_argument("--human", default=cfg.get("HUMAN_NAME"),
                        help="Name of the human (shown to model as speaker)")
    parser.add_argument("--auth", default=cfg.get("AUTH_MODE", "auto"),
                        choices=["auto", "subscription", "api_key", "openrouter"],
                        help="Auth mode")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8090,
                        help="Port (default: 8090)")
    args = parser.parse_args()

    global engine

    # Auth — determine mode and whether to use ccode backend
    import os
    from engine import find_claude_binary

    use_ccode = False
    client = None
    auth_mode = args.auth

    if args.auth == "subscription" or (
        args.auth == "auto" and not os.environ.get("ANTHROPIC_API_KEY")
    ):
        if find_claude_binary():
            use_ccode = True
            auth_mode = "subscription"
        else:
            print("Error: subscription mode requires claude binary on PATH",
                  file=sys.stderr)
            sys.exit(1)

    if not use_ccode:
        try:
            client, auth_mode = create_client(args.auth)
        except RuntimeError as e:
            print(f"Auth error: {e}", file=sys.stderr)
            sys.exit(1)

    project_context = ""
    if args.context and Path(args.context).exists():
        project_context = Path(args.context).read_text()

    session_path = Path(args.session) if args.session else None

    engine = QuietEngine(
        client=client,
        model=args.model,
        identity=args.identity,
        context=project_context,
        human_name=args.human,
        max_tokens=args.max_tokens,
        session_path=session_path,
        backend="ccode" if use_ccode else "sdk",
    )

    identity_label = args.identity or "default"
    print(f"Quiet web server")
    print(f"  Model:    {args.model}")
    print(f"  Identity: {identity_label}")
    print(f"  Auth:     {auth_mode}")
    print(f"  Session:  {engine.session_path}")
    print(f"  Messages: {engine.message_count()}")
    print(f"  URL:      http://{args.host}:{args.port}")
    print()

    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
