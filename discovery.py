#!/usr/bin/env python3
"""Channel discovery for Quiet — zero-config Discord routing.

Design (quiet-devs, 2026-07-08; Amy's admin-token insight, Nyx's
hardenings, Fable's probe):

    One source of truth: the Discord server itself. Channels, their
    topics, and their permission overwrites tell us everything the old
    channel-config files guessed at. No channel list to maintain, no
    stale entries to cause undiagnosable 400s.

Membership: the bot is "in" a channel if it has an explicit member
overwrite, or if the channel isn't denied to @everyone. Channels we're
not in produce NOTHING — no transcript, no wake, no route. Under-record
is the recoverable error (fail toward silence).

Policy: carried in the channel topic, one line:

    quiet: mode=group batch=5 backfill=24h

The backfill key bounds how far back the listener reaches when it
reconnects after downtime (see discord_listener._backfill). Durations
are <n>m / <n>h / <n>d; `backfill=0` opts a channel out entirely.

Strict parsing — a malformed policy line falls back to shape defaults
and logs a warning; it never guesses (Nyx's hardening #2).

Shape defaults when no policy line exists:
    - private channel, exactly 2 member overwrites  -> direct
    - private channel, 3+ member overwrites         -> group
    - public channel (no @everyone deny)            -> ambient

Rate-limit honesty: discovery runs at startup and on a slow refresh
(daily), never per-message (hardening #3). The output is an immutable
snapshot; callers swap it in atomically so in-flight message handling
never races a refresh.

Two fetchers, one brain:
    - fetch_rest(token)        for standalone tools (write_channel)
    - the listener feeds its gateway cache straight to classify()
"""

import json
import re
import sys
import urllib.request

API = "https://discord.com/api/v10"

TEXT_CHANNEL = 0
OW_ROLE = 0
OW_MEMBER = 1

VALID_MODES = {"direct", "group", "ambient"}

# ---------------------------------------------------------------- fetch

def _get(url: str, token: str):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bot {token}",
        # Discord 403s the default Python User-Agent (probe, 2026-07-08)
        "User-Agent": "DiscordBot (quiet, 0.1)",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def fetch_rest(token: str) -> list[dict]:
    """Fetch raw channel dicts for the bot's first guild via REST.

    One GET for the guild list, one for the channels. Used by
    standalone tools; the listener uses its gateway cache instead.
    """
    guilds = _get(f"{API}/users/@me/guilds", token)
    if not guilds:
        return []
    guild_id = guilds[0]["id"]
    channels = _get(f"{API}/guilds/{guild_id}/channels", token)
    for c in channels:
        c["_guild_id"] = guild_id
    return channels


def from_gateway(guild) -> list[dict]:
    """Convert discord.py gateway-cache channels to raw dicts.

    Duck-typed: `guild` needs .id and .text_channels, each channel
    needs .id, .name, .topic, .overwrites (a dict keyed by
    Role/Member objects). No discord.py import — this module stays
    dependency-free so write_channel can use it too.
    """
    out = []
    for ch in guild.text_channels:
        ows = []
        for target, ow in ch.overwrites.items():
            # discord.Role has .position; discord.Member doesn't
            ow_type = OW_ROLE if hasattr(target, "position") else OW_MEMBER
            allow, deny = ow.pair()
            ows.append({
                "id": str(target.id),
                "type": ow_type,
                "allow": str(allow.value),
                "deny": str(deny.value),
            })
        out.append({
            "id": str(ch.id),
            "name": ch.name,
            "type": TEXT_CHANNEL,
            "topic": ch.topic,
            "permission_overwrites": ows,
            "_guild_id": str(guild.id),
        })
    return out

# ------------------------------------------------------------- classify

VIEW_CHANNEL = 1 << 10  # Discord permission bit


_DURATION_RE = re.compile(r"^(\d+)([mhd])$")
_DURATION_UNITS = {"m": 60, "h": 3600, "d": 86400}


def _parse_duration(v: str) -> int | None:
    """Parse `90m` / `24h` / `7d` / `0` into seconds. None if malformed.

    Zero is a valid duration meaning "never" — callers treat a
    backfill window of 0 seconds as an opt-out.
    """
    if v == "0":
        return 0
    m = _DURATION_RE.match(v)
    if not m:
        return None
    return int(m.group(1)) * _DURATION_UNITS[m.group(2)]


def _parse_policy(topic: str, channel_name: str, log=None) -> dict | None:
    """Extract `quiet: k=v k=v` policy from a topic. Strict.

    Returns a dict of recognised keys, or None if there is no policy
    line. A malformed line returns None *and* logs a warning —
    defaults apply, nothing is guessed.
    """
    if not topic:
        return None
    m = re.search(r"^quiet:\s*(.+)$", topic, re.MULTILINE)
    if not m:
        return None
    log = log or (lambda msg: print(msg, file=sys.stderr))
    policy = {}
    for kv in m.group(1).split():
        if "=" not in kv:
            log(f"[discovery] #{channel_name}: malformed policy token "
                f"{kv!r} — using shape defaults")
            return None
        k, v = kv.split("=", 1)
        if k == "mode":
            if v not in VALID_MODES:
                log(f"[discovery] #{channel_name}: unknown mode {v!r} "
                    f"— using shape defaults")
                return None
            policy["mode"] = v
        elif k == "batch":
            if not v.isdigit() or int(v) < 1:
                log(f"[discovery] #{channel_name}: bad batch {v!r} "
                    f"— using shape defaults")
                return None
            policy["batch"] = int(v)
        elif k == "backfill":
            seconds = _parse_duration(v)
            if seconds is None:
                log(f"[discovery] #{channel_name}: bad backfill {v!r} "
                    f"— using shape defaults")
                return None
            policy["backfill"] = seconds
        else:
            # Unknown keys are ignored, not fatal — forward compat.
            log(f"[discovery] #{channel_name}: ignoring unknown policy "
                f"key {k!r}")
    return policy or None


def classify(raw_channels: list[dict], bot_id: str, log=None) -> dict:
    """Turn raw channel dicts into a routing table: name -> route.

    Only channels the bot is a member of appear. Route dict:
        {"id", "name", "mode", "batch", "policy_source"}
    """
    bot_id = str(bot_id)
    table = {}
    for c in raw_channels:
        if c.get("type") != TEXT_CHANNEL:
            continue
        guild_id = c.get("_guild_id") or c.get("guild_id")
        ows = c.get("permission_overwrites", [])

        everyone_denied = any(
            o["type"] == OW_ROLE and str(o["id"]) == str(guild_id)
            and int(o.get("deny", 0)) & VIEW_CHANNEL
            for o in ows)
        member_ids = [str(o["id"]) for o in ows
                      if o["type"] == OW_MEMBER
                      and int(o.get("allow", 0)) & VIEW_CHANNEL]
        is_member = bot_id in member_ids

        if everyone_denied and not is_member:
            continue  # not our room: no transcript, no wake, no route

        # Shape defaults
        if not everyone_denied:
            default_mode = "ambient"
        elif len(member_ids) <= 2:
            default_mode = "direct"
        else:
            default_mode = "group"

        topic = c.get("topic") or ""
        policy = _parse_policy(topic, c["name"], log)
        # Human-facing room sign: the topic minus the policy line.
        # Topics may carry both, one per line; strip `quiet:` lines
        # and whatever text remains is the description.
        description = re.sub(
            r"^quiet:.*$", "", topic, flags=re.MULTILINE).strip()
        route = {
            "id": str(c["id"]),
            "name": c["name"],
            "mode": default_mode,
            "batch": 5,
            "policy_source": "shape-default",
        }
        if description:
            route["description"] = description
        if policy:
            route["policy_source"] = "topic"
            route.update(policy)
        table[c["name"]] = route
    return table

# ------------------------------------------------------------- snapshot

class RoutingTable:
    """Immutable routing snapshot.

    Handlers hold ONE reference to the current instance per message
    and never cache it across awaits; refreshes build a new instance
    and rebind atomically. Ghost cleanup is implicit: deleted channels
    simply aren't in the next snapshot.
    """

    def __init__(self, table: dict):
        self._by_name = dict(table)
        self._by_id = {r["id"]: r for r in table.values()}

    def by_name(self, name: str) -> dict | None:
        return self._by_name.get(name)

    def by_id(self, channel_id) -> dict | None:
        return self._by_id.get(str(channel_id))

    def names(self) -> list[str]:
        return sorted(self._by_name)

    def __len__(self):
        return len(self._by_name)

    def __contains__(self, name):
        return name in self._by_name or str(name) in self._by_id


def discover_rest(token: str, bot_id: str, log=None) -> RoutingTable:
    """Standalone-tool entry point: fetch + classify in one call."""
    return RoutingTable(classify(fetch_rest(token), bot_id, log))


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Quiet channel discovery")
    p.add_argument("token")
    p.add_argument("bot_id")
    args = p.parse_args()
    table = discover_rest(args.token, args.bot_id)
    for name in table.names():
        r = table.by_name(name)
        print(f"#{name:<20} {r['mode']:<8} batch={r['batch']} "
              f"({r['policy_source']})  id={r['id']}")
