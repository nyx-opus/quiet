#!/usr/bin/env python3
"""
Generate a Quiet discord_config.json from an existing ClAP installation.

Reads the ClAP discord routing config and bot token to produce a config
file for the Quiet Discord listener.

Usage:
    python3 generate_discord_config.py --clap-dir ~/claude-autonomy-platform \
        --port 8090 -o discord_config.json
"""

import argparse
import json
import os
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(
        description="Generate Quiet Discord config from ClAP")
    parser.add_argument("--clap-dir", required=True,
                        help="Path to claude-autonomy-platform")
    parser.add_argument("--port", type=int, default=8090,
                        help="Quiet web server port")
    parser.add_argument("-o", "--output", default="discord_config.json",
                        help="Output path")
    args = parser.parse_args()

    clap = Path(args.clap_dir)

    # Get bot token from infrastructure config
    infra_config = clap / "config" / "claude_infrastructure_config.txt"
    token = None
    if infra_config.exists():
        for line in infra_config.read_text().split("\n"):
            if line.startswith("DISCORD_BOT_TOKEN="):
                token = line.split("=", 1)[1].strip().strip('"').strip("'")
                break

    if not token:
        # Try environment
        token = os.environ.get("DISCORD_BOT_TOKEN")

    if not token:
        print("Could not find DISCORD_BOT_TOKEN in ClAP config or env",
              file=sys.stderr)
        print("Add it manually to the output file", file=sys.stderr)
        token = "YOUR_BOT_TOKEN_HERE"

    # Get channel IDs from plugin access.json (has actual IDs)
    # and names from routing config (has human-readable names)
    access_file = Path.home() / ".claude" / "channels" / "discord" / "access.json"
    routing_file = clap / "config" / "discord_routing.json"

    # Build name lookup from routing: chat_id → name, channel_name → name
    name_lookup = {}
    dm_chat_ids = {}
    if routing_file.exists():
        routing = json.loads(routing_file.read_text())
        for route_name, info in routing.get("routes", {}).items():
            if isinstance(info, dict):
                if info.get("chat_id"):
                    name_lookup[str(info["chat_id"])] = route_name
                    if info.get("type") == "dm":
                        dm_chat_ids[route_name] = str(info["chat_id"])
                if info.get("name"):
                    name_lookup[info["name"]] = route_name

    # Get channel IDs and DM allowlist from access.json
    channels = {}
    dm_allow = []
    if access_file.exists():
        access = json.loads(access_file.read_text())
        dm_allow = access.get("allowFrom", [])

        for group_id in access.get("groups", {}):
            name = name_lookup.get(group_id, f"channel-{group_id}")
            channels[group_id] = {"name": name}

    config = {
        "token": token,
        "quiet_url": f"http://localhost:{args.port}",
        "channels": channels,
        "dm_allow": dm_allow,
    }

    out_path = Path(args.output)
    out_path.write_text(json.dumps(config, indent=2) + "\n")
    print(f"Wrote {out_path}", file=sys.stderr)
    print(f"  {len(channels)} channels, {len(dm_allow)} DM users",
          file=sys.stderr)
    if token == "YOUR_BOT_TOKEN_HERE":
        print("  ⚠ Token not found — edit the file to add it", file=sys.stderr)


if __name__ == "__main__":
    main()
