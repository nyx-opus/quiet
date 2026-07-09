"""Tests for the mailbox file-send and message splitter."""

import os
import tempfile
import pytest
from engine import QuietEngine, MAILBOX_FILE_SEND


class TestMailboxFileSendRegex:
    """Test the MAILBOX_FILE_SEND regex pattern."""

    def test_basic_match(self):
        m = MAILBOX_FILE_SEND.search("*message Orange /tmp/letter.txt*")
        assert m
        assert m.group(1) == "Orange"
        assert m.group(2) == "/tmp/letter.txt"

    def test_case_insensitive(self):
        m = MAILBOX_FILE_SEND.search("*Message orange /tmp/note.txt*")
        assert m
        assert m.group(1) == "orange"

    def test_relative_path(self):
        m = MAILBOX_FILE_SEND.search("*message Amy tmp/amyletter.txt*")
        assert m
        assert m.group(2) == "tmp/amyletter.txt"

    def test_no_match_in_prose(self):
        """Disease A: narrating the command in prose shouldn't fire."""
        text = "I could *message Orange /tmp/letter.txt* if I wanted to"
        # The regex requires ^ (start of line), so mid-line shouldn't match
        m = MAILBOX_FILE_SEND.search(text)
        # This IS at the start of a "line" within the string — MULTILINE
        # makes ^ match after \n. The text has no newline before *message,
        # so if the whole string starts with other text, it shouldn't match.
        assert m is None

    def test_start_of_line(self):
        text = "here is my plan:\n*message Orange /tmp/letter.txt*"
        m = MAILBOX_FILE_SEND.search(text)
        assert m
        assert m.group(1) == "Orange"


class TestMessageSplitter:
    """Test the _split_message static method."""

    def test_short_message_no_split(self):
        chunks = QuietEngine._split_message("Hello, Orange!", max_len=1900)
        assert chunks == ["Hello, Orange!"]

    def test_exact_limit(self):
        text = "x" * 1900
        chunks = QuietEngine._split_message(text, max_len=1900)
        assert chunks == [text]

    def test_paragraph_split(self):
        para1 = "A" * 1000
        para2 = "B" * 1000
        text = para1 + "\n\n" + para2
        chunks = QuietEngine._split_message(text, max_len=1500)
        assert len(chunks) == 2
        assert chunks[0] == para1
        assert chunks[1] == para2

    def test_sentence_split(self):
        sent1 = "A" * 900 + "."
        sent2 = "B" * 900 + "."
        text = sent1 + " " + sent2
        chunks = QuietEngine._split_message(text, max_len=1200)
        assert len(chunks) == 2
        assert chunks[0] == sent1

    def test_hard_wrap(self):
        text = " ".join(["word"] * 500)  # ~2500 chars
        chunks = QuietEngine._split_message(text, max_len=1000)
        assert len(chunks) >= 2
        for chunk in chunks:
            assert len(chunk) <= 1000


class TestMailboxFileSendHandler:
    """Test _mailbox_file_send with actual files."""

    def test_file_not_found(self):
        engine = QuietEngine.__new__(QuietEngine)
        result = engine._mailbox_file_send("Orange", "/tmp/nonexistent_xyz.txt")
        assert "not found" in result

    def test_empty_file(self):
        engine = QuietEngine.__new__(QuietEngine)
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("")
            path = f.name
        try:
            result = engine._mailbox_file_send("Orange", path)
            assert "empty" in result
        finally:
            os.unlink(path)

    def test_reads_file_content(self):
        """Verify file content is read (send will fail without write_channel,
        but we can check the file reading works by using a missing tool)."""
        engine = QuietEngine.__new__(QuietEngine)
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("Hello from the letter!\n*emphasis* works fine in here.")
            path = f.name
        try:
            result = engine._mailbox_file_send("Orange", path)
            # Will fail at write_channel but shouldn't fail at file reading
            # So either "sent" or "Couldn't send" (not "not found" or "empty")
            assert "not found" not in result or "write_channel" in result
        finally:
            os.unlink(path)


class TestSelfFlagFiltering:
    """Self-detection by bot ID, stamped at write time (2026-07-09).

    The listener records "self": true/false in each transcript entry,
    derived from message.author.id == bot's own ID. The mailbox trusts
    the flag when present; name heuristics apply only to legacy lines
    that predate the field.
    """

    def _filter(self, entries):
        """Mirror the mailbox's skip logic on a list of dict entries."""
        import json as _json
        from engine import QuietEngine
        eng = QuietEngine.__new__(QuietEngine)  # no init needed
        kept = []
        for msg in entries:
            self_flag = msg.get("self")
            if self_flag is True:
                continue
            if self_flag is None and eng._is_self_sender(msg["sender"]):
                continue
            kept.append(msg["sender"])
        return kept

    def test_flag_true_skipped_regardless_of_name(self):
        entries = [{"sender": "𝒬𝓊𝒾𝓁𝓁 🪶", "self": True}]
        assert self._filter(entries) == []

    def test_flag_false_kept_regardless_of_name(self):
        # Even a sender whose name might match heuristics is kept
        # when the listener says it wasn't us.
        entries = [{"sender": "self", "self": False}]
        assert self._filter(entries) == ["self"]

    def test_legacy_line_falls_back_to_name(self):
        entries = [{"sender": "self"}, {"sender": "Amy"}]
        assert self._filter(entries) == ["Amy"]

    def test_decorated_names_need_no_config(self):
        # The whole point: unicode display names are irrelevant when
        # the flag is present.
        entries = [
            {"sender": "ɴʏx 🌙", "self": False},
            {"sender": "𝐎𝐫𝐚𝐧𝐠𝐞 🍊", "self": False},
            {"sender": "ᶠᵃᵇˡᵉ", "self": True},
        ]
        assert self._filter(entries) == ["ɴʏx 🌙", "𝐎𝐫𝐚𝐧𝐠𝐞 🍊"]
