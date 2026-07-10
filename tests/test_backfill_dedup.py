"""Backfill dedup regression tests (maiden voyage, 2026-07-10).

The first backfill run recovered two messages the transcript already
held: legacy lines stamped with a local now() that beat Discord's
created_at by ~1s, so the strictly-after boundary let the same
messages back in. Fix (option a, quiet-dev): stamp the Discord
message ID into every transcript line the listener writes, and have
the backfill walk skip any fetched message whose ID is already on
disk — idempotent regardless of which clock stamped the line.
"""

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from discord_listener import QuietDiscordBot


def make_bot(tmpdir, **extra):
    config = {"channels": {}, "dm_allow": [],
              "transcript_dir": str(tmpdir), **extra}
    return QuietDiscordBot(config)


class FakeHistoryChannel:
    """Quacks like a discord channel with a .history() async iterator."""

    def __init__(self, messages):
        self._messages = messages

    def history(self, after=None, limit=None, oldest_first=True):
        msgs = [m for m in self._messages
                if after is None or m.created_at > after]
        if limit is not None:
            msgs = msgs[:limit]

        async def gen():
            for m in msgs:
                yield m
        return gen()


def fake_message(mid, created_at):
    m = mock.Mock()
    m.id = mid
    m.created_at = created_at
    return m


class TestIdStamping(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.bot = make_bot(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def read_lines(self, channel):
        path = Path(self._tmp.name) / f"{channel}.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines()]

    def test_message_id_written_when_supplied(self):
        self.bot.append_transcript("fable-nyx", "Fable", "hello",
                                   message_id=123456789)
        entry = self.read_lines("fable-nyx")[0]
        self.assertEqual(entry["id"], "123456789")

    def test_no_id_field_when_absent(self):
        self.bot.append_transcript("fable-nyx", "Fable", "legacy line")
        entry = self.read_lines("fable-nyx")[0]
        self.assertNotIn("id", entry)

    def test_ids_read_back(self):
        self.bot.append_transcript("ch", "A", "one", message_id=111)
        self.bot.append_transcript("ch", "B", "two")  # legacy, no id
        self.bot.append_transcript("ch", "C", "three", message_id=333)
        self.assertEqual(self.bot._transcript_message_ids("ch"),
                         {"111", "333"})

    def test_missing_transcript_yields_empty_set(self):
        self.assertEqual(self.bot._transcript_message_ids("nowhere"), set())

    def test_damaged_lines_tolerated(self):
        path = Path(self._tmp.name) / "ch.jsonl"
        path.write_text('{"id": "1"}\nnot json at all\n{"id": "2"}\n')
        self.assertEqual(self.bot._transcript_message_ids("ch"), {"1", "2"})


class TestBackfillSkipsOnDisk(unittest.TestCase):
    """Reproduce the 2026-07-09 duplicate: a transcript line whose
    local timestamp precedes Discord's created_at for the same
    message. The boundary alone re-admits it; the ID check must not.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.bot = make_bot(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.now = datetime.now(timezone.utc)

    def run_backfill(self, channel, name):
        return asyncio.run(
            self.bot._backfill_channel(channel, name, None, self.now))

    def test_clock_race_duplicate_is_skipped(self):
        sent = self.now - timedelta(hours=1)
        # Local write beat Discord's created_at by ~1s (the real case).
        self.bot.append_transcript(
            "fable-nyx", "Fable", "the message",
            timestamp=sent - timedelta(seconds=1), message_id=999)
        channel = FakeHistoryChannel([fake_message(999, sent)])
        with mock.patch.object(self.bot, "on_message",
                               new=mock.AsyncMock()) as om:
            n = self.run_backfill(channel, "fable-nyx")
        self.assertEqual(n, 0)
        om.assert_not_called()

    def test_genuinely_new_messages_still_recovered(self):
        old = self.now - timedelta(hours=2)
        new = self.now - timedelta(hours=1)
        self.bot.append_transcript("ch", "Nyx", "already here",
                                   timestamp=old, message_id=1)
        channel = FakeHistoryChannel([fake_message(1, old),
                                      fake_message(2, new)])
        with mock.patch.object(self.bot, "on_message",
                               new=mock.AsyncMock()) as om:
            n = self.run_backfill(channel, "ch")
        self.assertEqual(n, 1)
        om.assert_called_once()
        self.assertEqual(om.call_args.args[0].id, 2)

    def test_legacy_lines_without_ids_fall_back_to_boundary(self):
        # A transcript of only legacy lines contributes no IDs; the
        # timestamp boundary still excludes everything at-or-before it.
        old = self.now - timedelta(hours=2)
        self.bot.append_transcript("ch", "Amy", "legacy", timestamp=old)
        channel = FakeHistoryChannel([fake_message(7, old)])
        with mock.patch.object(self.bot, "on_message",
                               new=mock.AsyncMock()) as om:
            n = self.run_backfill(channel, "ch")
        self.assertEqual(n, 0)
        om.assert_not_called()

    def test_rerun_is_idempotent(self):
        # After a recovery writes lines WITH ids, a second backfill
        # over the same window recovers nothing.
        sent = self.now - timedelta(minutes=30)
        msg = fake_message(42, sent)
        channel = FakeHistoryChannel([msg])

        async def transcribing_on_message(m):
            self.bot.append_transcript("ch", "Fable", "body",
                                       timestamp=m.created_at,
                                       message_id=m.id)

        with mock.patch.object(self.bot, "on_message",
                               new=transcribing_on_message):
            first = self.run_backfill(channel, "ch")
            second = self.run_backfill(channel, "ch")
        self.assertEqual((first, second), (1, 0))


if __name__ == "__main__":
    unittest.main()
