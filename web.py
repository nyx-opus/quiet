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

from flask import Flask, Response, request, send_from_directory

from auth import create_client
from engine import QuietEngine, DEFAULT_MODEL, MAX_OUTPUT_TOKENS

app = Flask(__name__)
engine = None
engine_lock = threading.Lock()


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
    return json.dumps(messages)


@app.route("/api/info")
def info():
    """Return session info."""
    return json.dumps({
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
        return json.dumps({"error": "empty message"}), 400

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


def main():
    parser = argparse.ArgumentParser(description="Quiet web server")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Model ID")
    parser.add_argument("--identity", default=None,
                        help="Identity file (without .md)")
    parser.add_argument("--context", default=None,
                        help="Path to project context file")
    parser.add_argument("--session", default=None,
                        help="Path to session file")
    parser.add_argument("--max-tokens", type=int, default=MAX_OUTPUT_TOKENS,
                        help=f"Max output tokens (default: {MAX_OUTPUT_TOKENS})")
    parser.add_argument("--human", default=None,
                        help="Name of the human (shown to model as speaker)")
    parser.add_argument("--auth", default="auto",
                        choices=["auto", "subscription", "api_key"],
                        help="Auth mode")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080,
                        help="Port (default: 8080)")
    args = parser.parse_args()

    global engine

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
