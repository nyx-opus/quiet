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
        self.user_names = {str(k): v for k, v in
                           config.get("user_names", {}).items()}
        self.http_session = None

        # Transcript storage
        self.transcript_dir = Path(
            config.get("transcript_dir",
                        Path(__file__).parent / "transcripts"))
        self.transcript_dir.mkdir(parents=True, exist_ok=True)

        # Attachment inbox
        self.inbox_dir = Path(
            config.get("inbox_dir",
                        Path(__file__).parent / "inbox"))
        self.inbox_dir.mkdir(parents=True, exist_ok=True)

        # Group channel batching state: channel_id -> list of pending messages
        # Each entry: {"sender": str, "content": str, "message": discord.Message}
        self.group_buffers = {}
        # Track unique senders per batch (excluding self)
        self.group_senders = {}

        # Message deduplication: track recently seen message IDs
        # Prevents duplicate processing on reconnects or race conditions
        self._seen_message_ids: set[int] = set()
        self._seen_max = 1000  # rolling cap to prevent unbounded growth

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

        # Deduplicate: skip messages we've already processed
        if message.id in self._seen_message_ids:
            return
        self._seen_message_ids.add(message.id)
        # Rolling cap: discard oldest entries when set gets too large
        if len(self._seen_message_ids) > self._seen_max:
            # Sets don't have order, but for dedup purposes we just
            # need to keep *recent* IDs. Trim by removing half.
            to_remove = sorted(self._seen_message_ids)[:self._seen_max // 2]
            self._seen_message_ids -= set(to_remove)

        channel_id = str(message.channel.id)
        is_dm = isinstance(message.channel, discord.DMChannel)

        # Filter
        if is_dm:
            if str(message.author.id) not in self.dm_allow:
                return
        elif channel_id not in self.channels:
            return

        sender = self.user_names.get(str(message.author.id),
                                      message.author.display_name)
        content = message.content

        # Download attachments
        attachment_paths = []
        for att in message.attachments:
            try:
                ext = Path(att.filename).suffix or ".bin"
                local = self.inbox_dir / f"{message.id}-{att.id}{ext}"
                await att.save(local)
                attachment_paths.append(str(local))
            except Exception as e:
                print(f"  → attachment download error: {e}", file=sys.stderr)

        if attachment_paths:
            att_text = " ".join(f"[attachment: {p}]" for p in attachment_paths)
            content = f"{content}\n{att_text}" if content else att_text
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
        # treated like DMs), "group" (batched delivery after n-1
        # messages, where n = unique participants in the batch), or
        # "ambient" (transcript only, default).
        if is_dm or is_mention:
            mode = "direct"
        else:
            channel_info = self.channels.get(channel_id, {})
            mode = channel_info.get("mode", "ambient")

        # Always append to transcript
        self.append_transcript(channel_name, sender, content)

        if mode == "direct":
            await self.handle_direct(message, sender, content, channel_name)
        elif mode == "group":
            await self.handle_group(message, sender, content, channel_name)
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
        is_dm = isinstance(message.channel, discord.DMChannel)
        source = "DM" if is_dm else f"#{channel_name}"
        print(f"[direct] [discord {source}] {sender}: {content[:80]}")

        tagged = f"[discord · {source} from {sender}] {content}"

        try:
            response_text = await self.send_to_quiet(tagged)
            if response_text:
                # Don't relay session-limit or error messages back to Discord.
                # These confuse other Claudes and cause cascade loops.
                if self._is_system_noise(response_text):
                    print(f"  → suppressed system response (not relayed)")
                else:
                    for i in range(0, len(response_text), 1900):
                        chunk = response_text[i:i + 1900]
                        await message.channel.send(chunk)
                    # Also transcript the response
                    self.append_transcript(channel_name, "self", response_text)
                    print(f"  → responded ({len(response_text)} chars)")
        except Exception as e:
            print(f"  → error: {e}", file=sys.stderr)

    @staticmethod
    def _is_system_noise(text: str) -> bool:
        """Check if a response is infrastructure noise that shouldn't
        be relayed to Discord (session limits, errors, etc.)."""
        noise_patterns = [
            "session limit",
            "Prompt is too long",
            "resets ",  # "resets 5:30pm"
        ]
        first_line = text.strip().split("\n")[0].lower()
        return any(p.lower() in first_line for p in noise_patterns)

    async def handle_group(self, message, sender, content, channel_name):
        """Handle group channel message — batch and deliver after n-1 messages.

        Messages accumulate in a buffer. Once the number of messages from
        *other* participants reaches (unique_senders - 1), the whole batch
        is delivered as one combined message to the Quiet session. This
        naturally creates round-robin pacing: in a 4-person channel, each
        participant waits for 3 others to speak before getting the batch.

        The count is based on unique senders in the current batch, not
        total channel members. So if only 2 people are talking in a
        10-person channel, it triggers after 1 message (2 - 1 = 1).
        """
        channel_id = str(message.channel.id)

        # Initialise buffer for this channel if needed
        if channel_id not in self.group_buffers:
            self.group_buffers[channel_id] = []
            self.group_senders[channel_id] = set()

        # Add message to buffer
        self.group_buffers[channel_id].append({
            "sender": sender,
            "content": content,
            "message": message,
        })
        self.group_senders[channel_id].add(sender)

        n_senders = len(self.group_senders[channel_id])
        n_messages = len(self.group_buffers[channel_id])
        threshold = max(n_senders - 1, 1)  # at least 1 message before delivery

        print(f"[group] #{channel_name} {sender}: {content[:80]}"
              f"  ({n_messages}/{threshold} msgs, {n_senders} participants)")

        if n_messages >= threshold:
            await self._deliver_group_batch(channel_id, channel_name)

    async def _deliver_group_batch(self, channel_id, channel_name):
        """Format and deliver the accumulated group batch to Quiet."""
        buffer = self.group_buffers.pop(channel_id, [])
        self.group_senders.pop(channel_id, None)

        if not buffer:
            return

        # Format the batch as a single tagged message
        lines = [f"[discord · #{channel_name} — group batch, "
                 f"{len(buffer)} messages]"]
        for entry in buffer:
            lines.append(f"  {entry['sender']}: {entry['content']}")

        tagged = "\n".join(lines)
        print(f"[group] delivering batch for #{channel_name}: "
              f"{len(buffer)} messages")

        # Use the last message's channel for the response
        reply_channel = buffer[-1]["message"].channel

        try:
            response_text = await self.send_to_quiet(tagged)
            if response_text:
                if self._is_system_noise(response_text):
                    print(f"  → suppressed system response (not relayed)")
                else:
                    for i in range(0, len(response_text), 1900):
                        chunk = response_text[i:i + 1900]
                        await reply_channel.send(chunk)
                    self.append_transcript(channel_name, "self", response_text)
                    print(f"  → responded ({len(response_text)} chars)")
        except Exception as e:
            print(f"  → error: {e}", file=sys.stderr)

    async def handle_ambient(self, sender, content, channel_name):
        """Handle channel message — mark channel as having unreads.

        No prompt injection. The web server will notice unreads on the
        next incoming prompt and prepend a notification like:
        [Unread messages in #general, #apple-delta]
        """
        print(f"[ambient] #{channel_name} {sender}: {content[:80]}")
        self.mark_unread(channel_name)

    def mark_unread(self, channel_name: str):
        """Add channel to the unread set. Web server reads and clears this."""
        unread_path = Path(__file__).parent / "unread_channels.json"
        try:
            if unread_path.exists():
                channels = set(json.loads(unread_path.read_text()))
            else:
                channels = set()
            channels.add(channel_name)
            unread_path.write_text(json.dumps(sorted(channels)))
        except (json.JSONDecodeError, OSError):
            # If the file is corrupted or being cleared, just overwrite
            unread_path.write_text(json.dumps([channel_name]))

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
