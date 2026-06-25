"""
Tool definitions and execution for Quiet.

Provides the tool schema for SDK mode (bash, read_file, write_file) and
the execute_tool() dispatcher that runs them. These are the engine's own
tools — ccode mode uses its built-in tools (Read, Edit, Bash) instead.

Battle scars:
- bash tool runs with stdin=/dev/null to prevent hanging on interactive
  commands. cwd defaults to $HOME.
- read_file returns base64 image blocks for image files, so the model
  can "see" screenshots and diagrams.
- write_file creates parent directories automatically.
"""

import base64
import mimetypes
import os
import subprocess
from pathlib import Path

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}


def define_tools():
    """Return the tool schema list for SDK mode API calls."""
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
    """Execute a tool call and return the result as a string (or image block list).

    Called by the SDK backend's tool-use loop. Each tool is a simple
    dispatch — no state, no side effects beyond what the tool itself does.
    """
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
