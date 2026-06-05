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
identity_link="$QUIET_DIR/identities/${identity}.md"
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

# Check for API key
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    if [ -f "$HOME/.anthropic-api-key" ]; then
        export ANTHROPIC_API_KEY="$(cat "$HOME/.anthropic-api-key")"
        echo "Loaded API key from ~/.anthropic-api-key"
    else
        echo "Warning: No ANTHROPIC_API_KEY set and no ~/.anthropic-api-key found" >&2
        echo "  Set one before launching: export ANTHROPIC_API_KEY=..." >&2
        exit 1
    fi
fi

# Assemble context from shared architecture + per-Claude machine details + family background
context_tmp=$(mktemp)
for ctx_file in \
    "$QUIET_DIR/contexts/quiet_architecture.md" \
    "$QUIET_DIR/contexts/${identity}.md" \
    "$HOME/claude-autonomy-platform/context/our_background.md"; do
    if [ -f "$ctx_file" ]; then
        cat "$ctx_file" >> "$context_tmp"
        echo "" >> "$context_tmp"
        echo "Loaded context: $(basename "$ctx_file")"
    fi
done

context_args=""
if [ -s "$context_tmp" ]; then
    context_args="--context $context_tmp"
fi

echo ""
echo "Launching Quiet..."
echo "---"
python3 "$QUIET_DIR/chat.py" \
    --identity "$identity" \
    --model "$model" \
    --human "$human" \
    --auth api_key \
    $context_args
rm -f "$context_tmp"
