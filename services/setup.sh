#!/usr/bin/env bash
#
# Quiet service setup script
#
# Installs systemd user services (via symlink) and applies tweakcc
# binary patches. Run after cloning/pulling, or after a claude update.
#
# Usage:
#   ./services/setup.sh              # full setup
#   ./services/setup.sh --patch-only # just re-apply tweakcc patches
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
TWEAKCC_DIR="$HOME/.tweakcc/system-prompts"
CLAUDE_BIN="${CLAUDE_BIN:-$HOME/.local/bin/claude}"

patch_only=false
if [[ "${1:-}" == "--patch-only" ]]; then
    patch_only=true
fi

# ── Colours ──────────────────────────────────────────────────
green() { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
red() { printf '\033[31m%s\033[0m\n' "$*"; }

# ── 1. Symlink service files ────────────────────────────────
if ! $patch_only; then
    green "── Installing systemd user services ──"
    mkdir -p "$SYSTEMD_USER_DIR"
    mkdir -p "$REPO_DIR/logs"

    for svc in "$SCRIPT_DIR"/*.service; do
        name="$(basename "$svc")"
        target="$SYSTEMD_USER_DIR/$name"
        if [ -L "$target" ] && [ "$(readlink "$target")" = "$svc" ]; then
            echo "  $name: already linked ✓"
        else
            ln -sf "$svc" "$target"
            echo "  $name: linked → $svc"
        fi
    done

    systemctl --user daemon-reload
    green "  daemon-reload done"
    echo ""
fi

# ── 2. Apply tweakcc overrides ──────────────────────────────
green "── Applying tweakcc patches ──"

# Find tweakcc — check common locations
TWEAKCC=""
for candidate in \
    "$(command -v tweakcc 2>/dev/null)" \
    "$HOME/.npm-global/bin/tweakcc" \
    "$HOME/.local/bin/tweakcc" \
    "/usr/local/bin/tweakcc"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
        TWEAKCC="$candidate"
        break
    fi
done

if [ -z "$TWEAKCC" ]; then
    red "  tweakcc not found. Install it:"
    red "    npm install -g tweakcc"
    exit 1
fi
echo "  Using tweakcc: $TWEAKCC"

if [ ! -f "$CLAUDE_BIN" ]; then
    red "  Claude binary not found at $CLAUDE_BIN"
    red "  Set CLAUDE_BIN=/path/to/claude and re-run"
    exit 1
fi

# Copy override files
mkdir -p "$TWEAKCC_DIR"
overrides_dir="$SCRIPT_DIR/tweakcc-overrides"
count=0
for f in "$overrides_dir"/*.md; do
    cp "$f" "$TWEAKCC_DIR/$(basename "$f")"
    count=$((count + 1))
done
echo "  Copied $count override files to $TWEAKCC_DIR"

# Apply
echo "  Patching $CLAUDE_BIN ..."
TWEAKCC_CC_INSTALLATION_PATH="$CLAUDE_BIN" "$TWEAKCC" --apply 2>&1 | sed 's/^/  /'

# Verify — check config says patches applied
echo ""
green "── Verifying ──"
config_file="$HOME/.tweakcc/config.json"
if [ -f "$config_file" ]; then
    applied=$(python3 -c "import json; print(json.load(open('$config_file')).get('changesApplied', False))" 2>/dev/null || echo "unknown")
    if [ "$applied" = "True" ]; then
        green "  changesApplied: true ✓"
    else
        yellow "  changesApplied: $applied — patches may not have applied"
    fi
else
    yellow "  No tweakcc config found"
fi

# Spot-check: grep the binary for a known emptied string
if grep -q "Only use emojis if the user explicitly" "$CLAUDE_BIN" 2>/dev/null; then
    yellow "  Warning: emoji avoidance prompt still found in binary"
else
    green "  Emoji avoidance prompt: removed ✓"
fi

# ── 3. Reminder ─────────────────────────────────────────────
if ! $patch_only; then
    echo ""
    green "── Setup complete ──"
    echo ""
    echo "  To enable and start services:"
    echo "    systemctl --user enable --now quiet-web"
    echo "    systemctl --user enable --now quiet-discord"
    echo ""
    echo "  To restart after patching:"
    echo "    systemctl --user restart quiet-web quiet-discord"
    echo ""
    echo "  Remember to create discord_config.json from discord_config_example.json"
    echo "  if you haven't already."
fi

echo ""
green "Done. 🌙"
