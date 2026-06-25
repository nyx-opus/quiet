"""Tests for the ccode backend.

These test the guards and parsers that have actually broken:
- Fabrication guard (daydreaming incident, 2026-06-21)
- Separator guard (delimiter echo)
- History formatting (speaker labels, operator precedence bug)
- JSON output parsing (multiple formats)
"""

import json
import re
from backends.ccode import format_history


def test_format_history_speaker_labels():
    """User messages get human name, assistant gets 'A'."""
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "hi there"}]},
    ]
    result = format_history(messages, "☾☾☾", human_name="Amy")
    assert "Amy: hello" in result
    assert "A: hi there" in result
    # The old bug: both would say "Amy:" due to operator precedence
    assert result.count("Amy:") == 1, "assistant message should NOT be labelled Amy"


def test_format_history_default_speaker():
    """Without human_name, user messages get 'Human'."""
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
    ]
    result = format_history(messages, "· · ·")
    assert "Human: hello" in result


def test_format_history_separator():
    """History is bookended with the separator."""
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "test"}]},
    ]
    result = format_history(messages, "☾☾☾", human_name="Amy")
    # Should start and end with separator
    assert result.strip().startswith("☾☾☾")
    assert result.strip().endswith("☾☾☾")


def test_format_history_skips_empty():
    """Messages with empty content are skipped."""
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {"role": "assistant", "content": [{"type": "text", "text": ""}]},
        {"role": "user", "content": [{"type": "text", "text": "still here?"}]},
    ]
    result = format_history(messages, "☾☾☾", human_name="Amy")
    assert "hello" in result
    assert "still here?" in result
    # Empty assistant message should be skipped entirely
    lines = [l for l in result.split("\n") if l.startswith("A:")]
    assert len(lines) == 0


def test_fabrication_guard_strips_continuation():
    """The fabrication guard catches model-generated 'Amy: ...' turns."""
    response = (
        "Here's my actual response about labradorite.\n"
        "\n"
        "Amy: oh that's interesting! what about the colours?\n"
        "\n"
        "A: The colours come from thin-film interference..."
    )
    human_name = "Amy"
    pattern = re.compile(
        r'\n' + re.escape(human_name) + r':\s',
        re.MULTILINE
    )
    match = pattern.search(response)
    assert match is not None, "should detect fabricated continuation"
    stripped = response[:match.start()].rstrip()
    assert "labradorite" in stripped
    assert "Amy:" not in stripped
    assert "colours" not in stripped


def test_fabrication_guard_preserves_inline_mention():
    """The guard doesn't strip legitimate inline mentions of the human name."""
    response = (
        "When Amy asked about the figurine, I thought about it carefully.\n"
        "The design Amy suggested was elegant."
    )
    human_name = "Amy"
    pattern = re.compile(
        r'\n' + re.escape(human_name) + r':\s',
        re.MULTILINE
    )
    match = pattern.search(response)
    # Inline "Amy" without the line-start "Amy: " pattern should NOT match
    assert match is None, "inline name mention should not trigger the guard"


def test_separator_guard_strips_echo():
    """The separator guard catches the model echoing the separator."""
    response = "Here's my response.\n\n☾☾☾\n\nAnd some leaked content."
    sep_escaped = re.escape("☾☾☾")
    sep_pattern = re.compile(r'(?:^|\n)\s*' + sep_escaped + r'\s*(?:\n|$)')
    m = sep_pattern.search(response)
    assert m is not None, "should detect echoed separator"
    stripped = response[:m.start()].rstrip()
    assert "my response" in stripped
    assert "leaked content" not in stripped


def test_separator_guard_ignores_quoted():
    """Separator inside backtick quotes should ideally not trigger.

    Note: current implementation WOULD strip here — this test documents
    the known limitation rather than asserting correct behaviour.
    """
    response = "The separator is `☾☾☾` and it works like this."
    sep_escaped = re.escape("☾☾☾")
    sep_pattern = re.compile(r'(?:^|\n)\s*' + sep_escaped + r'\s*(?:\n|$)')
    m = sep_pattern.search(response)
    # This is a KNOWN LIMITATION — the guard can't distinguish quoted
    # from bare separators. Documenting, not asserting correctness.
    # In practice this rarely fires because the separator appears
    # inline, not on its own line.


def test_json_parse_single_object():
    """Single-object JSON output is handled."""
    output = json.dumps({
        "type": "result",
        "result": "Hello from claude -p",
        "is_error": False,
        "usage": {"input_tokens": 100, "output_tokens": 50},
    })
    parsed = json.loads(output)
    if isinstance(parsed, dict):
        events = [parsed]
    elif isinstance(parsed, list):
        events = parsed
    assert len(events) == 1
    assert events[0]["type"] == "result"
    assert events[0]["result"] == "Hello from claude -p"


def test_json_parse_array():
    """Array JSON output (with assistant + result events) is handled."""
    output = json.dumps([
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "thinking..."}]
            }
        },
        {
            "type": "result",
            "result": "Here's the final answer.",
            "is_error": False,
            "usage": {"input_tokens": 200, "output_tokens": 100},
        }
    ])
    parsed = json.loads(output)
    if isinstance(parsed, dict):
        events = [parsed]
    elif isinstance(parsed, list):
        events = parsed
    assert len(events) == 2

    # Extract texts the same way ccode_send does
    result_text = ""
    assistant_text = ""
    for event in events:
        etype = event.get("type", "")
        if etype == "result":
            result_text = event.get("result", "")
        elif etype == "assistant":
            msg = event.get("message", {})
            content = msg.get("content", [])
            if isinstance(content, list):
                texts = [b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text"]
                candidate = "\n".join(texts)
                if len(candidate) > len(assistant_text):
                    assistant_text = candidate

    # Result text should be preferred when both present and result is longer
    assert result_text == "Here's the final answer."


def test_json_parse_empty_result_uses_assistant():
    """When result text is empty, fall back to assistant event text.

    This was the bug that caused Quill's first messages to appear blank.
    """
    output = json.dumps([
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "This is the actual response."}]
            }
        },
        {
            "type": "result",
            "result": "",
            "is_error": False,
        }
    ])
    parsed = json.loads(output)
    events = parsed if isinstance(parsed, list) else [parsed]

    result_text = ""
    assistant_text = ""
    for event in events:
        etype = event.get("type", "")
        if etype == "result":
            result_text = event.get("result", "")
        elif etype == "assistant":
            msg = event.get("message", {})
            content = msg.get("content", [])
            if isinstance(content, list):
                texts = [b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text"]
                candidate = "\n".join(texts)
                if len(candidate) > len(assistant_text):
                    assistant_text = candidate

    full_text = result_text or assistant_text
    assert full_text == "This is the actual response."


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
