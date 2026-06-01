#!/usr/bin/env python3
"""
Discord listener for Quiet.

Connects to Discord via a bot token, listens for messages in configured
channels, and handles them differently based on type:

- DMs and mentions: injected directly into the Quiet session (immediate
  awareness, model responds, response sent back to Discord)
- Channel messages: appended to per-channel transcript files and a
  notification sent to the session ("new message in #general from Delta").
  Model can read transcripts via bash when they choose to.

This preserves model agency — direct messages deserve attention,
channel chatter is ambient awareness the model opts into.

Usage:
    python3 discord_listener.py --config discord_config.json

Config format (discord_config.json):
{
    "token": "BOT_TOKEN",
    "channels": {
        "CHANNEL_ID": {"name": "general"},
        "CHANNEL_ID": {"name": "apple-delta"}
    },
    "dm_allow": ["USER_ID_1", "USER_ID_2"],
    "quiet_url": "http://localhost:8090",
    "transcript_dir": "transcripts"
}
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

try:
    import discord
except ImportError:
    print("discord.py required: pip install discord.py", file=sys.stderr)
    sys.exit(1)


class QuietDiscordBot(discord.Client):
    """Discord bot that bridges Discord and a Quiet session."""

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

        # Transcript storage
        self.transcript_dir = Path(
            config.get("transcript_dir",
                        Path(__file__).parent / "transcripts"))
        self.transcript_dir.mkdir(parents=True, exist_ok=True)

    async def on_ready(self):
        # Resolve channel names from Discord
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
        print(f"  Transcripts: {self.transcript_dir}")

    async def on_message(self, message: discord.Message):
        # Never respond to self
        if message.author.id == self.user.id:
            return

        channel_id = str(message.channel.id)
        is_dm = isinstance(message.channel, discord.DMChannel)

        # Filter
        if is_dm:
            if str(message.author.id) not in self.dm_allow:
                return
        elif channel_id not in self.channels:
            return

        sender = message.author.display_name
        content = message.content
        if not content and message.attachments:
            content = f"[sent {len(message.attachments)} attachment(s)]"
        if not content:
            return

        # Determine channel name
        if is_dm:
            channel_name = f"dm-{sender.lower()}"
        else:
            channel_info = self.channels.get(channel_id, {})
            channel_name = channel_info.get("name", message.channel.name)

        # Is this a mention of our bot?
        is_mention = self.user in message.mentions

        # Determine mode: DMs and mentions are always direct.
        # Channels can be configured as "direct" (sibling channels,
        # treated like DMs) or "ambient" (notify only, default).
        if is_dm or is_mention:
            mode = "direct"
        else:
            channel_info = self.channels.get(channel_id, {})
            mode = channel_info.get("mode", "ambient")

        # Always append to transcript
        self.append_transcript(channel_name, sender, content)

        if mode == "direct":
            await self.handle_direct(message, sender, content, channel_name)
        else:
            await self.handle_ambient(sender, content, channel_name)

    def append_transcript(self, channel_name: str, sender: str, content: str):
        """Append message to per-channel transcript file."""
        path = self.transcript_dir / f"{channel_name}.jsonl"
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "sender": sender,
            "content": content,
        }
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    async def handle_direct(self, message, sender, content, channel_name):
        """Handle DM or mention — inject into session and respond."""
        source = f"discord #{channel_name}" if not isinstance(
            message.channel, discord.DMChannel) else "discord DM"
        print(f"[direct] [{source}] {sender}: {content[:80]}")

        try:
            response_text = await self.send_to_quiet(content)
            if response_text:
                for i in range(0, len(response_text), 1900):
                    chunk = response_text[i:i + 1900]
                    await message.channel.send(chunk)
                # Also transcript the response
                self.append_transcript(channel_name, "self", response_text)
                print(f"  → responded ({len(response_text)} chars)")
        except Exception as e:
            print(f"  → error: {e}", file=sys.stderr)

    async def handle_ambient(self, sender, content, channel_name):
        """Handle channel message — notify session, don't inject full text."""
        print(f"[ambient] #{channel_name} {sender}: {content[:80]}")

        # Send a brief notification to the session
        preview = content[:60] + "..." if len(content) > 60 else content
        notification = (f"*ding* New message in #{channel_name} from "
                        f"{sender}: \"{preview}\"")

        try:
            await self.send_to_quiet(notification)
        except Exception as e:
            print(f"  → notify error: {e}", file=sys.stderr)

    async def send_to_quiet(self, content: str) -> str:
        """POST message to Quiet web server and return response text."""
        if self.http_session is None:
            self.http_session = aiohttp.ClientSession()

        url = f"{self.quiet_url}/api/message"
        payload = {"message": content}

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
