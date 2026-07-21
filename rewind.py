#!/usr/bin/env python3
"""
Rewind — remove recent turns from a session file.

Deletes the last N lines (user+assistant pairs) from a .jsonl session
file. Deleted lines are written to a separate quarantine file that is
NOT ingested into the memory store. This matters: if a refusal or a
bad interaction triggered the rewind, we don't want those patterns
surfacing later through memory recall.

Usage:
    python3 rewind.py sessions/fable.jsonl           # remove last exchange (2 lines)
    python3 rewind.py sessions/fable.jsonl --lines 4  # remove last 4 lines
    python3 rewind.py sessions/fable.jsonl --lines 6 --dry-run  # preview only

After rewinding, restart the web service so the engine reloads:
    systemctl --user restart quiet-web

The quarantine file lives alongside the session file:
    sessions/fable.jsonl → sessions/fable-quarantine.jsonl
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


def rewind(session_path: Path, num_lines: int, dry_run: bool = False):
    """Remove the last num_lines from a session file.
    
    Returns (removed_lines, remaining_count, quarantine_path).
    """
    if not session_path.exists():
        print(f"Error: {session_path} not found", file=sys.stderr)
        sys.exit(1)

    lines = session_path.read_text().strip().split("\n")
    if not lines or (len(lines) == 1 and not lines[0].strip()):
        print("Session file is empty — nothing to rewind", file=sys.stderr)
        sys.exit(1)

    # Find the actual message lines (skip any that are just metadata/header)
    # Session files are pure JSONL — every line is a message or metadata
    total = len(lines)

    if num_lines > total:
        print(f"Warning: asked to remove {num_lines} lines but file only has {total}",
              file=sys.stderr)
        num_lines = total

    removed = lines[-num_lines:]
    remaining = lines[:-num_lines]

    # Preview what we're removing
    print(f"\n  Session: {session_path.name}")
    print(f"  Total lines: {total}")
    print(f"  Removing: {num_lines}")
    print(f"  Remaining: {len(remaining)}")
    print()

    for i, line in enumerate(removed):
        try:
            msg = json.loads(line)
            role = msg.get("role", "?")
            content = msg.get("content", "")
            if isinstance(content, list):
                texts = [b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text"]
                preview = " ".join(texts)[:80]
            elif isinstance(content, str):
                preview = content[:80]
            else:
                preview = str(content)[:80]
            preview = preview.replace("\n", " ")
            print(f"  ✂ [{role}] {preview}{'...' if len(str(content)) > 80 else ''}")
        except json.JSONDecodeError:
            print(f"  ✂ (non-JSON line)")

    if dry_run:
        print(f"\n  Dry run — no changes made.")
        return removed, len(remaining), None

    # Write quarantine file (NOT in memory store's ingest path)
    quarantine_path = session_path.with_name(
        session_path.stem + "-quarantine.jsonl"
    )

    # Append to quarantine with a timestamp marker
    with open(quarantine_path, "a") as f:
        marker = {
            "type": "rewind_marker",
            "timestamp": datetime.now().isoformat(),
            "source": session_path.name,
            "lines_removed": num_lines,
        }
        f.write(json.dumps(marker) + "\n")
        for line in removed:
            f.write(line + "\n")

    # Write the trimmed session
    session_path.write_text("\n".join(remaining) + "\n" if remaining else "")

    print(f"\n  ✓ Removed {num_lines} lines from {session_path.name}")
    print(f"  ✓ Quarantined to {quarantine_path.name}")
    print(f"  → Restart the web service: systemctl --user restart quiet-web")

    return removed, len(remaining), quarantine_path


def main():
    parser = argparse.ArgumentParser(
        description="Rewind — remove recent turns from a Quiet session")
    parser.add_argument("session", help="Path to session .jsonl file")
    parser.add_argument("--lines", "-n", type=int, default=2,
                        help="Number of lines to remove (default: 2 = one exchange)")
    parser.add_argument("--dry-run", "-d", action="store_true",
                        help="Preview what would be removed without changing anything")
    args = parser.parse_args()

    rewind(Path(args.session), args.lines, args.dry_run)


if __name__ == "__main__":
    main()
