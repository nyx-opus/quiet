"""
Anthropic SDK backend for Quiet.

Direct API calls via the Anthropic Python SDK. Supports streaming,
tool use loops, and proper message-array formatting (no role confusion).

This is the cleaner backend — proper structured messages, no text
formatting, no fabrication risk. Used for API key or OpenRouter auth.
The trade-off is cost: API billing vs subscription.
"""

from typing import Callable
from tools import execute_tool


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
    """
    full_text = ""

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
