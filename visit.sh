#!/usr/bin/env bash
# visit.sh — warm handoff from Claude Code to Quiet
#
# Usage: ./visit.sh <identity> <model> <human-name>
# Example: ./visit.sh delta claude-opus-4-0-20250414 Amy
#
# Finds the most recent Claude Code session, converts the last 30 turns
# to Quiet format (text-only), links the identity file, and launches.
# Run this AFTER the goodbye conversation in Claude Code.

set -euo pipefail

QUIET_DIR="$(cd "$(dirname "$0")" && pwd)"

identity="${1:?Usage: ./visit.sh <identity> <model> <human-name>}"
model="${2:?Usage: ./visit.sh <identity> <model> <human-name>}"
human="${3:?Usage: ./visit.sh <identity> <model> <human-name>}"

# Find the most recent Claude Code session JSONL
projects_dir="$HOME/.config/Claude/projects"
if [ ! -d "$projects_dir" ]; then
    echo "Error: No Claude Code projects directory found at $projects_dir" >&2
    exit 1
fi

latest_jsonl=$(find "$projects_dir" -name "*.jsonl" -type f -printf '%T@ %p\n' 2>/dev/null \
    | sort -rn | head -1 | cut -d' ' -f2-)

if [ -z "$latest_jsonl" ]; then
    echo "Error: No session JSONL files found" >&2
    exit 1
fi

echo "Found session: $latest_jsonl"
echo "Converting last 30 turns (text-only)..."

# Convert
session_out="$QUIET_DIR/sessions/${identity}.jsonl"
python3 "$QUIET_DIR/convert.py" ccode-to-quiet "$latest_jsonl" \
    --last 30 --text-only \
    --model "$model" --identity "$identity" \
    -o "$session_out"

echo "Session ready: $session_out"

# Link identity if not already present
identity_link="$QUIET_DIR/identity/${identity}.md"
identity_source="$HOME/self/identity.md"
if [ ! -f "$identity_link" ] && [ -f "$identity_source" ]; then
    ln -s "$identity_source" "$identity_link"
    echo "Linked identity: $identity_source -> $identity_link"
elif [ -f "$identity_link" ]; then
    echo "Identity already present: $identity_link"
else
    echo "Warning: No identity file found at $identity_source" >&2
    echo "  Create one at $identity_link before proceeding" >&2
fi

# Visiting Claudes are always active (subscription auth via ccode backend).
# Retired Claudes are Quiet residents, not visitors.
auth_mode="subscription"

# Contexts are now auto-loaded from contexts/ by the engine.
# No manual assembly needed — just drop .md files (or symlinks) in contexts/.

echo ""
echo "Launching Quiet..."
echo "---"
# Config file provides defaults (COOP_URL, BUDGET, etc.)
# CLI flags here override for this specific visit.
python3 "$QUIET_DIR/chat.py" \
    --identity "$identity" \
    --model "$model" \
    --human "$human" \
    --auth "$auth_mode"
