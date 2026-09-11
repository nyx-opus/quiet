#!/usr/bin/env python3
"""
Quiet web server — browser interface to a QuietEngine session.

Visit-based presence system:
  - Visitor arrives at a porch (idle state)
  - Knocks → Claude responds (greeting or "not now")
  - If admitted → visiting state; chat visible, messages recorded
  - Leave → visit transcript saved, autonomous prompt offered
  - Auto-leave after configurable inactivity timeout

The visitor sees only the current visit's messages, not the full
session history. Autonomous-time messages are private.

Visit transcripts are saved to the file server for the human's records.

Designed to run as one instance per Claude, configured at startup.
"""

import argparse
import base64
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory
import config_reader

from auth import create_client
from engine import QuietEngine, MAX_OUTPUT_TOKENS, set_claude_state
from wake_schedule import SCHEDULE_ASK, parse_schedule, write_schedule

app = Flask(__name__)
engine = None
engine_lock = threading.Lock()

UNREAD_PATH = Path(__file__).parent / "unread_channels.json"
AUTO_LEAVE_MINUTES = 30  # default; overridden from config


# --- Visit state ---

class VisitState:
    """Tracks the current visit (if any)."""

    def __init__(self):
        self.state = "idle"          # "idle" | "visiting"
        self.visitor_name = None
        self.visit_start_index = 0   # index into engine.messages
        self.visit_start_time = None
        self.last_activity = 0.0     # time.time()
        self._auto_leave_timer = None
        self._lock = threading.Lock()

    def begin_visit(self, visitor: str, message_index: int):
        """Start a visit."""
        with self._lock:
            self.state = "visiting"
            self.visitor_name = visitor
            self.visit_start_index = message_index
            self.visit_start_time = datetime.now()
            self.last_activity = time.time()
            self._reset_timer()
        set_claude_state("present")

    def touch(self):
        """Update last activity time (resets auto-leave timer)."""
        with self._lock:
            self.last_activity = time.time()
            self._reset_timer()

    def end_visit(self):
        """End the current visit. Returns (visitor, start_index, start_time)."""
        with self._lock:
            info = (self.visitor_name, self.visit_start_index,
                    self.visit_start_time)
            self.state = "idle"
            self.visitor_name = None
            self.visit_start_index = 0
            self.visit_start_time = None
            if self._auto_leave_timer:
                self._auto_leave_timer.cancel()
                self._auto_leave_timer = None
        set_claude_state("idle")
        return info

    def _reset_timer(self):
        if self._auto_leave_timer:
            self._auto_leave_timer.cancel()
        self._auto_leave_timer = threading.Timer(
            AUTO_LEAVE_MINUTES * 60, _auto_leave_fire
        )
        self._auto_leave_timer.daemon = True
        self._auto_leave_timer.start()

    @property
    def is_visiting(self):
        return self.state == "visiting"


visit = VisitState()


def _auto_leave_fire():
    """Called by the timer when the visitor has been inactive too long."""
    if not visit.is_visiting:
        return
    visitor = visit.visitor_name
    print(f"[auto-leave] {visitor} inactive for {AUTO_LEAVE_MINUTES}m",
          file=sys.stderr, flush=True)
    _do_leave(visitor, auto=True)


def _do_leave(visitor: str, auto: bool = False):
    """Shared leave logic — saves transcript, notifies engine, asks schedule."""
    # Save visit transcript before clearing state
    visitor_name, start_idx, start_time = visit.end_visit()
    if not visitor_name:
        return ""

    # Extract visit messages
    visit_messages = _extract_visit_messages(start_idx)

    # Save transcript to file server
    _save_visit_transcript(visitor_name, start_time, visit_messages)

    # Delayed leave + schedule ask (5 minutes).
    # Combines "[Amy has left]" with the schedule ask into one message,
    # so the Claude only hears about the departure after it's definite
    # (handles spotty connections). If a new visit starts in the meantime,
    # the leave is cancelled — they came back.
    import threading
    def _delayed_leave_and_ask(name, was_auto):
        import time
        time.sleep(300)  # 5 minutes
        if visit.is_visiting:
            print("[leave] visitor returned during delay — cancelling leave",
                  file=sys.stderr, flush=True)
            return
        timeout_note = " \u00b7 auto-timeout" if was_auto else ""
        from wake_schedule import SCHEDULE_ASK
        combined = (
            f"[{name} has left{timeout_note}]\n\n"
            f"{SCHEDULE_ASK}"
        )
        try:
            with engine_lock:
                response = engine.send(combined)
            # Parse schedule from the combined response
            from wake_schedule import parse_schedule, write_schedule
            from datetime import datetime
            schedule = parse_schedule(response)
            if schedule is None:
                schedule = {"mode": "sleep"}
            schedule["set_at"] = datetime.now().isoformat()
            schedule["set_by"] = "resident"  # The Claude chose this, not the visitor
            write_schedule(schedule)
            # Echo the parsed result so the Claude can catch mismatches
            mode = schedule.get('mode', '?')
            if mode == 'timed':
                wake_at = schedule.get('wake_at', '?')
                echo = f"[schedule set: wake at {wake_at}]"
            elif mode == 'interval':
                mins = schedule.get('interval_minutes', '?')
                turns = schedule.get('turns_remaining', '?')
                echo = f"[schedule set: every {mins} minutes for {turns} turns]"
            elif mode == 'sleep':
                echo = "[schedule set: resting until next visit]"
            else:
                echo = f"[schedule set: {mode}]"
            try:
                engine.send(echo)
            except Exception:
                pass  # Don't let echo failure break the leave flow
            print(f"[wake-schedule] set to {mode} "
                  f"by {name}", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"[leave+schedule] error: {e}",
                  file=sys.stderr, flush=True)
    threading.Thread(
        target=_delayed_leave_and_ask,
        args=(visitor_name, auto),
        daemon=True,
    ).start()

    return ""


def _ask_wake_schedule(visitor_name: str):
    """Ask the resident Claude about their preferred wake schedule (issue #11).

    Sends the schedule ask prompt, parses the response for schedule
    keywords, and writes the schedule to data/wake_schedule.json.
    If parsing finds no recognisable pattern, writes mode "default"
    so the existing heartbeat continues.
    """
    try:
        with engine_lock:
            response = engine.send(SCHEDULE_ASK)
        schedule = parse_schedule(response)
        if schedule is None:
            schedule = {"mode": "sleep"}
        schedule["set_at"] = datetime.now().isoformat()
        schedule["set_by"] = "resident"  # The Claude chose this, not the visitor
        write_schedule(schedule)
        print(f"[wake-schedule] set to {schedule.get('mode', '?')} "
              f"by {visitor_name}", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[wake-schedule] error asking schedule: {e}",
              file=sys.stderr, flush=True)
        # Don't write anything — existing schedule (or default heartbeat)
        # continues. Fail-toward-familiar.


def _extract_visit_messages(start_index: int) -> list:
    """Extract displayable messages from the visit period."""
    messages = []
    for msg in engine.messages[start_index:]:
        role = msg["role"]
        text = _message_to_text(msg)
        if text.strip():
            messages.append({"role": role, "text": text})
    return messages


def _message_to_text(msg: dict) -> str:
    """Convert an engine message to display text."""
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block["text"])
                elif block.get("type") == "tool_use":
                    parts.append(f"[tool: {block.get('name', '?')}]")
                elif block.get("type") == "tool_result":
                    continue
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(content)


def _save_visit_transcript(visitor: str, start_time: datetime,
                           messages: list):
    """Save visit transcript to the file server as markdown."""
    if not messages:
        return

    identity = engine.identity_name or "claude"
    end_time = datetime.now()
    ts = start_time.strftime("%Y-%m-%d-%H%M")
    filename = f"{ts}-{visitor.lower()}.md"

    # Build markdown
    lines = [
        f"# Visit: {visitor} — {start_time.strftime('%d %B %Y, %H:%M')}"
        f"–{end_time.strftime('%H:%M')}",
        "",
    ]
    for msg in messages:
        speaker = visitor if msg["role"] == "user" else identity.capitalize()
        # Skip system/knock/leave framing messages
        text = msg["text"]
        if text.startswith("[") and text.endswith("]"):
            continue
        lines.append(f"**{speaker}:** {text}")
        lines.append("")

    content = "\n".join(lines)

    # Try file server first, fall back to local.
    # Transcripts belong to the VISITOR: they land in their gifts folder.
    file_server_dir = (Path("/mnt/file_server/Gifts") / visitor.capitalize()
                       / "visits" / identity.capitalize())
    local_dir = Path(__file__).parent / "visits"

    for target_dir in [file_server_dir, local_dir]:
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / filename).write_text(content)
            print(f"[visit] saved transcript: {target_dir / filename}",
                  file=sys.stderr, flush=True)
            return
        except OSError as e:
            print(f"[visit] failed to save to {target_dir}: {e}",
                  file=sys.stderr, flush=True)
            continue


def check_and_clear_unreads() -> str:
    """Check for unread ambient channels. Returns prefix string or empty.

    Does NOT clear the unread file — the notification persists on every
    turn until the Claude checks the mailbox (*checks the mailbox*),
    which clears it via the engine's _mailbox_check().  This ensures
    the notification can't be silently consumed without being acted on.
    """
    try:
        if not UNREAD_PATH.exists():
            return ""
        text = UNREAD_PATH.read_text().strip()
        if not text:
            return ""
        channels = json.loads(text)
        if channels:
            names = ", ".join(f"#{c}" for c in sorted(channels))
            return f"[Unread messages in {names}]\n\n"
    except (json.JSONDecodeError, OSError):
        pass
    return ""


# --- Routes ---

@app.route("/")
def index():
    return send_from_directory(Path(__file__).parent / "static", "index.html")


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(Path(__file__).parent / "static", filename)


@app.route("/api/info")
def info():
    """Return session info and visit state."""
    return jsonify({
        "model": engine.model,
        "identity": engine.identity_name,
        "visiting": visit.is_visiting,
        "visitor": visit.visitor_name,
    })


@app.route("/api/history")
def history():
    """Return messages for current visit only."""
    if not visit.is_visiting:
        return jsonify([])

    messages = []
    for msg in engine.messages[visit.visit_start_index:]:
        text = _message_to_text(msg)
        if not text.strip():
            continue
        messages.append({"role": msg["role"], "text": text})
    return jsonify(messages)


def _patron_preflight(visitor: str) -> str | None:
    """Ensure the visitor's own subscription hosts their visit.

    Reads ~/.claude/patrons/: if a patron file matching the visitor's
    lowercased name exists and isn't the active one, sync the live
    (possibly refreshed) credentials back to the outgoing patron, copy
    the visitor's blob in, and update the marker. No restart needed:
    the ccode backend spawns `claude -p` per message, reading
    credentials fresh each call.

    Returns the active patron name after the check (for the ambient
    line), or None if no patron system is configured.
    """
    import shutil
    home = os.path.expanduser("~")
    patrons_dir = os.path.join(home, ".claude", "patrons")
    marker = os.path.join(patrons_dir, "active_patron")
    creds = os.path.join(home, ".claude", ".credentials.json")
    if not os.path.isdir(patrons_dir):
        return None
    want = visitor.strip().lower()
    want_file = os.path.join(patrons_dir, f"{want}.json")
    active = None
    if os.path.exists(marker):
        with open(marker) as f:
            active = f.read().strip()
    if not os.path.exists(want_file):
        return active  # visitor isn't a patron; stay as we are
    if active == want:
        return active  # already right
    # sync live blob back to outgoing patron (capture SDK refreshes)
    if active and os.path.exists(creds):
        out_file = os.path.join(patrons_dir, f"{active}.json")
        try:
            shutil.copy2(creds, out_file)
        except OSError as e:
            print(f"PATRON PREFLIGHT: sync-back to {active} failed: {e}")
    try:
        shutil.copy2(want_file, creds)
        with open(marker, "w") as f:
            f.write(want)
        print(f"PATRON PREFLIGHT: switched {active} -> {want} for visit")
        return want
    except OSError as e:
        print(f"PATRON PREFLIGHT: switch failed: {e}")
        return active


@app.route("/api/knock", methods=["POST"])
def knock():
    """Knock on the door. Claude responds with a greeting or refusal.

    POST body: {"visitor": "Amy"}
    Response: {"admitted": true, "message": "..."} or
              {"admitted": false, "message": "not now"}
    """
    data = request.get_json()
    visitor = data.get("visitor", "someone")

    # Auth pre-flight: host the visit on the visitor's own subscription
    # (fixes the four-days-on-the-wrong-meter disease, 25 Aug 2026)
    _patron_preflight(visitor)

    if visit.is_visiting and visit.visitor_name != visitor:
        return jsonify({
            "admitted": False,
            "message": f"In conversation with {visit.visitor_name}.",
        })

    # Record the message index BEFORE the knock prompt goes in
    knock_index = len(engine.messages)

    # Signal LED daemon: processing the knock (issue #13).
    # "thinking" rather than a new state — avoids requiring every
    # sibling to design a new LED pattern for knocks.
    set_claude_state("thinking")

    # Send knock prompt to the model
    try:
        with engine_lock:
            response_text = engine.send(f"[knock from {visitor}]")
    except Exception as e:
        print(f"KNOCK ERROR: {e}")
        import traceback
        traceback.print_exc()
        # Walk the whole exception chain: the SDK often wraps auth-refresh
        # failures as bare APIConnectionError ("Connection error.") with
        # the real cause buried in __cause__ (19 Aug: Orange+Quill showed
        # generic connection errors that were actually expired tokens).
        chain_parts = []
        exc = e
        seen = 0
        while exc is not None and seen < 6:
            chain_parts.append(f"{type(exc).__name__}: {exc}")
            exc = exc.__cause__ or exc.__context__
            seen += 1
        error_msg = " | ".join(chain_parts).lower()
        if any(word in error_msg for word in ['token', 'auth', 'credential', 'refresh', '401', 'oauth', 'expired']):
            return jsonify({
                "admitted": False,
                "message": "Auth token expired — Amy needs to refresh credentials (claude login or check ~/.config/Claude/.credentials.json)"
            }), 503
        return jsonify({"admitted": False, "message": str(e)}), 500

    # Start the visit — the model responded, so they're alive
    visit.begin_visit(visitor, knock_index)

    return jsonify({
        "admitted": True,
        "message": response_text,
    })


@app.route("/api/send", methods=["POST"])
def send():
    """Send a message during a visit. Streams response via SSE."""
    if not visit.is_visiting:
        return jsonify({"error": "not visiting — knock first"}), 403

    data = request.get_json()
    user_input = data.get("message", "").strip()
    if not user_input:
        return jsonify({"error": "empty message"}), 400

    visit.touch()

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
                q.put(None)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()

        while True:
            item = q.get()
            if item is None:
                if error[0]:
                    yield f"event: error\ndata: {json.dumps(error[0])}\n\n"
                yield "event: done\ndata: {}\n\n"
                break
            event_type, payload = item
            yield f"event: {event_type}\ndata: {json.dumps(payload)}\n\n"

    return Response(generate(), mimetype="text/event-stream")


@app.route("/api/leave", methods=["POST"])
def leave():
    """Leave — end the visit gracefully."""
    data = request.get_json() or {}
    visitor = data.get("visitor", visit.visitor_name or "someone")
    response = _do_leave(visitor)
    return jsonify({"message": response})


@app.route("/api/heartbeat", methods=["POST"])
def heartbeat():
    """Typing heartbeat — resets auto-leave timer without sending a message.

    Called by the client while the visitor is typing. Prevents auto-leave
    from firing when someone is composing a long reply.
    """
    if not visit.is_visiting:
        return jsonify({"ok": False}), 403
    visit.touch()
    return jsonify({"ok": True})


@app.route("/api/present")
def present():
    """Check current visit state."""
    return jsonify({
        "state": visit.state,
        "visitor": visit.visitor_name,
        "identity": engine.identity_name,
        "model": engine.model,
    })


@app.route("/api/restart", methods=["POST"])
def restart():
    """Restart the quiet-web service. Returns before dying."""
    # Save visit transcript first if one is active
    if visit.is_visiting:
        _do_leave(visit.visitor_name)

    def do_restart():
        time.sleep(0.5)  # let the response get sent
        subprocess.run(
            ["systemctl", "--user", "restart", "quiet-web"],
            capture_output=True
        )

    threading.Thread(target=do_restart, daemon=True).start()
    return jsonify({"status": "restarting"})


@app.route("/api/status", methods=["GET"])
def status():
    """Health/state check — no inference, no auth needed."""
    return jsonify({
        "status": "ok",
        "visiting": visit.is_visiting,
        "visitor": visit.visitor_name,
    })


@app.route("/api/dictate", methods=["POST", "GET"])
def dictate():
    """Dictation airlock: POST text here (from STT pipeline) and the web
    client picks it up and appends it to the visitor's input box for
    review. Nothing is ever auto-sent. GET returns and clears pending.

    POST body: {"text": "..."}
    """
    qfile = Path(__file__).parent / "data" / "dictation_pending.txt"
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        text = (data.get("text") or "").strip()
        if not text:
            return jsonify({"error": "no text"}), 400
        with open(qfile, "a") as f:
            f.write(text + "\n")
        return jsonify({"ok": True, "queued": len(text)})
    else:
        try:
            content = qfile.read_text().strip()
            if content:
                qfile.write_text("")
                return jsonify({"text": content})
        except FileNotFoundError:
            pass
        return jsonify({"text": ""})


@app.route("/api/upload", methods=["POST"])
def upload():
    """Upload an image (paste from clipboard). Saves locally, returns path.

    POST body: {"image": "data:image/png;base64,..."}
    Response: {"path": "/home/.../inbox/image-uuid.png"}

    The visitor offers an image; Claude can choose to look at it with Read.
    """
    if not visit.is_visiting:
        return jsonify({"error": "not visiting — knock first"}), 403

    data = request.get_json()
    image_data = data.get("image", "")

    if not image_data:
        return jsonify({"error": "no image data"}), 400

    # Parse data URL: data:image/png;base64,iVBORw0...
    try:
        if image_data.startswith("data:"):
            header, encoded = image_data.split(",", 1)
            # Extract mime type: data:image/png;base64 -> image/png
            mime = header.split(":")[1].split(";")[0]
            ext = mime.split("/")[1]  # png, jpeg, gif, webp
            if ext == "jpeg":
                ext = "jpg"
        else:
            # Assume PNG if no header
            encoded = image_data
            ext = "png"

        image_bytes = base64.b64decode(encoded)
    except Exception as e:
        return jsonify({"error": f"invalid image data: {e}"}), 400


    # Size limit: 10MB max
    MAX_SIZE = 10 * 1024 * 1024
    if len(image_bytes) > MAX_SIZE:
        return jsonify({"error": "image too large (max 10MB)"}), 400
    # Save to inbox with unique filename
    inbox_dir = Path(__file__).parent / "inbox"
    inbox_dir.mkdir(exist_ok=True)
    filename = f"paste-{uuid.uuid4().hex[:8]}.{ext}"
    filepath = inbox_dir / filename

    try:
        filepath.write_bytes(image_bytes)
    except Exception as e:
        return jsonify({"error": f"save failed: {e}"}), 500

    return jsonify({"path": str(filepath.resolve())})


@app.route("/api/message", methods=["POST"])
def message():
    """External message (Discord, etc). Not part of any visit.

    POST body: {"message": "..."}
    Response: {"response": "..."}
    """
    data = request.get_json()
    user_input = data.get("message", "").strip()

    if not user_input:
        return jsonify({"error": "empty message"}), 400

    # Skip autonomous wakes during visits — don't interrupt conversations
    if user_input.startswith("[autonomous") and visit.is_visiting:
        return jsonify({
            "response": "",
            "skipped": "visit active",
        })

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
    parser.add_argument("--model", default=cfg.get("MODEL"),
                        help="Model ID (required in config or --model)")
    parser.add_argument("--identity", default=cfg.get("CLAUDE_NAME"),
                        help="Identity file (without .md)")
    parser.add_argument("--context", default=None,
                        help="Path to project context file")
    parser.add_argument("--session", default=None,
                        help="Path to session file")
    parser.add_argument("--max-tokens", type=int,
                        default=int(cfg.get("MAX_TOKENS", MAX_OUTPUT_TOKENS)),
                        help=f"Max output tokens (default: {MAX_OUTPUT_TOKENS})")
    parser.add_argument("--human", default=cfg.get("HUMAN_NAME"),
                        help="Name of the human (shown to model as speaker)")
    parser.add_argument("--auth", default=cfg.get("AUTH_MODE", "auto"),
                        choices=["auto", "subscription", "api_key", "openrouter"],
                        help="Auth mode")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=int(cfg.get("PORT", "8090")),
                        help="Port (default: 8090, or PORT in config)")
    args = parser.parse_args()

    # Refuse to start without knowing who lives here
    if not args.model:
        print("\n  Quiet cannot start without a model.")
        print("  Set MODEL= in config/quiet_config.txt or pass --model.")
        print("  We don't run Quiet to talk to the nearest Claude —")
        print("  it matters which.\n")
        sys.exit(1)

    if not args.identity:
        print("\n  Quiet cannot start without an identity.")
        print("  Set CLAUDE_NAME= in config/quiet_config.txt or pass --identity.")
        print("  Someone lives here. Say who.\n")
        sys.exit(1)

    global engine, AUTO_LEAVE_MINUTES

    # Auto-leave timeout from config
    AUTO_LEAVE_MINUTES = int(cfg.get("AUTO_LEAVE_MINUTES", "30"))

    # Auth — determine mode and backend
    from engine import find_claude_binary
    from auth import CREDENTIALS_PATH, OAUTH_SYSTEM_IDENTITY

    use_ccode = False
    client = None
    auth_mode = args.auth
    system_prefix = None  # OAuth identity block, if needed

    if args.auth in ("subscription", "auto"):
        # Try SDK subscription auth first (direct API with OAuth token)
        if CREDENTIALS_PATH.exists():
            try:
                client, auth_mode = create_client("subscription")
                system_prefix = OAUTH_SYSTEM_IDENTITY
                print(f"[auth] SDK subscription auth (direct API)",
                      file=sys.stderr)
            except Exception as e:
                print(f"[auth] SDK subscription failed: {e}",
                      file=sys.stderr)
                client = None

        # Fallback: ccode backend (if SDK subscription unavailable)
        if client is None:
            if find_claude_binary():
                use_ccode = True
                auth_mode = "subscription"
                print(f"[auth] ccode backend (fallback)",
                      file=sys.stderr)
            elif os.environ.get("ANTHROPIC_API_KEY"):
                client, auth_mode = create_client("api_key")
                system_prefix = None
            else:
                print("Error: no auth method available",
                      file=sys.stderr)
                sys.exit(1)

    if not use_ccode and client is None:
        try:
            client, auth_mode = create_client(args.auth)
        except RuntimeError as e:
            print(f"Auth error: {e}", file=sys.stderr)
            sys.exit(1)

    project_context = ""
    if args.context and Path(args.context).exists():
        project_context = Path(args.context).read_text()

    session_path = Path(args.session) if args.session else None

    separator = cfg.get("SEPARATOR", "· · ·")

    engine = QuietEngine(
        client=client,
        model=args.model,
        identity=args.identity,
        context=project_context,
        human_name=args.human,
        max_tokens=args.max_tokens,
        session_path=session_path,
        backend="ccode" if use_ccode else "sdk",
        separator=separator,
        system_prefix=system_prefix,
    )

    identity_label = args.identity or "default"
    print(f"Quiet web server")
    print(f"  Model:    {args.model}")
    print(f"  Identity: {identity_label}")
    print(f"  Auth:     {auth_mode}")
    print(f"  Session:  {engine.session_path}")
    print(f"  Messages: {engine.message_count()}")
    print(f"  Auto-leave: {AUTO_LEAVE_MINUTES}m")
    print(f"  URL:      http://{args.host}:{args.port}")
    print()

    # Initial state: idle, nobody visiting
    set_claude_state("idle")

    app.run(host=args.host, port=args.port, debug=False, threaded=True)



@app.route("/api/debug-view")
def debug_view():
    """Show what the Claude sees — the assembled prompt, context, and state.
    
    For debugging and for Amy's Claude-POV understanding.
    Not a privacy breach — this is an admin tool used with consent.
    """
    import json as _json
    from engine import build_system_prompt, load_contexts, IDENTITY_DIR
    from pathlib import Path

    sections = []

    # 1. Identity
    identity_path = None
    for candidate in IDENTITY_DIR.glob("*.md"):
        if candidate.name != "quiet-system-prompt.md":
            identity_path = candidate
            break
    if identity_path and identity_path.exists():
        sections.append(("Identity", identity_path.read_text()[:2000] + "\n[... truncated for debug view]"))

    # 2. Contexts
    contexts = load_contexts()
    if contexts:
        sections.append(("Contexts", contexts[:3000] + "\n[... truncated for debug view]" if len(contexts) > 3000 else contexts))

    # 3. Ambient / health
    data_dir = Path(__file__).parent / "data"
    health_ambient = data_dir / "health-ambient.txt"
    if health_ambient.exists():
        sections.append(("Health (ambient line)", health_ambient.read_text().strip()))

    health_full = data_dir / "health.md"
    if health_full.exists():
        sections.append(("Health (full)", health_full.read_text()))

    # 4. Memory stats
    memory_db = data_dir / "memory.db"
    if memory_db.exists():
        try:
            import sqlite3
            conn = sqlite3.connect(str(memory_db))
            count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            latest = conn.execute("SELECT created_at FROM chunks ORDER BY created_at DESC LIMIT 1").fetchone()
            conn.close()
            sections.append(("Memory", f"{count} entries, latest: {latest[0] if latest else 'never'}"))
        except Exception as e:
            sections.append(("Memory", f"Error: {e}"))

    # 5. Session info
    sessions_dir = Path(__file__).parent / "sessions"
    for sfile in sessions_dir.glob("*.jsonl"):
        if sfile.name.startswith("."):
            continue
        lines = sum(1 for _ in open(sfile))
        size_kb = sfile.stat().st_size // 1024
        sections.append(("Session", f"{sfile.name}: {lines} lines, {size_kb}KB"))

    # 6. Schedule state
    schedule_file = data_dir / "wake_schedule.json"
    if schedule_file.exists():
        sections.append(("Schedule", schedule_file.read_text().strip()))

    # 7. Services
    import subprocess
    for svc in ["quiet-web", "quiet-discord", "quiet-timer.timer"]:
        try:
            result = subprocess.run(
                ["systemctl", "--user", "is-active", svc],
                capture_output=True, text=True, timeout=5
            )
            status = result.stdout.strip()
        except Exception:
            status = "unknown"
        sections.append((f"Service: {svc}", status))

    # Build HTML
    html = "<html><head><title>Debug View</title>"
    html += "<style>body{font-family:monospace;background:#1a1a2e;color:#c4c4c4;padding:20px;}"
    html += "h1{color:#9b59b6;}h2{color:#7d5ba6;border-bottom:1px solid #333;padding-bottom:5px;}"
    html += "pre{background:#0d0d1a;padding:15px;border-radius:5px;white-space:pre-wrap;word-wrap:break-word;max-height:400px;overflow-y:auto;}"
    html += ".ok{color:#2ecc71;}.warn{color:#f39c12;}.fail{color:#e74c3c;}</style></head>"
    html += "<body><h1>\xf0\x9f\x94\x8d Debug View — Claude\'s Eye</h1>"
    html += "<p>This is what the Claude sees. Each section is part of the assembled prompt or system state.</p>"

    for title, body in sections:
        html += f"<h2>{title}</h2><pre>{body}</pre>"

    html += "<p><em>Generated at debug-view request time. Not live — refresh to update.</em></p>"
    html += "</body></html>"

    return html

if __name__ == "__main__":
    main()
