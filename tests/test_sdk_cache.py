"""Tests for prompt-cache breakpoint advancement in the SDK backend.

Regression test for the 2026-07-04 caching bug: sdk_send appended
response.content as raw SDK objects (TextBlock/ToolUseBlock), not dicts.
_set_cache_breakpoint only recognises dict blocks, so on every live turn
after the first, its isinstance checks fell through and it returned None.
The breakpoint stayed frozen at the session-load position and the entire
live conversation history paid full input rate on every call — observed
as $1.20+ per message on 65k-token histories (OpenRouter activity CSV,
2026-07-03: 65,438 prompt tokens, only 1,360 cached).

The fix: normalise response.content to plain dicts at append time using
session.normalise_content.
"""

from backends.sdk import _set_cache_breakpoint
from session import normalise_content


class MockTextBlock:
    """Stands in for anthropic.types.TextBlock."""
    type = "text"

    def __init__(self, text):
        self.text = text


class MockToolUseBlock:
    """Stands in for anthropic.types.ToolUseBlock."""
    type = "tool_use"

    def __init__(self, id="tu_1", name="bash", input=None):
        self.id = id
        self.name = name
        self.input = input or {}


def _breakpoints(messages):
    return [(i, j) for i, m in enumerate(messages)
            for j, b in enumerate(m.get("content", []))
            if isinstance(b, dict) and "cache_control" in b]


def test_raw_sdk_objects_freeze_breakpoint():
    """Documents the bug: raw SDK objects are invisible to the
    breakpoint setter, so it returns None and never advances."""
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "turn 1"}]},
        {"role": "assistant", "content": [MockTextBlock("reply 1")]},
        {"role": "user", "content": [{"type": "text", "text": "turn 2"}]},
    ]
    result = _set_cache_breakpoint(messages)
    assert result is None, "raw SDK objects should not be markable"


def test_normalised_content_advances_breakpoint():
    """The fix: content normalised at append time advances the
    breakpoint every turn."""
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "loaded 1"}]},
        {"role": "assistant",
         "content": [{"type": "text", "text": "loaded reply 1"}]},
    ]
    for turn in range(1, 4):
        messages.append({"role": "user", "content": [
            {"type": "text", "text": f"live turn {turn}"}]})
        result = _set_cache_breakpoint(messages)
        assert result is not None, f"breakpoint frozen on turn {turn}"
        assert result[0] == len(messages) - 2, "breakpoint didn't advance"
        messages.append({"role": "assistant", "content": normalise_content(
            [MockTextBlock(f"live reply {turn}")])})
    bps = _breakpoints(messages)
    # Old breakpoints pruned: at most 2 conversation breakpoints
    # (+ 1 system = 3, within the API's limit of 4).
    assert len(bps) <= 2, f"too many breakpoints: {bps}"


def test_normalise_handles_tool_use_blocks():
    """Tool-use turns normalise to valid dicts and remain markable."""
    content = normalise_content([MockTextBlock("sure"), MockToolUseBlock()])
    assert content == [
        {"type": "text", "text": "sure"},
        {"type": "tool_use", "id": "tu_1", "name": "bash", "input": {}},
    ]
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "run it"}]},
        {"role": "assistant", "content": content},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": "ok"}]},
        {"role": "user", "content": [{"type": "text", "text": "next"}]},
    ]
    assert _set_cache_breakpoint(messages) is not None


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✓ {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  ✗ {t.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
