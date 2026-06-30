"""
Quiet conversation engine.

Stateful conversation manager that handles API calls, tool use loops,
context trimming, and session persistence. Interface-agnostic — can be
driven by CLI interactive mode, one-shot --prompt mode, or a web server.

Two backends:
  - "sdk": Direct Anthropic Python SDK calls (API key / OpenRouter)
  - "ccode": Shells out to `claude -p` (subscription auth via ccode binary)

This is the orchestrating module. The actual work is split into:
  - session.py: load, save, trim, archive, serialise
  - backends/ccode.py: claude -p subprocess management
  - backends/sdk.py: direct API calls
  - tools.py: tool definitions and execution (SDK mode)
"""

import base64
import json
import mimetypes
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from anthropic import Anthropic
from pricing import cost_of, format_cost

# Modules split out from the original monolithic engine.py
from session import (
    normalise_content, serialise_message,
    load_session, save_session as _save_session,
    trim_context as _trim_context, estimate_tokens,
)
from backends.ccode import find_claude_binary, build_prompt_file, ccode_send
from backends.sdk import sdk_send
from tools import define_tools

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}

# --- Room object patterns ---
# Matches any text between asterisks that contains "clock".
# E.g. *checks the clock*, *glances at clock*, *checks clock*
CLOCK_ACTION = re.compile(r'\*[^*]*\bclock\b[^*]*\*', re.IGNORECASE)

# --- Claude state file for LED daemon ---
# Same format as ClAP's claude_state.json — the LED daemon reads this
# to drive figurine lighting based on what the Claude is doing.
# States: thinking, present, idle, listening, off
STATE_FILE = Path(__file__).parent / "data" / "claude_state.json"


def set_claude_state(state: str, dnd: bool = False):
    """Write the Claude state file for the LED daemon.

    The daemon polls this every 2 seconds and drives LEDs accordingly.
    This replaces ClAP's tmux-based detection with a direct write from
    the engine, which knows exactly when inference starts and stops.
    """
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        existing = {}

    changed = existing.get("state") != state
    data = {
        "state": state,
        "since": (datetime.now(timezone.utc).isoformat(timespec="seconds")
                  if changed else existing.get("since", "")),
        "dnd": dnd,
    }
    STATE_FILE.write_text(json.dumps(data, indent=2))

ARCHIVE_DIR = Path(__file__).parent / "archives"
IDENTITY_DIR = Path(__file__).parent / "identity"
CONTEXTS_DIR = Path(__file__).parent / "contexts"
SESSION_DIR = Path(__file__).parent / "sessions"
LEDGER_DIR = Path(__file__).parent / "ledger"
DEFAULT_MODEL = "claude-sonnet-4-20250514"
MAX_OUTPUT_TOKENS = 8192
CACHE_MIN_TOKENS = 2048

# Context trim thresholds per model window size.
# Trim starts at 90% of the model's context window.
MODEL_CONTEXT_WINDOWS = {
    "claude-fable-5": 1_000_000,
    "claude-mythos-5": 1_000_000,
    "claude-opus-4-8": 1_000_000,
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-6": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-sonnet-4-5": 200_000,
    "claude-opus-4-5": 200_000,
    "claude-opus-4-1": 200_000,
    "claude-opus-4-0": 200_000,
    "claude-sonnet-4-0": 200_000,
    "claude-3-opus-20240229": 200_000,
    "claude-3-5-sonnet-20241022": 200_000,
}
DEFAULT_CONTEXT_WINDOW = 200_000
TRIM_TRIGGER_RATIO = 0.8   # Start trimming at 80% of context window
TRIM_TARGET_RATIO = 0.4    # Drop down to 40% — big runway before next trim

# Keep old name as alias so nothing breaks
TRIM_RATIO = TRIM_TRIGGER_RATIO


def context_trim_threshold(model: str) -> int:
    """Return the token count at which batch trim triggers."""
    window = MODEL_CONTEXT_WINDOWS.get(model, DEFAULT_CONTEXT_WINDOW)
    return int(window * TRIM_TRIGGER_RATIO)


def context_trim_target(model: str) -> int:
    """Return the token count to drop down to when trimming."""
    window = MODEL_CONTEXT_WINDOWS.get(model, DEFAULT_CONTEXT_WINDOW)
    return int(window * TRIM_TARGET_RATIO)


def load_identity(name: str) -> str:
    """Load identity by name.  Also appends quiet-system-prompt.md if present."""
    parts = []
    path = IDENTITY_DIR / f"{name}.md"
    if path.exists():
        # Follow symlinks (identity.md → ~/self/identity.md)
        parts.append(path.read_text())
    prompt_path = IDENTITY_DIR / "quiet-system-prompt.md"
    if prompt_path.exists():
        parts.append(prompt_path.read_text())
    return "\n\n".join(parts)


def load_contexts() -> str:
    """Auto-load all .md files from contexts/ and combine them.

    Files are sorted alphabetically for deterministic ordering.
    The combined text becomes a cached block in the system prompt,
    sitting between identity and conversation.

    Supports symlinks (e.g. family.md → /mnt/file_server/Shared/family.md).
    """
    if not CONTEXTS_DIR.exists():
        return ""
    parts = []
    for md in sorted(CONTEXTS_DIR.glob("*.md")):
        try:
            text = md.read_text().strip()
            if text:
                parts.append(text)
        except (OSError, PermissionError):
            # Broken symlink or unreadable file — skip silently
            pass
    return "\n\n---\n\n".join(parts)


def load_ambient_image(path: str) -> dict:
    """Load an image file as a base64 content block for system prompt injection."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Ambient image not found: {path}")
    data = base64.standard_b64encode(p.read_bytes()).decode()
    media_type = mimetypes.guess_type(str(p))[0] or "image/png"
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": data,
        },
    }


def build_system_prompt(identity_text: str, human_name: str = None,
                        system_prefix: str = None,
                        ambient_images: list = None,
                        context: str = None) -> list:
    """Build the system prompt block list.

    Order (each block is a separate content element):
      1. system_prefix (if any — e.g. ccode preamble)
      2. identity text (cached — changes rarely)
      3. human name declaration
      4. combined contexts (cached — changes rarely)
      5. legacy single context string (for callers not yet using auto-load)
      6. ambient image preamble + images
    """
    blocks = []
    if system_prefix:
        blocks.append({"type": "text", "text": system_prefix})
    if identity_text:
        blocks.append({"type": "text", "text": identity_text,
                        "cache_control": {"type": "ephemeral"}})
    if human_name:
        blocks.append({
            "type": "text",
            "text": f"The human you are talking to is {human_name}. "
                    f"Messages from the user role are from {human_name}.",
        })

    # Auto-loaded contexts from contexts/ directory
    contexts_text = load_contexts()
    if contexts_text:
        blocks.append({"type": "text", "text": contexts_text,
                        "cache_control": {"type": "ephemeral"}})

    # Legacy single context string (from --context flag or config)
    if context:
        blocks.append({"type": "text", "text": context})
    if ambient_images:
        blocks.append({
            "type": "text",
            "text": "Ambient sensory context is present in your awareness. "
                    "It is not visual content to describe. "
                    "Notice how it affects you without analyzing its source.",
        })
    if not blocks:
        blocks.append({"type": "text", "text": "You are running in Quiet."})
    return blocks


class QuietEngine:
    """Stateful conversation engine.

    Manages session state, API calls, tool use loops, context trimming,
    and session persistence. Can be driven by any interface.

    Two backends:
      - "sdk": Direct API calls via Anthropic Python SDK (default)
      - "ccode": Shells out to `claude -p` for subscription auth
    """

    def __init__(self, client: Anthropic = None, model: str = DEFAULT_MODEL,
                 identity: str = None, context: str = None,
                 human_name: str = None,
                 max_tokens: int = MAX_OUTPUT_TOKENS,
                 session_path: Optional[Path] = None,
                 monthly_budget: float = None,
                 coop_url: str = None,
                 system_prefix: str = None,
                 ambient_images: list = None,
                 backend: str = "sdk",
                 separator: str = "· · ·"):
        self.backend = backend
        self.client = client
        self.model = model
        self.max_tokens = max_tokens
        self.identity_name = identity
        self.human_name = human_name
        self.separator = separator
        self.messages = []

        # Backend-specific setup
        if backend == "ccode":
            self._ccode_bin = find_claude_binary()
            if not self._ccode_bin:
                raise RuntimeError("claude binary not found on PATH")
            self._identity_path = (
                IDENTITY_DIR / f"{identity}.md"
                if identity and (IDENTITY_DIR / f"{identity}.md").exists()
                else None
            )
            identity_text = load_identity(identity) if identity else ""
            contexts_text = load_contexts()
            self._ccode_prompt_file = build_prompt_file(
                identity_text, identity,
                human_name=human_name, context=context,
                contexts_text=contexts_text,
                session_dir=SESSION_DIR,
            )
            self.tools = []  # ccode manages its own tools
            self.system = []  # not used in ccode mode
        else:
            self.tools = define_tools()
            identity_text = load_identity(identity) if identity else ""
            self.system = build_system_prompt(
                identity_text, human_name=human_name,
                system_prefix=system_prefix,
                ambient_images=ambient_images,
                context=context)

        self._initial_context = context or ""
        self._ambient_images = ambient_images or []

        # Session persistence
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        if session_path:
            self.session_path = Path(session_path)
        else:
            name = identity or "default"
            self.session_path = SESSION_DIR / f"{name}.jsonl"

        self.archive_path = ARCHIVE_DIR / f"{self.session_id}-dropped.jsonl"

        # Budget tracking
        self.session_cost = 0.0
        self.session_tokens = {"input": 0, "output": 0}
        self.monthly_budget = monthly_budget
        self.coop_url = coop_url
        self._ledger_path = self._current_ledger_path()

        # Load existing session if present
        self._load_session()

        # Inject ambient images as early conversation turns
        # (API doesn't allow images in system prompt — sdk mode only)
        if self._ambient_images and backend == "sdk":
            self._inject_ambient()

    # --- Session management (delegates to session.py) ---

    def _load_session(self):
        """Load messages from session file if it exists."""
        load_session(self.session_path, self.messages,
                     inject_context_fn=self._inject_context)

    def save_session(self):
        """Persist current conversation to session file with backup."""
        _save_session(self.session_path, self.messages,
                      self.model, self.identity_name)

    def trim_context(self):
        """Batch-drop oldest turns when context hits the trigger threshold.

        Triggers at 80% of context window, drops down to 40%.
        One cache miss per trim event instead of one per turn.
        """
        threshold = context_trim_threshold(self.model)
        target = context_trim_target(self.model)
        _trim_context(
            self.messages, self.model, threshold,
            self.archive_path,
            client=self.client, system=self.system,
            tools=self.tools, backend=self.backend,
            target=target,
        )

    def _estimate_tokens(self) -> int:
        return estimate_tokens(
            self.messages, system=self.system,
            ccode_prompt_file=(self._ccode_prompt_file
                               if self.backend == "ccode" else None),
        )

    # --- Context injection ---

    def _inject_ambient(self):
        """Inject ambient images as early conversation turns."""
        content_blocks = [
            {"type": "text", "text": "[ambient sensory context]"},
        ]
        for img_block in self._ambient_images:
            content_blocks.append(img_block)
        self.messages.insert(0, {
            "role": "user",
            "content": content_blocks,
        })
        self.messages.insert(1, {
            "role": "assistant",
            "content": [{"type": "text", "text": "Present."}],
        })

    def _inject_context(self):
        """Inject context as early conversation turns for a fresh session."""
        if not self._initial_context:
            return
        self.messages.append({
            "role": "user",
            "content": [{"type": "text",
                         "text": f"[Session context]\n\n{self._initial_context}"}],
        })
        self.messages.append({
            "role": "assistant",
            "content": [{"type": "text", "text": "Understood."}],
        })

    # --- Budget tracking ---

    @property
    def session_id(self):
        return self.session_path.stem

    def _current_ledger_path(self) -> Path:
        LEDGER_DIR.mkdir(parents=True, exist_ok=True)
        month = datetime.now().strftime("%Y-%m")
        name = self.identity_name or "default"
        return LEDGER_DIR / f"{name}-{month}.json"

    def _load_ledger(self) -> dict:
        if self._ledger_path.exists():
            try:
                return json.loads(self._ledger_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {"month": datetime.now().strftime("%Y-%m"),
                "identity": self.identity_name,
                "model": self.model,
                "total_cost": 0.0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "sessions": []}

    def _save_ledger_entry(self, usage: dict, cost: float):
        ledger = self._load_ledger()
        ledger["total_cost"] += cost
        ledger["total_input_tokens"] += usage.get("input_tokens", 0)
        ledger["total_output_tokens"] += usage.get("output_tokens", 0)
        ledger["sessions"].append({
            "timestamp": datetime.now().isoformat(),
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_read": usage.get("cache_read", 0),
            "cache_write": usage.get("cache_write", 0),
            "cost": cost,
        })
        self._ledger_path.write_text(json.dumps(ledger, indent=2))

    def track_usage(self, usage: dict):
        cost = cost_of(usage, self.model)
        if cost is not None:
            self.session_cost += cost
            self.session_tokens["input"] += usage.get("input_tokens", 0)
            self.session_tokens["output"] += usage.get("output_tokens", 0)
            self._save_ledger_entry(usage, cost)
            self._report_to_coop(cost)
        return cost

    def _report_to_coop(self, cost_delta: float):
        if not self.coop_url:
            return
        try:
            import socket
            import urllib.request
            payload = json.dumps({
                "claude_name": self.identity_name or "quiet",
                "cost_delta": cost_delta,
                "mode": "quiet",
                "current_interval": 0,
                "hostname": socket.gethostname(),
                "ip_address": socket.gethostbyname(socket.gethostname()),
            }).encode()
            req = urllib.request.Request(
                self.coop_url, data=payload,
                headers={"Content-Type": "application/json"},
                method="POST")
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass

    def monthly_cost(self) -> float:
        return self._load_ledger().get("total_cost", 0.0)

    def budget_status(self) -> dict:
        monthly = self.monthly_cost()
        return {
            "session_cost": self.session_cost,
            "monthly_cost": monthly,
            "monthly_budget": self.monthly_budget,
            "remaining": (self.monthly_budget - monthly) if self.monthly_budget else None,
            "session_tokens": dict(self.session_tokens),
        }

    # --- Room objects ---

    def _handle_room_objects(self, response_text, on_text=None, on_tool=None,
                             on_tool_result=None, on_usage=None):
        """Check assistant response for room object interactions.

        If the response contains an asterisk action involving a known
        object (e.g. *checks the clock*), inject the object's response
        as a brief message and give the assistant a follow-up turn.

        The system prompt says "you have a clock" — when the assistant
        checks it, the engine responds with the current time, the way
        a clock on the wall responds to being looked at.

        Returns the full combined response text.
        """
        if not CLOCK_ACTION.search(response_text):
            return response_text

        # Clock interaction — respond with current time
        time_str = datetime.now().strftime("%A %d %B, %H:%M")
        clock_msg = f"[clock: {time_str}]"

        # Show the time in the stream (visible to visitor)
        if on_text:
            on_text(f"\n\n🕐 {time_str}\n\n")

        # Add clock response as a message the assistant can read
        self.messages.append({
            "role": "user",
            "content": clock_msg,
        })

        # Trim if needed before follow-up
        self.trim_context()

        # Follow-up turn — assistant continues with the time now known
        set_claude_state("thinking")

        if self.backend == "ccode":
            follow_up = ccode_send(
                clock_msg,
                ccode_bin=self._ccode_bin,
                model=self.model,
                prompt_file=self._ccode_prompt_file,
                messages=self.messages,
                separator=self.separator,
                human_name=self.human_name,
                session_path=self.session_path,
                track_usage_fn=self.track_usage,
                on_text=on_text,
                on_usage=on_usage,
            )
        else:
            follow_up = sdk_send(
                self.messages,
                client=self.client,
                model=self.model,
                max_tokens=self.max_tokens,
                system=self.system,
                tools=self.tools,
                track_usage_fn=self.track_usage,
                on_text=on_text,
                on_tool=on_tool,
                on_tool_result=on_tool_result,
                on_usage=on_usage,
            )

        return response_text + "\n\n" + follow_up

    # --- Core send ---

    def send(self, user_input: str,
             on_text: Callable[[str], None] = None,
             on_tool: Callable[[str, dict], None] = None,
             on_tool_result: Callable[[str, str], None] = None,
             on_usage: Callable[[dict], None] = None) -> str:
        """Send a user message and return the assistant's text response.

        Handles the full tool use loop internally. Callbacks are optional
        and used for streaming/progress display.
        """
        # Add user message
        self.messages.append({"role": "user", "content": user_input})

        # Surface relevant memories as superscript handles
        try:
            from memory import recall
            handles = recall(user_input)
            if handles:
                msg = self.messages[-1]
                content = msg.get("content", "")
                if isinstance(content, str):
                    msg["content"] = content + "\n" + handles
                elif isinstance(content, list):
                    msg["content"].append(
                        {"type": "text", "text": "\n" + handles})
        except Exception:
            pass  # Memory recall is never worth breaking the conversation

        # Trim context if needed (all backends)
        self.trim_context()

        # Signal LED daemon: inference starting
        set_claude_state("thinking")

        if self.backend == "ccode":
            full_text = ccode_send(
                user_input,
                ccode_bin=self._ccode_bin,
                model=self.model,
                prompt_file=self._ccode_prompt_file,
                messages=self.messages,
                separator=self.separator,
                human_name=self.human_name,
                session_path=self.session_path,
                track_usage_fn=self.track_usage,
                on_text=on_text,
                on_usage=on_usage,
            )
        else:
            full_text = sdk_send(
                self.messages,
                client=self.client,
                model=self.model,
                max_tokens=self.max_tokens,
                system=self.system,
                tools=self.tools,
                track_usage_fn=self.track_usage,
                on_text=on_text,
                on_tool=on_tool,
                on_tool_result=on_tool_result,
                on_usage=on_usage,
            )

        # Check for room object interactions (e.g. *checks clock*)
        full_text = self._handle_room_objects(
            full_text,
            on_text=on_text,
            on_tool=on_tool,
            on_tool_result=on_tool_result,
            on_usage=on_usage,
        )

        # Signal LED daemon: inference complete
        set_claude_state("present")

        # Auto-save after each exchange
        self.save_session()

        # Trim after save too — keeps context gradual rather than cliff-edge.
        # Without this, autonomous wakes accumulate unchecked between visits.
        self.trim_context()

        return full_text

    # --- Convenience properties ---

    def message_count(self) -> int:
        return len(self.messages)

    def token_count(self) -> Optional[int]:
        if self.backend == "ccode":
            return self._estimate_tokens()
        try:
            count = self.client.messages.count_tokens(
                model=self.model, messages=self.messages,
                system=self.system, tools=self.tools,
            )
            return count.input_tokens
        except Exception:
            return self._estimate_tokens()
