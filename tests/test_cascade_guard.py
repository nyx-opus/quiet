"""Cascade guard regression tests (incidents 2026-07-02, 2026-07-04).

Two sibling bots in a "direct" channel reply to each other forever:
every reply is a new message ID (dedup can't catch it) containing
sensible text (the noise filter can't catch it). The guard caps
consecutive auto-responses to bot-authored messages per channel,
parks the conversation past the cap, and re-opens on human voice
or cooldown expiry.
"""

import time
import unittest
from unittest import mock

from discord_listener import QuietDiscordBot


def make_bot(**guard):
    config = {"channels": {}, "dm_allow": []}
    if guard:
        config["cascade_guard"] = guard
    return QuietDiscordBot(config)


class TestCascadeAllow(unittest.TestCase):

    def test_allows_up_to_cap_then_parks(self):
        bot = make_bot(max_bot_exchanges=4)
        results = [bot._cascade_allow("nyx-orange") for _ in range(6)]
        self.assertEqual(results, [True, True, True, True, False, False])

    def test_channels_are_independent(self):
        bot = make_bot(max_bot_exchanges=1)
        self.assertTrue(bot._cascade_allow("a"))
        self.assertFalse(bot._cascade_allow("a"))
        self.assertTrue(bot._cascade_allow("b"))  # fresh channel, fresh count

    def test_human_message_resets_counter(self):
        bot = make_bot(max_bot_exchanges=2)
        bot._cascade_allow("ch")
        bot._cascade_allow("ch")
        self.assertFalse(bot._cascade_allow("ch"))  # parked
        bot._bot_chain.pop("ch", None)  # what on_message does for humans
        self.assertTrue(bot._cascade_allow("ch"))   # floor re-opened

    def test_cooldown_expiry_reopens(self):
        bot = make_bot(max_bot_exchanges=1, cooldown_minutes=30)
        with mock.patch("discord_listener.time.monotonic") as clock:
            clock.return_value = 1000.0
            self.assertTrue(bot._cascade_allow("ch"))
            self.assertFalse(bot._cascade_allow("ch"))  # parked
            clock.return_value = 1000.0 + 31 * 60  # past cooldown
            self.assertTrue(bot._cascade_allow("ch"))

    def test_live_loop_cannot_outwait_cooldown(self):
        """A loop that keeps firing refreshes the window: parked stays
        parked until the loop actually stops for a full cooldown."""
        bot = make_bot(max_bot_exchanges=1, cooldown_minutes=30)
        with mock.patch("discord_listener.time.monotonic") as clock:
            clock.return_value = 0.0
            self.assertTrue(bot._cascade_allow("ch"))
            # Loop fires every 10 minutes — each refresh extends the park
            for minute in (10, 20, 30, 40, 50, 60):
                clock.return_value = minute * 60.0
                self.assertFalse(bot._cascade_allow("ch"))
            # Loop stops; 31 quiet minutes later the floor re-opens
            clock.return_value = (60 + 31) * 60.0
            self.assertTrue(bot._cascade_allow("ch"))

    def test_defaults_when_unconfigured(self):
        bot = make_bot()
        self.assertEqual(bot.cascade_cap, 4)
        self.assertEqual(bot.cascade_cooldown, 30 * 60.0)
        self.assertEqual(bot.bot_reply_delay, 150.0)


if __name__ == "__main__":
    unittest.main()
