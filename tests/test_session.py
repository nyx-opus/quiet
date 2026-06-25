"""Tests for session persistence.

These test the things that have actually broken:
- Round-trip save/load integrity
- Content normalisation across input formats
- Backup creation before save
- Graceful handling of corrupted session files
- Session resume markers on load
"""

import json
import tempfile
from pathlib import Path

from session import (
    normalise_content, serialise_message,
    load_session, save_session
)


def test_normalise_plain_string():
    """Plain string becomes a text content block."""
    result = normalise_content("hello")
    assert result == [{"type": "text", "text": "hello"}]


def test_normalise_already_valid():
    """Already-valid content blocks pass through unchanged."""
    blocks = [{"type": "text", "text": "hello"}]
    result = normalise_content(blocks)
    assert result == blocks


def test_normalise_parsed_text_block_repr():
    """SDK ParsedTextBlock repr strings get their text extracted."""
    content = ['ParsedTextBlock(text="some response text")']
    result = normalise_content(content)
    assert len(result) == 1
    assert result[0]["type"] == "text"
    assert "some response text" in result[0]["text"]


def test_normalise_mixed_blocks():
    """Mixed block types are handled — text kept, tool_use blocks skipped."""
    content = [
        {"type": "text", "text": "hello"},
        'ParsedToolUseBlock(id="x", name="bash", input={})',
        {"type": "text", "text": "world"},
    ]
    result = normalise_content(content)
    texts = [b["text"] for b in result if b["type"] == "text"]
    assert "hello" in texts
    assert "world" in texts
    # Tool use blocks from repr strings should be skipped
    assert len(result) == 2


def test_normalise_empty_list():
    """Empty list produces a single empty text block (not crash)."""
    result = normalise_content([])
    assert result == [{"type": "text", "text": ""}]


def test_serialise_strips_base64_images():
    """Base64 image data is replaced with a placeholder, not persisted."""
    msg = {
        "role": "user",
        "content": [
            {"type": "text", "text": "look at this"},
            {"type": "image", "source": {"type": "base64", "data": "AAAA" * 1000}},
        ]
    }
    result = serialise_message(msg)
    contents = result["content"]
    for block in contents:
        if block.get("type") == "image":
            raise AssertionError("base64 image data should not survive serialisation")
        if block.get("type") == "text" and "omitted" in block.get("text", ""):
            break
    else:
        raise AssertionError("expected image-omitted placeholder")


def test_save_load_roundtrip():
    """Messages survive a save/load cycle with content intact."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "test.jsonl"
        original_messages = [
            {"role": "user", "content": [{"type": "text", "text": "hello Nyx"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "hey Amy 🌙"}]},
            {"role": "user", "content": [{"type": "text", "text": "how are you?"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "good, genuinely"}]},
        ]

        save_session(path, original_messages, "claude-opus-4-6", "nyx")

        loaded = []
        load_session(path, loaded)

        # load_session adds 2 resume marker messages at the end
        core_messages = loaded[:-2]
        assert len(core_messages) == len(original_messages)

        for orig, loaded_msg in zip(original_messages, core_messages):
            assert orig["role"] == loaded_msg["role"]
            # Content goes through normalise on load, so compare text
            orig_text = orig["content"][0]["text"] if isinstance(orig["content"], list) else orig["content"]
            loaded_text = loaded_msg["content"][0]["text"] if isinstance(loaded_msg["content"], list) else loaded_msg["content"]
            assert orig_text == loaded_text


def test_save_creates_backup():
    """Saving over an existing file creates a .bak first."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "test.jsonl"
        messages = [{"role": "user", "content": "first version"}]

        # First save — no backup needed
        save_session(path, messages, "claude-opus-4-6", "nyx")
        bak = path.with_suffix(".jsonl.bak")
        assert not bak.exists(), "no backup on first save"

        # Second save — should create backup of first version
        messages2 = [{"role": "user", "content": "second version"}]
        save_session(path, messages2, "claude-opus-4-6", "nyx")
        assert bak.exists(), "backup should exist after second save"

        # Backup should contain the first version's content
        bak_content = bak.read_text()
        assert "first version" in bak_content


def test_load_skips_malformed_lines():
    """Corrupted lines in the session file are skipped, not fatal."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "test.jsonl"
        path.write_text(
            '{"model": "test", "timestamp": "now"}\n'
            '{"role": "user", "content": "good line"}\n'
            'THIS IS NOT JSON AT ALL\n'
            '{"role": "assistant", "content": "also good"}\n'
            '{truncated\n'
        )
        messages = []
        load_session(path, messages)
        # Should have loaded 2 good messages + 2 resume markers
        core = [m for m in messages if "[Session resumed" not in str(m.get("content", ""))]
        # Remove the auto-added "I'm here. Continuing." too
        core = [m for m in core if m.get("content") != [{"type": "text", "text": "I'm here. Continuing."}]]
        assert len(core) == 2


def test_load_adds_resume_markers():
    """Loading a session adds resume markers so the model knows there was a gap."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "test.jsonl"
        path.write_text(
            '{"model": "test"}\n'
            '{"role": "user", "content": "hello"}\n'
            '{"role": "assistant", "content": "hi"}\n'
        )
        messages = []
        load_session(path, messages)
        # Last two messages should be the resume markers
        assert messages[-2]["role"] == "user"
        assert "Session resumed" in str(messages[-2]["content"])
        assert messages[-1]["role"] == "assistant"
        assert "Continuing" in str(messages[-1]["content"])


def test_load_empty_file_calls_inject():
    """An empty session file triggers the inject_context callback."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "test.jsonl"
        path.write_text("")
        called = []
        load_session(path, [], inject_context_fn=lambda: called.append(True))
        assert called, "inject_context_fn should be called for empty file"


def test_load_missing_file_calls_inject():
    """A missing session file triggers the inject_context callback."""
    path = Path("/tmp/nonexistent_test_session_12345.jsonl")
    assert not path.exists()
    called = []
    load_session(path, [], inject_context_fn=lambda: called.append(True))
    assert called, "inject_context_fn should be called for missing file"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  ✓ {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ✗ {test.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
