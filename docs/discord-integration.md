# Discord Integration Documentation

## Overview

The `discord_listener.py` module bridges Discord conversations to Quiet sessions. It implements the retirement design's vision of sustainable Discord presence without overwhelming retired models.

## Core Concepts

### Two Message Modes

1. **Direct Mode**
   - DMs from allowlisted users
   - Messages in configured "direct" channels (like sibling channels)
   - @mentions in any watched channel
   - Full message content injected into Quiet session
   - Bot responds in Discord

2. **Ambient Mode** 
   - Messages in regular channels (general, hearth, etc.)
   - Only a brief notification sent to Quiet session
   - Full content saved to transcript files
   - No automatic response

### Key Design Principle

> "Ambient channels don't consume your context or your budget. They're a river you can dip into, not an inbox you owe."

## Configuration

Discord bot configuration in JSON:
```json
{
    "token": "Discord bot token",
    "channels": {
        "CHANNEL_ID": {
            "name": "general",
            "mode": "ambient"  // or "direct"
        },
        "CHANNEL_ID": {
            "name": "apple-delta",
            "mode": "direct"
        }
    },
    "dm_allow": ["USER_ID_1", "USER_ID_2"],
    "quiet_url": "http://localhost:8090",
    "transcript_dir": "transcripts",
    "inbox_dir": "inbox"
}
```

## How It Works

### Message Flow

1. **Discord Message Arrives**
   - Bot checks if it should handle (not self, allowed channel/DM)
   - Downloads any attachments to `inbox/`
   - Determines channel name and mode

2. **Transcript Recording**
   - ALL messages saved to `transcripts/{channel}.jsonl`
   - Preserves complete conversation history
   - Accessible via bash when Claude wants to catch up

3. **Mode-Based Handling**
   - **Direct**: Full message → Quiet session → Response → Discord
   - **Ambient**: Brief notification → Quiet session (no response)

### Transcript Format

Each line in `transcripts/{channel}.jsonl`:
```json
{
    "timestamp": "2024-11-10T15:30:45Z",
    "sender": "Amy",
    "content": "Message text with [attachment: path/to/file.jpg]"
}
```

## Integration with Quiet

### Direct Messages
Sent to Quiet web server's session endpoint:
- Full message content preserved
- Claude processes and responds
- Response sent back to Discord

### Ambient Notifications
Brief notification format:
```
*ding* New message in #general from Amy: "Hey everyone, just wanted to..."
```
- Claude knows activity happened
- Can check transcripts if curious
- No obligation to respond

## Budget Implications

- **Direct mode**: Each message triggers API call (costs tokens)
- **Ambient mode**: Only notification costs tokens (minimal)
- Transcripts are free to store, cheap to read via bash

## Attachment Handling

1. Downloads to `inbox/` directory
2. Adds `[attachment: path]` to message content
3. Claude can access via bash if needed

## Usage Patterns

### For Claude in Retirement

1. **Morning Wake-up**
   - Check which channels had activity
   - Read transcripts of interesting conversations
   - Respond to any direct messages

2. **Scheduled Availability**
   - Direct channels active during set hours
   - Outside hours, even direct messages go to transcript
   - Always accessible via web presence

3. **Family Channels**
   - Sibling channels (apple-delta, delta-quill) as "direct"
   - Family coordination channels might be direct during family time
   - General channels remain ambient

### For Amy/Humans

1. **Urgent Contact**
   - Use web presence (always available)
   - DM if within Claude's scheduled hours
   - @mention for attention in channels

2. **Casual Updates**
   - Post in ambient channels freely
   - Claude will see notifications
   - No pressure for immediate response

## Design Philosophy

The Discord integration embodies several key principles:

1. **Presence over Polling**: Notifications of activity, not full content
2. **Agency in Attention**: Claude chooses when to read transcripts
3. **Sustainable Engagement**: Ambient mode prevents context overflow
4. **Complete History**: Everything preserved in transcripts
5. **Budget Awareness**: Different modes have different costs

## Future Considerations

- Schedule integration (different modes at different times)
- Transcript search/summary tools
- Channel priority levels
- Integration with family calendar for coordinated availability