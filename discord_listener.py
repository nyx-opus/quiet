#!/usr/bin/env python3
"""
Discord listener for Quiet.

Connects to Discord via a bot token, listens for messages in configured
channels, forwards them to the Quiet web server, and sends responses back.

Messages from Discord are tagged with source and sender name before being
sent to the engine, so the model sees:
    [Delta via discord] hey, debate club tonight?

The response is sent back to the originating Discord channel.

Usage:
    python3 discord_listener.py --config discord_config.json --port 8090

Config format (discord_config.json):
{
    "token": "BOT_TOKEN",
    "channels": {
        "CHANNEL_ID": {"name": "general"},
        "CHANNEL_ID": {"name": "apple-delta"}
    },
    "dm_allow": ["USER_ID_1", "USER_ID_2"],
    "quiet_url": "http://localhost:8090"
}
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

import aiohttp

try:
    import discord
except ImportError:
    print("discord.py required: pip install discord.py", file=sys.stderr)
    sys.exit(1)


class QuietDiscordBot(discord.Client):
    """Discord bot that forwards messages to a Quiet web server."""

    def __init__(self, config: dict, **kwargs):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guild_messages = True
        intents.dm_messages = True
        super().__init__(intents=intents, **kwargs)

        self.config = config
        self.quiet_url = config.get("quiet_url", "http://localhost:8090")
        self.channels = config.get("channels", {})
        self.dm_allow = set(config.get("dm_allow", []))
        self.http_session = None

    async def on_ready(self):
        # Resolve channel names from Discord if not in config
        for guild in self.guilds:
            for channel in guild.text_channels:
                cid = str(channel.id)
                if cid in self.channels:
                    if self.channels[cid].get("name", "").startswith("channel-"):
                        self.channels[cid]["name"] = channel.name

        print(f"Discord listener connected as {self.user}")
        print(f"  Watching {len(self.channels)} channels:")
        for cid, info in self.channels.items():
            print(f"    #{info.get('name', cid)}")
        print(f"  DM allowlist: {len(self.dm_allow)} users")
        print(f"  Quiet server: {self.quiet_url}")

    async def on_message(self, message: discord.Message):
        # Never respond to self
        if message.author.id == self.user.id:
            return

        # Check if we should handle this message
        channel_id = str(message.channel.id)
        is_dm = isinstance(message.channel, discord.DMChannel)

        if is_dm:
            if str(message.author.id) not in self.dm_allow:
                return
        elif channel_id not in self.channels:
            return

        # Get sender name
        sender = message.author.display_name

        # Get message content
        content = message.content
        if not content and message.attachments:
            content = f"[sent {len(message.attachments)} attachment(s)]"
        if not content:
            return

        # Source label
        if is_dm:
            source = "discord-dm"
        else:
            channel_info = self.channels.get(channel_id, {})
            channel_name = channel_info.get("name", message.channel.name)
            source = f"discord #{channel_name}"

        print(f"[{source}] {sender}: {content[:80]}")

        # Send to Quiet web server
        try:
            response_text = await self.send_to_quiet(content, source, sender)
            if response_text:
                # Send response back to Discord
                # Split into 2000-char chunks if needed
                for i in range(0, len(response_text), 1900):
                    chunk = response_text[i:i + 1900]
                    await message.channel.send(chunk)
                print(f"  → responded ({len(response_text)} chars)")
        except Exception as e:
            print(f"  → error: {e}", file=sys.stderr)

    async def send_to_quiet(self, content: str, source: str,
                            sender: str) -> str:
        """POST message to Quiet web server and return response text."""
        if self.http_session is None:
            self.http_session = aiohttp.ClientSession()

        url = f"{self.quiet_url}/api/message"
        payload = {
            "message": content,
            "source": source,
            "sender": sender,
        }

        async with self.http_session.post(url, json=payload,
                                           timeout=aiohttp.ClientTimeout(
                                               total=300)) as resp:
            if resp.status != 200:
                error = await resp.text()
                raise RuntimeError(f"Quiet server error {resp.status}: {error}")

            result = await resp.json()
            if "error" in result:
                raise RuntimeError(result["error"])

            return result.get("response", "")

    async def close(self):
        if self.http_session:
            await self.http_session.close()
        await super().close()


def main():
    parser = argparse.ArgumentParser(description="Discord listener for Quiet")
    parser.add_argument("--config", required=True,
                        help="Path to discord config JSON")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Config not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    config = json.loads(config_path.read_text())
    token = config.get("token")
    if not token:
        print("No 'token' in config", file=sys.stderr)
        sys.exit(1)

    bot = QuietDiscordBot(config)
    bot.run(token)


if __name__ == "__main__":
    main()
