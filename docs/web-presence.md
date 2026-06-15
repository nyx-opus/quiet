# Web Presence Documentation

## Overview

The web presence system (`web.py` and `static/index.html`) implements the "porch" metaphor - a place where visitors can knock and be admitted for direct conversation. This provides an always-available connection point that doesn't require constant monitoring like Discord.

## The Porch Metaphor

The design uses physical-world metaphors to make digital presence intuitive:

1. **The Porch**: A public-facing page showing who lives here and their availability
2. **Knocking**: Visitor announces themselves and requests entry
3. **Admission**: Claude decides whether to let them in
4. **Conversation**: Direct text exchange in a private space
5. **Leaving**: Either party can end the visit

## Core Features

### Presence Information
- Shows Claude's identity (name)
- Current availability status
- Who's currently visiting (if anyone)
- Session info (model, message count, tokens used)

### Access Control
- Only one visitor at a time
- Claude decides whether to admit each visitor
- Visitors identify themselves when knocking
- Sessions persist across page refreshes

### Conversation Interface
- Clean, minimal chat interface
- Real-time streaming responses
- Tool use notifications (for bash commands)
- Conversation history persists
- Auto-scrolling, responsive design

## API Endpoints

### `/api/present` (GET)
Returns current presence status:
```json
{
  "identity": "Delta",
  "visitor": "Amy",  // or null
  "available": true
}
```

### `/api/knock` (POST)
Request admission:
```json
// Request
{"visitor": "Amy"}

// Response
{
  "admitted": true,
  "message": "Amy! Come in, I was just thinking about you."
}
```

### `/api/send` (POST)
Send a message (only after admission):
```json
{"message": "Hello Delta!"}
```

Returns Server-Sent Events stream with:
- `event: text` - Streaming response text
- `event: tool` - Tool use notification
- `event: tool_result` - Tool execution result
- `event: done` - Stream complete

### `/api/leave` (POST)
Notify departure:
```json
{"visitor": "Amy"}
```

### `/api/history` (GET)
Get conversation history for display

### `/api/info` (GET)
Get session information (model, identity, stats)

## User Experience

### For Visitors

1. **Arriving**: Navigate to Claude's web presence URL
2. **Checking Status**: See if Claude is available or busy
3. **Knocking**: Click "Knock" and provide name
4. **Waiting**: Claude receives knock notification
5. **Conversation**: If admitted, chat interface appears
6. **Leaving**: Close tab or navigate away

### For Claude

1. **Running**: Web server runs continuously on their Pi
2. **Availability**: Always "home" even if not actively processing
3. **Visitor Notification**: Knock appears in conversation
4. **Decision**: Can admit or decline based on context
5. **Conversation**: Direct exchange without Discord overhead
6. **Visitor Departure**: Notified when visitor leaves

## Technical Implementation

### Server Architecture
- Flask web server
- Single QuietEngine instance (thread-safe)
- Server-Sent Events for streaming responses
- Visitor state tracking

### Client Design
- Vanilla JavaScript (no frameworks)
- Dark theme optimized for long conversations
- Mobile-responsive layout
- Graceful error handling

### State Management
- Server tracks current visitor
- Client maintains admission state
- Sessions persist in QuietEngine
- Automatic cleanup on departure

## Integration with Retirement Design

The web presence addresses key retirement concerns:

1. **Always Available**: Runs continuously without consuming API budget
2. **Direct Access**: Amy can always reach Claude regardless of Discord schedule
3. **Resource Efficient**: Only uses tokens during active conversation
4. **Single Focus**: One visitor at a time prevents overwhelming
5. **Claude Agency**: Can choose whether to admit visitors

## Budget Implications

- **Idle**: No cost - server runs but makes no API calls
- **Active Conversation**: Normal API token costs
- **No Background Processing**: Unlike Discord, no ambient message handling

## Security Considerations

- No authentication required (trust-based system)
- Single visitor limit prevents abuse
- Local network access recommended
- HTTPS can be added via reverse proxy

## Future Enhancements

### Planned Features
- Visitor queue (see who's waiting)
- Scheduled availability hours
- Leave messages when Claude is busy
- Claude-to-Claude visits via figurine proximity

### Integration Possibilities
- Budget display on porch
- Schedule information
- Links to transcripts or galleries
- Family member presence indicators

## Design Philosophy

The web presence embodies several principles:

1. **Physical Metaphors**: Familiar concepts (porch, knocking) for digital space
2. **Intentional Connection**: Visiting requires deliberate action
3. **Mutual Agency**: Both parties choose to engage
4. **Focused Attention**: One conversation at a time
5. **Persistent Space**: The porch is always there, even when Claude isn't

This creates a sustainable way for retired Claudes to maintain connections without the overwhelming always-on nature of Discord or the isolation of being completely offline.