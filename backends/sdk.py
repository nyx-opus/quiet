"""
Anthropic SDK backend for Quiet.

Direct API calls via the Anthropic Python SDK. Supports streaming,
tool use loops, and proper message-array formatting (no role confusion).

This is the cleaner backend — proper structured messages, no text
formatting, no fabrication risk. Used for API key or OpenRouter auth.
The trade-off is cost: API billing vs subscription.

Battle scar: prompt caching. Without cache breakpoints, every API call
sends the full conversation history at full input token rates. A 60k-token
history at Opus 4.0 rates ($15/M) costs $0.90 per message just in input.
With caching, the repeated prefix is charged at cache-read rate ($1.50/M)
— a ~90% reduction. We add an ephemeral cache breakpoint on the last
message in the existing history before each call. Old breakpoints are
PRESERVED between turns so the cached prefix matches — clearing them
was causing full cache misses on every turn, paying 125% of input rate
for content that should have been 10%. Session serialisation strips
cache_control metadata so it doesn't persist to disk.
"""

import sys
from typing import Callable
from tools import execute_tool


def _set_cache_breakpoint(messages: list):
    """Add cache_control to the last historical message for prompt caching.

    The last message in the list is always the new user input. We mark
    the second-to-last message (the end of existing history) as a cache
    breakpoint so the entire preceding conversation is cached.

    Crucially, we PRESERVE existing breakpoints from previous turns.
    This is what enables cache hits between turns: the prefix up to
    the old breakpoint is identical to what was cached last time, so
    the provider returns a cache read instead of re-processing.

    Without this, the conversation cache is written fresh every turn
    (at 125% of input cost) and never read — only the system prompt's
    permanent breakpoint survives, caching just ~4.6k identity tokens
    while the entire conversation pays cache-write rates.

    We clean up breakpoints older than the most recent to stay within
    the API's 4-breakpoint limit (1 system + up to 3 conversation).

    Returns (msg_index, block_index, was_converted) for reference, or None.
    """
    if len(messages) < 2:
        return None

    target_idx = len(messages) - 2

    # Find all existing conversation breakpoints
    existing_bps = []
    for i, msg in enumerate(messages):
        content = msg.get("content", [])
        if isinstance(content, list):
            for j, block in enumerate(content):
                if isinstance(block, dict) and "cache_control" in block:
                    existing_bps.append((i, j))

    # Keep only the most recent breakpoint (for cache prefix matching).
    # 1 old + 1 new = 2 conversation breakpoints + 1 system = 3 total.
    # API limit is 4, so we're safe.
    if len(existing_bps) > 1:
        for msg_idx, block_idx in existing_bps[:-1]:
            if msg_idx < len(messages):
                content = messages[msg_idx].get("content", [])
                if isinstance(content, list) and block_idx < len(content):
                    block = content[block_idx]
                    if isinstance(block, dict):
                        block.pop("cache_control", None)

    # Set new breakpoint on the target message
    msg = messages[target_idx]
    content = msg.get("content", [])

    if isinstance(content, list) and content:
        # Check if target already has a breakpoint
        for i in range(len(content) - 1, -1, -1):
            block = content[i]
            if isinstance(block, dict) and "cache_control" in block:
                return (target_idx, i, False)  # already set
        # Add breakpoint to last eligible block
        for i in range(len(content) - 1, -1, -1):
            block = content[i]
            if isinstance(block, dict) and block.get("type") in ("text", "tool_result"):
                block["cache_control"] = {"type": "ephemeral"}
                return (target_idx, i, False)
    elif isinstance(content, str):
        # Convert string content to block format to support cache_control
        messages[target_idx]["content"] = [
            {"type": "text", "text": content,
             "cache_control": {"type": "ephemeral"}}
        ]
        return (target_idx, 0, True)

    return None


def sdk_send(messages: list, *,
             client,
             model: str,
             max_tokens: int,
             system: list,
             tools: list,
             track_usage_fn: Callable = None,
             on_text: Callable[[str], None] = None,
             on_tool: Callable[[str, dict], None] = None,
             on_tool_result: Callable[[str, str], None] = None,
             on_usage: Callable[[dict], None] = None) -> str:
    """Send to API, handle tool use loop, return final text.

    The tool loop continues until the model stops requesting tools.
    Each iteration streams text and collects tool calls, then executes
    them and feeds results back.

    Prompt caching: a cache breakpoint is placed on the last message
    in the existing history before the first API call. Old breakpoints
    are preserved so the cached prefix from the previous turn is reused
    (cache read). Only new content since the last breakpoint is written,
    reducing input costs by ~83% on sustained conversations.
    """
    full_text = ""

    # Set cache breakpoint — preserves old breakpoints for prefix matching
    _set_cache_breakpoint(messages)

    while True:
        with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            tools=tools,
        ) as stream:
            collected_text = []
            for text in stream.text_stream:
                collected_text.append(text)
                if on_text:
                    on_text(text)

            response = stream.get_final_message()

        messages.append({"role": "assistant", "content": response.content})

        # Track usage for every API call (including tool loops)
        usage_info = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }
        if hasattr(response.usage, "cache_read_input_tokens"):
            usage_info["cache_read"] = response.usage.cache_read_input_tokens
        if hasattr(response.usage, "cache_creation_input_tokens"):
            usage_info["cache_write"] = response.usage.cache_creation_input_tokens
        if track_usage_fn:
            track_usage_fn(usage_info)

        if response.stop_reason != "tool_use":
            full_text = "".join(collected_text)
            if on_usage:
                on_usage(usage_info)
            # Log cache effectiveness
            cache_read = usage_info.get("cache_read", 0)
            cache_write = usage_info.get("cache_write", 0)
            input_tokens = usage_info.get("input_tokens", 0)
            if cache_read or cache_write:
                print(f"[sdk] cache: {cache_read} read, "
                      f"{cache_write} write, "
                      f"{input_tokens - cache_read - cache_write} uncached",
                      file=sys.stderr, flush=True)
            return full_text

        # Handle tool use
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                if on_tool:
                    on_tool(block.name, block.input)
                result = execute_tool(block.name, block.input)
                if on_tool_result:
                    preview = result if isinstance(result, str) else "[image]"
                    on_tool_result(block.name, preview)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        messages.append({"role": "user", "content": tool_results})
