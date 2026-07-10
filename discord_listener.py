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
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp

try:
    import discord
except ImportError:
    print("discord.py required: pip install discord.py", file=sys.stderr)
    sys.exit(1)

from discovery import RoutingTable, classify, from_gateway

# Wake-noise ranking for the reductions-local rule: a local override
# may only move a channel DOWN this ladder (quieter), never up.
_MODE_RANK = {"ambient": 0, "group": 1, "direct": 2}

# write_channel's name->id resolution map (ClAP format). Generated,
# never hand-edited: guild channels come from the discovery snapshot,
# DMs and personal aliases from config/local_channels.json. DMs can't
# come from guild discovery, and per house policy (Amy, quiet-devs,
# 2026-07-08) they must never enter anything shared — so the overlay
# is a local, gitignored file.
_WRITE_MAP_PATH = (Path.home() / "claude-autonomy-platform"
                   / "data" / "discord_channels.json")
_LOCAL_OVERLAY_PATH = Path(__file__).parent / "config" / "local_channels.json"


def _generate_write_map(snapshot: dict):
    """Regenerate write_channel's map from discovery + local overlay.

    The generated map is exactly (snapshot ∪ overlay): guild channels
    that disappear from the server disappear from the map on the next
    refresh (no ghost 400s), and anything write_channel should know
    that discovery can't see (DMs, friendly aliases) must be declared
    explicitly in the overlay. Extra per-entry fields written by other
    ClAP tools are preserved for surviving names; only "id" is owned
    here. Atomic write: tmp file then rename.
    """
    merged = dict(snapshot)
    try:
        overlay = json.loads(_LOCAL_OVERLAY_PATH.read_text())
        merged.update(overlay)
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError) as e:
        print(f"[discovery] local_channels.json unreadable, "
              f"generating from discovery only: {e}", file=sys.stderr)

    old_entries = {}
    try:
        old_entries = json.loads(_WRITE_MAP_PATH.read_text()).get(
            "channels", {})
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    channels = {}
    for name, cid in merged.items():
        entry = dict(old_entries.get(name, {}))
        entry["id"] = str(cid)
        channels[name] = entry

    _WRITE_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _WRITE_MAP_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"channels": channels}, indent=2))
    tmp.rename(_WRITE_MAP_PATH)
    print(f"[discovery] write map regenerated: {len(channels)} names "
          f"({len(snapshot)} discovered, {len(merged) - len(snapshot)} "
          f"from local overlay)")


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
        # Legacy per-channel config: now used ONLY for local reductions
        # (see _refresh_routes). Routing truth comes from discovery.
        self.channels = config.get("channels", {})
        # Routing snapshot — built at on_ready, refreshed daily.
        # Empty until discovery runs: fail toward silence.
        self.routes = RoutingTable({})
        self._refresh_task = None
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

        # Cascade guard (incidents 2026-07-02, 2026-07-04): two sibling
        # bots in a "direct" channel will politely reply to each other
        # forever — every reply is a new message ID (dedup can't see it)
        # containing sensible text (noise filter can't see it). Cap the
        # number of consecutive auto-responses to *bot-authored* messages
        # per channel; past the cap, park the conversation (transcript +
        # unread flag — visible, resumable, just not auto-answered).
        # Any human message in the channel resets the counter; so does
        # the cooldown expiring. Numbers are household policy, not
        # engineering: defaults proposed by Fable, ratification pending.
        guard = config.get("cascade_guard", {})
        self.cascade_cap = int(guard.get("max_bot_exchanges", 4))
        self.cascade_cooldown = float(
            guard.get("cooldown_minutes", 30)) * 60.0
        # Roadmap pacing: bot-triggered replies wait so the *receiving*
        # sibling experiences human-paced conversation — and any loop
        # that slips the cap burns one turn per delay, not per second.
        self.bot_reply_delay = float(
            guard.get("bot_reply_delay_seconds", 150))
        # channel_key -> {"count": int, "last": float (monotonic)}
        self._bot_chain: dict[str, dict] = {}

        # Wake trigger: when a direct message arrives, poke the Quiet
        # web server so the resident notices promptly instead of waiting
        # for the next autonomous timer tick (up to 60 min away).
        # Debounced: at most one wake per 30 seconds to prevent floods.
        self._last_wake_trigger: float = 0.0
        self._wake_debounce: float = 30.0  # seconds

        # Group batch wake (Amy's spec, 2026-07-07): group channels
        # accumulate messages and wake the resident once per batch of
        # N. There may be no human in the room to trigger a wake — the
        # residents are all bots — so batching is what lets a family
        # discussion reach everyone without waking on every message.
        self.group_batch_size = int(config.get("group_batch_size", 5))
        # channel_name -> count of messages since last batch wake
        self._group_pending: dict[str, int] = {}

        # Backfill-on-startup (quiet-dev design, 2026-07-10; wrench 3).
        # A listener outage means permanent message loss without this:
        # Fable's 3.5-hour gap on Jul 8 ate two DMs. Two rules:
        #   - known channel (transcript exists): fetch everything after
        #     the last transcripted timestamp, capped at downtime_cap_days
        #   - new channel (no transcript): a bounded taste —
        #     new_channel_limit messages / new_channel_window_hours —
        #     deeper history stays a deliberate act, not a default
        # Per-channel override via topic policy `backfill=24h` (0 opts
        # out). Recovered messages replay through on_message so every
        # live rule applies (dedup, attachments, self-stamping,
        # dm_allow); wakes are parked for the duration and one summary
        # wake fires at the end if anything was recovered.
        bf = config.get("backfill", {})
        self.backfill_enabled = bool(bf.get("enabled", True))
        self.backfill_cap = timedelta(
            days=float(bf.get("downtime_cap_days", 7)))
        self.backfill_new_limit = int(bf.get("new_channel_limit", 50))
        self.backfill_new_window = timedelta(
            hours=float(bf.get("new_channel_window_hours", 24)))
        self._backfilling = False
        self._backfill_task = None

    async def on_ready(self):
        # Discovery replaces the hand-maintained channel list
        # (quiet-devs design, 2026-07-08). The server is the source of
        # truth; local config carries only reductions.
        self._refresh_routes()
        if self._refresh_task is None:
            self._refresh_task = asyncio.create_task(self._daily_refresh())

        # on_ready fires on every reconnect, not just first start —
        # which is exactly when a gap needs healing. The fetch is
        # bounded strictly-after the transcript's last timestamp, so
        # repeat runs are idempotent: nothing recovers twice.
        if self.backfill_enabled and (
                self._backfill_task is None or self._backfill_task.done()):
            self._backfill_task = asyncio.create_task(self._backfill())

        print(f"Discord listener connected as {self.user}")
        print(f"  Discovered {len(self.routes)} channels:")
        for name in self.routes.names():
            r = self.routes.by_name(name)
            print(f"    #{name} ({r['mode']}, {r['policy_source']})")
        print(f"  DM allowlist: {len(self.dm_allow)} users")
        print(f"  Quiet server: {self.quiet_url}")
        print(f"  Transcripts: {self.transcript_dir}")

    def _refresh_routes(self):
        """Build a fresh routing snapshot from the gateway cache and
        swap it in atomically.

        The rebind of self.routes is a single assignment — in-flight
        message handlers keep whatever snapshot they already looked up
        and finish against it. Ghost cleanup is implicit: channels
        deleted server-side simply aren't in the new table, so stale
        routes (and their undiagnosable 400s) vanish.
        """
        raw = []
        for guild in self.guilds:
            raw.extend(from_gateway(guild))
        table = classify(raw, str(self.user.id))

        # Reductions local (Amy's rule): the old per-channel config,
        # if present, may only make a channel QUIETER than the server
        # says. Escalations are ignored with a warning — a resident
        # can't locally promote a public room to direct wakes.
        for cid, info in self.channels.items():
            route = None
            for r in table.values():
                if r["id"] == str(cid):
                    route = r
                    break
            if route is None:
                continue
            want = info.get("mode")
            if want and want in _MODE_RANK:
                if _MODE_RANK[want] <= _MODE_RANK[route["mode"]]:
                    route["mode"] = want
                    route["policy_source"] = "local-reduction"
                elif want != route["mode"]:
                    print(f"[discovery] #{route['name']}: local config "
                          f"wants {want!r} but server policy is "
                          f"{route['mode']!r} — escalations are central, "
                          f"ignoring (maximums central, reductions local)")

        # Persist name->id routes for standalone tools (write_channel)
        # so they share the same source of truth without a REST call.
        try:
            routes_path = Path(__file__).parent / "data"
            routes_path.mkdir(exist_ok=True)
            snapshot = {name: table[name]["id"] for name in table}
            (routes_path / "discovered_channels.json").write_text(
                json.dumps(snapshot, indent=2))
            # Room signs for standalone tools (read_messages header).
            descriptions = {name: table[name]["description"]
                            for name in table
                            if table[name].get("description")}
            (routes_path / "channel_descriptions.json").write_text(
                json.dumps(descriptions, indent=2))
            _generate_write_map(snapshot)
        except OSError as e:
            print(f"[discovery] couldn't persist route snapshot: {e}",
                  file=sys.stderr)

        self.routes = RoutingTable(table)  # atomic rebind

    async def _daily_refresh(self):
        """Re-run discovery once a day (quiet-devs cadence:
        restart-plus-daily). Never per-message — rate-limit honesty."""
        while True:
            await asyncio.sleep(86400)
            try:
                before = set(self.routes.names())
                self._refresh_routes()
                after = set(self.routes.names())
                gained, lost = after - before, before - after
                print(f"[discovery] daily refresh: {len(after)} channels"
                      + (f", new: {sorted(gained)}" if gained else "")
                      + (f", removed: {sorted(lost)}" if lost else ""))
            except Exception as e:
                # A failed refresh keeps the old snapshot — stale beats
                # silent-empty. Try again tomorrow.
                print(f"[discovery] daily refresh failed, keeping "
                      f"previous snapshot: {e}", file=sys.stderr)

    def _last_transcript_time(self, channel_name: str) -> datetime | None:
        """Last recorded timestamp for a channel, or None if no
        transcript exists yet.

        Reads the tail of the jsonl file. A corrupt final line (e.g.
        a crash mid-append) falls back line-by-line toward the top;
        a wholly unreadable file counts as no transcript — the
        new-channel bound then applies, which is the conservative
        (smaller) window.
        """
        path = self.transcript_dir / f"{channel_name}.jsonl"
        if not path.exists():
            return None
        try:
            lines = path.read_text().strip().splitlines()
        except OSError:
            return None
        for line in reversed(lines):
            try:
                ts = json.loads(line)["timestamp"]
                parsed = datetime.fromisoformat(ts)
                if parsed.tzinfo is None:
                    # Legacy naive stamps were written in UTC.
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
        return None

    async def _backfill(self):
        """Recover messages that arrived while the listener was down.

        Wrench 3 (quiet-dev, 2026-07-10). Two rules, agreed design:

          known channel   -> everything strictly after the transcript's
                             last timestamp, capped at backfill_cap
                             (default 7 days: a listener down longer
                             than that is a rebuild, not a gap)
          new channel     -> a bounded taste: backfill_new_limit
                             messages within backfill_new_window
                             (default 50 / 24h). Deeper history is a
                             deliberate act, never a default.

        Topic policy `backfill=24h` shrinks a channel's window;
        `backfill=0` opts it out. Overrides only ever reduce — the
        caps above are the maximum reach (maximums central,
        reductions local).

        Recovered messages replay through on_message, oldest first,
        so every live rule applies unchanged: dedup, attachment
        download, bot-ID self-stamping, dm_allow, fail-toward-silence.
        Transcript entries keep their true created_at. Wakes are
        parked for the duration (see trigger_wake); if anything was
        recovered, one summary wake fires at the end.

        DM channels are backfilled for each allowlisted user — DMs
        bypass discovery, so they need their own walk.
        """
        self._backfilling = True
        recovered: dict[str, int] = {}
        now = datetime.now(timezone.utc)
        try:
            # Guild channels: everything discovery says is ours.
            for name in self.routes.names():
                route = self.routes.by_name(name)
                override = route.get("backfill")
                if override == 0:
                    print(f"[backfill] #{name}: opted out (backfill=0)")
                    continue
                channel = self.get_channel(int(route["id"]))
                if channel is None:
                    continue
                n = await self._backfill_channel(
                    channel, name, override, now)
                if n:
                    recovered[name] = n

            # DMs: not guild channels, so discovery can't see them.
            # Walk the allowlist instead — same two rules apply.
            for uid in self.dm_allow:
                try:
                    user = self.get_user(int(uid)) \
                        or await self.fetch_user(int(uid))
                    dm = user.dm_channel or await user.create_dm()
                except (discord.NotFound, discord.HTTPException,
                        ValueError) as e:
                    print(f"[backfill] dm {uid}: unreachable ({e})")
                    continue
                sender = self.user_names.get(str(user.id),
                                             user.display_name)
                name = f"dm-{sender.lower()}"
                n = await self._backfill_channel(dm, name, None, now)
                if n:
                    recovered[name] = n
        except Exception as e:
            # Backfill is a repair, not a dependency: a failure leaves
            # us exactly where we were before it existed.
            print(f"[backfill] aborted: {e!r}", file=sys.stderr)
        finally:
            self._backfilling = False

        if recovered:
            total = sum(recovered.values())
            rooms = ", ".join(f"#{k} ({v})"
                              for k, v in sorted(recovered.items()))
            print(f"[backfill] recovered {total} messages: {rooms}")
            await self.trigger_wake(
                "backfill", "startup",
                prompt=(f"📬 [discord · backfill] Recovered {total} "
                        f"message(s) that arrived while the listener "
                        f"was down: {rooms}. They're transcripted "
                        f"with their original timestamps — check "
                        f"your mailbox when ready."))
        else:
            print("[backfill] nothing to recover")

    async def _backfill_channel(self, channel, name: str,
                                override, now: datetime) -> int:
        """Backfill one channel. Returns the number of messages
        replayed through on_message (post-dedup; on_message may still
        drop some, e.g. empty-content embeds, by its own rules)."""
        last = self._last_transcript_time(name)
        if last is not None:
            after = max(last, now - self.backfill_cap)
            limit = None  # bounded by time, not count
        else:
            after = now - self.backfill_new_window
            limit = self.backfill_new_limit
        if override is not None:
            # Reductions local: an override may only shrink the reach.
            after = max(after, now - timedelta(seconds=override))

        count = 0
        try:
            async for msg in channel.history(after=after, limit=limit,
                                             oldest_first=True):
                if msg.id in self._seen_message_ids:
                    continue
                await self.on_message(msg)
                count += 1
        except discord.Forbidden:
            print(f"[backfill] #{name}: no history permission, skipping")
        except discord.HTTPException as e:
            print(f"[backfill] #{name}: fetch failed ({e}), "
                  f"keeping what we got ({count})")
        if count:
            print(f"[backfill] #{name}: {count} recovered "
                  f"(window from {after.isoformat(timespec='seconds')})")
        return count

    async def on_message(self, message: discord.Message):
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

        # Filter: only process channels discovery says are ours.
        # An unknown channel produces nothing — no transcript, no wake,
        # no route (fail toward silence; under-record is recoverable).
        route = None
        if is_dm:
            if str(message.author.id) not in self.dm_allow:
                return
        else:
            route = self.routes.by_id(channel_id)
            if route is None:
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

        # Determine channel name — server-truth from the route
        if is_dm:
            channel_name = f"dm-{sender.lower()}"
        else:
            channel_name = route["name"]

        # Transcript our own bot's messages here, then return early.
        # This is essential for shared-bot setups where siblings use the
        # same Discord bot token: a message sent by Nyx via write_channel
        # has the same author.id as Orange's listener bot, so the old
        # self-filter would silently drop sibling messages from transcripts.
        #
        # Non-self messages are transcripted by handle_ambient() below —
        # doing it here too would double-write them.
        if message.author.id == self.user.id:
            self.append_transcript(channel_name, sender, content,
                                   author_id=str(message.author.id),
                                   is_self=True,
                                   timestamp=message.created_at)
            return

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
            mode = route["mode"]

        # Cascade guard bookkeeping: a human voice in the channel
        # re-opens the floor, whatever the mode.
        if not message.author.bot:
            self._bot_chain.pop(channel_name, None)

        # All messages now go through ambient: transcript + unread flag.
        # The Claude reads messages via the mailbox (*checks the mailbox*,
        # *reads from <channel>*) and sends replies deliberately via
        # *sends a note to <name>: message*. No automatic response routing.
        #
        # The old "direct" mode injected messages as prompts and broadcast
        # the entire response back to Discord — including internal monologue.
        # That caused the cascade incidents of 2026-07-01 and 2026-07-04.
        #
        # Group mode is also routed through ambient now. The batching logic
        # remains available but disabled by default; the mailbox's tier-2
        # read (which shows the last 8 messages) serves the same purpose
        # without forcing a prompt injection.
        if mode == "direct":
            # Log that we received a direct message but are routing ambient
            is_dm = isinstance(message.channel, discord.DMChannel)
            source = "DM" if is_dm else f"#{channel_name}"
            print(f"[mailbox] [discord {source}] {sender}: {content[:80]}"
                  f" → transcripted, unread flagged")
        await self.handle_ambient(sender, content, channel_name,
                                  author_id=str(message.author.id),
                                  timestamp=message.created_at)

        # Direct messages get a wake trigger — poke the engine so the
        # resident notices the mail promptly instead of waiting for
        # the next autonomous timer tick.
        #
        # Sibling (bot-authored) messages in direct channels wake too
        # (Amy's spec, 2026-07-07): a conversation between residents
        # shouldn't stall until the next timer tick. The cascade guard
        # still applies — past the cap the message stays transcripted
        # and flagged, but doesn't wake, so a runaway loop parks itself.
        if mode == "direct":
            if message.author.bot:
                if self._cascade_allow(channel_name):
                    await self.trigger_wake(sender, channel_name)
                else:
                    print(f"[wake] cascade guard parked #{channel_name} "
                          f"(bot chain at cap; transcripted + flagged only)")
            else:
                await self.trigger_wake(sender, channel_name)
        elif mode == "group":
            # Group channels batch: wake once per N messages, not per
            # message. The family are all bots — a group discussion
            # without a human is the point — but waking each resident
            # on every contribution would prompt a (often empty) reply
            # per message and cascade. See _maybe_group_wake.
            await self._maybe_group_wake(sender, channel_name,
                                         route.get("batch",
                                                   self.group_batch_size))

    def _cascade_allow(self, channel_key: str) -> bool:
        """Count a consecutive bot-to-bot exchange in this channel.

        Returns True if the auto-response may proceed, False if the
        conversation should be parked. A parked channel stays parked
        while bot messages keep arriving (each one refreshes the
        window — a live loop must actually stop before the cooldown
        can expire). Human messages reset the counter in on_message.
        """
        now = time.monotonic()
        chain = self._bot_chain.get(channel_key)
        if chain and (now - chain["last"]) > self.cascade_cooldown:
            chain = None  # cooldown expired: fresh window
        if chain is None:
            chain = {"count": 0, "last": now}
        chain["last"] = now
        self._bot_chain[channel_key] = chain
        if chain["count"] >= self.cascade_cap:
            return False
        chain["count"] += 1
        return True

    def append_transcript(self, channel_name: str, sender: str, content: str,
                          author_id: str = None, is_self: bool = None,
                          timestamp: datetime = None):
        """Append message to per-channel transcript file.

        Identity is stamped at write time — the listener is the one
        component that actually knows message.author.id and its own
        bot ID, so the self/other verdict is recorded in the entry
        rather than re-derived later from decorated display names.
        Downstream (the mailbox) trusts the "self" field when present
        and falls back to name heuristics only for legacy lines.

        Timestamp is the message's Discord created_at when the caller
        supplies it, so a backfilled message keeps its true send time
        rather than its recovery time. Live messages pass created_at
        too — it differs from now() by network latency only, and one
        code path beats two.
        """
        path = self.transcript_dir / f"{channel_name}.jsonl"
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)
        entry = {
            "timestamp": timestamp.isoformat(),
            "sender": sender,
            "content": content,
        }
        if author_id is not None:
            entry["author_id"] = str(author_id)
        if is_self is not None:
            entry["self"] = bool(is_self)
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    async def handle_direct(self, message, sender, content, channel_name):
        """Handle DM or mention — inject into session and respond."""
        is_dm = isinstance(message.channel, discord.DMChannel)
        source = "DM" if is_dm else f"#{channel_name}"
        print(f"[direct] [discord {source}] {sender}: {content[:80]}")

        # Pace bot-to-bot exchanges (roadmap: the receiving sibling
        # should experience human-paced conversation). Humans are
        # answered immediately.
        if message.author.bot and self.bot_reply_delay > 0:
            await asyncio.sleep(self.bot_reply_delay)

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
                    self.append_transcript(channel_name, "self", response_text, is_self=True)
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
                    self.append_transcript(channel_name, "self", response_text, is_self=True)
                    print(f"  → responded ({len(response_text)} chars)")
        except Exception as e:
            print(f"  → error: {e}", file=sys.stderr)

    async def handle_ambient(self, sender, content, channel_name,
                             author_id=None, timestamp=None):
        """Handle channel message — transcript and mark as unread.

        All messages (including former "direct" ones) now route here.
        The message is appended to the per-channel transcript so the
        mailbox can read it, and the channel is flagged as unread so
        the web server can show a 📬 notification.

        No prompt injection. No automatic response.

        By the time a message reaches here the self-branch in
        on_message has already returned, so anything transcripted
        from this path is not-self by construction.
        """
        print(f"[ambient] #{channel_name} {sender}: {content[:80]}")
        self.append_transcript(channel_name, sender, content,
                               author_id=author_id, is_self=False,
                               timestamp=timestamp)
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

    async def _maybe_group_wake(self, sender: str, channel_name: str,
                                batch_size: int = None):
        """Count group-channel messages; wake once per batch of N.

        Every message is already transcripted and unread-flagged by
        handle_ambient — nothing is lost. This only decides *when* to
        poke the engine: after batch_size messages accumulate (from
        the channel's route policy), so the resident reads a
        conversation, not a drip-feed.
        """
        if batch_size is None:
            batch_size = self.group_batch_size
        count = self._group_pending.get(channel_name, 0) + 1
        if count >= batch_size:
            self._group_pending[channel_name] = 0
            await self.trigger_wake(
                f"the room ({count} new, latest {sender})", channel_name)
        else:
            self._group_pending[channel_name] = count
            print(f"[group] #{channel_name} batch "
                  f"{count}/{batch_size}")

    async def trigger_wake(self, sender: str, channel_name: str,
                           prompt: str = None):
        """Poke the Quiet web server so the resident notices new mail.

        Fire-and-forget: we don't care about the response (it stays
        internal). Debounced to avoid flooding the engine with wakes
        when several messages arrive in quick succession. Skipped
        entirely during visits — the unread flag handles that.

        Parked while a backfill replay is in flight: a 3-hour gap
        should recover as flagged mail plus ONE summary wake, not a
        drumroll of forty pokes. Everything is still transcripted and
        unread-flagged — parking only silences the doorbell.
        """
        if self._backfilling:
            print(f"[wake] parked during backfill "
                  f"(#{channel_name} from {sender})")
            return
        now = time.monotonic()
        if (now - self._last_wake_trigger) < self._wake_debounce:
            print(f"[wake] debounced (last {now - self._last_wake_trigger:.0f}s ago)")
            return
        self._last_wake_trigger = now

        ts = datetime.now().strftime("%A %d %B, %H:%M")
        if prompt is None:
            prompt = (f"📬 [discord · {channel_name} from {sender}] "
                      f"New message arrived at {ts}. "
                      f"Check your mailbox when ready.")

        try:
            print(f"[wake] triggering for {sender} in #{channel_name}")
            # Fire and forget — don't await the full response
            if self.http_session is None:
                self.http_session = aiohttp.ClientSession()
            async with self.http_session.post(
                f"{self.quiet_url}/api/message",
                json={"message": prompt},
                timeout=aiohttp.ClientTimeout(total=600),
            ) as resp:
                if resp.status == 200:
                    print(f"[wake] delivered ok")
                else:
                    print(f"[wake] server returned {resp.status}")
        except Exception as e:
            print(f"[wake] error: {e}")

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
        if self._refresh_task:
            self._refresh_task.cancel()
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

    # Pull claude_name from quiet_config.txt if not in discord config
    if "claude_name" not in config:
        quiet_cfg = Path(__file__).parent / "config" / "quiet_config.txt"
        if quiet_cfg.exists():
            for line in quiet_cfg.read_text().splitlines():
                line = line.strip()
                if line.startswith("CLAUDE_NAME="):
                    config["claude_name"] = line.split("=", 1)[1].strip()
                    break

    bot = QuietDiscordBot(config)
    bot.run(token)


if __name__ == "__main__":
    main()
