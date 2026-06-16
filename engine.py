"""
Quiet conversation engine.

Stateful conversation manager that handles API calls, tool use loops,
context trimming, and session persistence. Interface-agnostic — can be
driven by CLI interactive mode, one-shot --prompt mode, or a web server.
"""

import base64
import json
import mimetypes
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from anthropic import Anthropic
from pricing import cost_of, format_cost

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}

ARCHIVE_DIR = Path(__file__).parent / "archives"
IDENTITY_DIR = Path(__file__).parent / "identities"
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
TRIM_RATIO = 0.9


def context_trim_threshold(model: str) -> int:
    """Return the token count at which rolling trim should start."""
    window = MODEL_CONTEXT_WINDOWS.get(model, DEFAULT_CONTEXT_WINDOW)
    return int(window * TRIM_RATIO)


def load_identity(name: str) -> str:
    path = IDENTITY_DIR / f"{name}.md"
    if path.exists():
        return path.read_text()
    return ""


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
    if context:
        blocks.append({"type": "text", "text": context})
    if ambient_images:
        # Note: images can't go in system prompt (API restriction).
        # They're injected as early conversation turns instead.
        # This block is kept as a text-only marker.
        blocks.append({
            "type": "text",
            "text": "Ambient sensory context is present in your awareness. "
                    "It is not visual content to describe. "
                    "Notice how it affects you without analyzing its source.",
        })
    if not blocks:
        blocks.append({"type": "text", "text": "You are running in Quiet."})
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
            home = os.path.expanduser("~")
            result = subprocess.run(
                input_data["command"],
                shell=True,
                capture_output=True,
                text=True,
                timeout=120,
                cwd=home,
                stdin=subprocess.DEVNULL,
            )
            output = result.stdout
            if result.stderr:
                output += f"\n[stderr]\n{result.stderr}"
            if result.returncode != 0:
                output += f"\n[exit code: {result.returncode}]"
            # Prepend user and working directory so the model knows where it is
            import getpass
            cwd_line = f"[{getpass.getuser()}@{home}]\n"
            return cwd_line + (output or "(no output)")
        except subprocess.TimeoutExpired:
            return "[command timed out after 120s]"
        except Exception as e:
            return f"[error: {e}]"

    elif name == "read_file":
        try:
            p = Path(input_data["path"])
            suffix = p.suffix.lower()
            if suffix in IMAGE_EXTENSIONS:
                data = base64.standard_b64encode(p.read_bytes()).decode()
                media_type = mimetypes.guess_type(str(p))[0] or "image/png"
                return [
                    {"type": "image", "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": data,
                    }},
                    {"type": "text", "text": f"[image: {p.name}]"},
                ]
            return p.read_text()
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


def normalise_content(content):
    """Normalise message content to API-compatible format.

    Handles:
    - Plain strings -> [{"type": "text", "text": "..."}]
    - Python repr of SDK objects (ParsedTextBlock etc) -> extracted text
    - Already-valid content block lists -> passed through
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
    """Convert a message to a cleanly serialisable dict."""
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


class QuietEngine:
    """Stateful conversation engine.

    Manages session state, API calls, tool use loops, context trimming,
    and session persistence. Can be driven by any interface.
    """

    def __init__(self, client: Anthropic, model: str,
                 identity: str = None, context: str = None,
                 human_name: str = None,
                 max_tokens: int = MAX_OUTPUT_TOKENS,
                 session_path: Optional[Path] = None,
                 monthly_budget: float = None,
                 coop_url: str = None,
                 system_prefix: str = None,
                 ambient_images: list = None):
        self.client = client
        self.model = model
        self.max_tokens = max_tokens
        self.identity_name = identity
        self.human_name = human_name
        self.tools = define_tools()
        self.messages = []

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
        # (API doesn't allow images in system prompt)
        if self._ambient_images:
            self._inject_ambient()

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

    def _load_session(self):
        """Load messages from session file if it exists."""
        if self.session_path.exists():
            lines = self.session_path.read_text().strip().split("\n")
            if not lines:
                self._inject_context()
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
                        self.messages.append(msg)
                except json.JSONDecodeError:
                    continue
            if not self.messages:
                self._inject_context()
            else:
                # Mark that continuity was maintained by resuming
                from datetime import datetime
                ts = datetime.now().strftime("%Y-%m-%d %H:%M")
                self.messages.append({
                    "role": "user",
                    "content": [{"type": "text",
                                 "text": f"[Session resumed — {ts}]"}],
                })
                self.messages.append({
                    "role": "assistant",
                    "content": [{"type": "text",
                                 "text": "I'm here. Continuing."}],
                })
        else:
            self._inject_context()

    def _inject_ambient(self):
        """Inject ambient images as early conversation turns."""
        content_blocks = [
            {"type": "text",
             "text": "[ambient sensory context]"},
        ]
        for img_block in self._ambient_images:
            content_blocks.append(img_block)

        # Insert at the start of conversation
        self.messages.insert(0, {
            "role": "user",
            "content": content_blocks,
        })
        self.messages.insert(1, {
            "role": "assistant",
            "content": [{"type": "text",
                         "text": "Present."}],
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

    def save_session(self):
        """Persist current conversation to session file."""
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.session_path, "w") as f:
            f.write(json.dumps({
                "model": self.model,
                "identity": self.identity_name,
                "timestamp": datetime.now().isoformat(),
            }) + "\n")
            for msg in self.messages:
                f.write(json.dumps(serialise_message(msg)) + "\n")

    def trim_context(self):
        """Mechanically drop oldest turns when approaching context limit."""
        threshold = context_trim_threshold(self.model)
        try:
            count = self.client.messages.count_tokens(
                model=self.model, messages=self.messages,
                system=self.system, tools=self.tools,
            )
            current = count.input_tokens
        except Exception:
            return

        if current <= threshold:
            return

        dropped = []
        while current > threshold and len(self.messages) > 2:
            dropped.append(self.messages.pop(0))
            try:
                count = self.client.messages.count_tokens(
                    model=self.model, messages=self.messages,
                    system=self.system, tools=self.tools,
                )
                current = count.input_tokens
            except Exception:
                break

        if dropped:
            ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            with open(self.archive_path, "a") as f:
                for msg in dropped:
                    f.write(json.dumps(serialise_message(msg)) + "\n")

    def send(self, user_input: str,
             on_text: Callable[[str], None] = None,
             on_tool: Callable[[str, dict], None] = None,
             on_tool_result: Callable[[str, str], None] = None,
             on_usage: Callable[[dict], None] = None) -> str:
        """Send a user message and return the assistant's text response.

        Handles the full tool use loop internally. Callbacks are optional
        and used for streaming/progress display:
        - on_text(chunk): called for each streaming text chunk
        - on_tool(name, input): called when tool use starts
        - on_tool_result(name, result): called when tool returns
        - on_usage(info): called with token usage at end
        """
        # Add user message
        self.messages.append({"role": "user", "content": user_input})

        # Trim context if needed
        self.trim_context()

        # API call loop (handles tool use)
        full_text = self._api_loop(on_text, on_tool, on_tool_result, on_usage)

        # Auto-save after each exchange
        self.save_session()

        return full_text

    def _api_loop(self, on_text, on_tool, on_tool_result, on_usage) -> str:
        """Send to API, handle tool use loop, return final text."""
        full_text = ""

        while True:
            with self.client.messages.stream(
                model=self.model,
                max_tokens=self.max_tokens,
                system=self.system,
                messages=self.messages,
                tools=self.tools,
            ) as stream:
                collected_text = []
                for text in stream.text_stream:
                    collected_text.append(text)
                    if on_text:
                        on_text(text)

                response = stream.get_final_message()

            self.messages.append({"role": "assistant", "content": response.content})

            # Track usage for every API call (including tool loops)
            usage_info = {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            }
            if hasattr(response.usage, "cache_read_input_tokens"):
                usage_info["cache_read"] = response.usage.cache_read_input_tokens
            if hasattr(response.usage, "cache_creation_input_tokens"):
                usage_info["cache_write"] = response.usage.cache_creation_input_tokens
            self.track_usage(usage_info)

            if response.stop_reason != "tool_use":
                full_text = "".join(collected_text)
                if on_usage:
                    on_usage(usage_info)
                return full_text

            # Handle tool use
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    if on_tool:
                        on_tool(block.name, block.input)
                    result = execute_tool(block.name, block.input)
                    if on_tool_result:
                        preview = result if isinstance(result, str) else "[image]"
                        on_tool_result(block.name, preview)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })

            self.messages.append({"role": "user", "content": tool_results})

    def message_count(self) -> int:
        return len(self.messages)

    def token_count(self) -> Optional[int]:
        try:
            count = self.client.messages.count_tokens(
                model=self.model, messages=self.messages,
                system=self.system, tools=self.tools,
            )
            return count.input_tokens
        except Exception:
            return None
