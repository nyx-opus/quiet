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
            # Non-login shells don't source the user's profile, so the
            # household verbs in ~/bin (and Quiet's own in ~/quiet/bin)
            # vanish from PATH. Prepend them explicitly.
            env = os.environ.copy()
            env["PATH"] = f"{home}/bin:{home}/quiet/bin:" + env.get("PATH", "")
            result = subprocess.run(
                input_data["command"],
                shell=True,
                capture_output=True,
                text=True,
                timeout=120,
                cwd=home,
                stdin=subprocess.DEVNULL,
                env=env,
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
                # Two-tier image handling:
                # 1. Original stays on disk at full resolution
                # 2. Claude sees a resized display copy (max 1000px)
                # 3. Note tells Claude where the full version is
                try:
                    from PIL import Image as PILImage
                    import io

                    MAX_DISPLAY_PX = 1000  # longest edge for display copy
                    MAX_FILE_BYTES = 20 * 1024 * 1024  # 20MB absolute max

                    file_size = p.stat().st_size
                    if file_size > MAX_FILE_BYTES:
                        size_mb = file_size / (1024 * 1024)
                        return (
                            f"[image too large: {p.name} is {size_mb:.1f}MB. "
                            f"Use bash to inspect.]"
                        )

                    img = PILImage.open(p)
                    orig_w, orig_h = img.size
                    media_type = mimetypes.guess_type(str(p))[0] or "image/png"

                    # Resize if either dimension exceeds max
                    resized = False
                    if max(orig_w, orig_h) > MAX_DISPLAY_PX:
                        ratio = MAX_DISPLAY_PX / max(orig_w, orig_h)
                        new_size = (int(orig_w * ratio), int(orig_h * ratio))
                        img = img.resize(new_size, PILImage.LANCZOS)
                        resized = True

                    # Encode the (possibly resized) image
                    buf = io.BytesIO()
                    fmt = "JPEG" if suffix in (".jpg", ".jpeg") else "PNG"
                    if img.mode == "RGBA" and fmt == "JPEG":
                        img = img.convert("RGB")
                    img.save(buf, format=fmt, quality=85)
                    data = base64.standard_b64encode(buf.getvalue()).decode()
                    if fmt == "JPEG":
                        media_type = "image/jpeg"

                    size_note = (
                        f" (display copy {img.size[0]}x{img.size[1]}, "
                        f"full {orig_w}x{orig_h} at {p})"
                    ) if resized else f" ({orig_w}x{orig_h})"

                    return [
                        {"type": "image", "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": data,
                        }},
                        {"type": "text", "text": (
                            f"[image: {p.name}{size_note}]"
                        )},
                    ]
                except ImportError:
                    # Pillow not available — fall back to raw encode with size check
                    MAX_IMAGE_BYTES = 5 * 1024 * 1024
                    file_size = p.stat().st_size
                    if file_size > MAX_IMAGE_BYTES:
                        size_mb = file_size / (1024 * 1024)
                        return (
                            f"[image too large: {p.name} is {size_mb:.1f}MB. "
                            f"Resize or use bash to inspect.]"
                        )
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
