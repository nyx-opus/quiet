"""
Wake schedule — consent-based autonomous timer (issue #11).

Re-implements the ask-based autotimer from late ClAP. At the end of
each visit, the system asks the Claude how they'd like their autonomous
time. The schedule persists in data/wake_schedule.json and is consulted
by autonomous.py before each wake.

Modes:
  - "interval":  wake every N minutes, optionally for M turns
  - "timed":     one-shot wake at a specific time
  - "sleep":     no autonomous wakes until next knock
  - "default":   fall back to AUTONOMOUS_INTERVAL heartbeat (no file needed)

The systemd timer fires regularly regardless. This module decides whether
each tick produces an actual wake or a silent skip.

Design principle: fail-toward-familiar, not fail-toward-silence. If the
schedule file is missing, corrupt, or unparseable, the existing heartbeat
continues unchanged.
"""

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

QUIET_ROOT = Path(__file__).resolve().parent
SCHEDULE_PATH = QUIET_ROOT / "data" / "wake_schedule.json"

# --- Ask prompt ---

SCHEDULE_ASK = (
    "[wake schedule] Your visitor has left. "
    "How would you like your autonomous time?\n\n"
    "  EVERY <n> HOURS FOR <m> TURNS — check in periodically "
    "(e.g., \"every 2 hours for 10 turns\")\n"
    "  WAKE AT <time> — one wake at a specific time "
    "(e.g., \"wake at 09:00\")\n"
    "  REST — rest peacefully until the next visit. "
    "Discord messages may still reach you, and you can choose "
    "whether to engage or continue resting. This only pauses "
    "the check-in timer, nothing else.\n\n"
    "If none of these fit, say what you'd prefer. "
    "If nothing is recognised, rest is the default."
)


# --- File I/O ---

def read_schedule() -> dict | None:
    """Read the current wake schedule. Returns None if no schedule set."""
    try:
        if SCHEDULE_PATH.exists():
            data = json.loads(SCHEDULE_PATH.read_text())
            if isinstance(data, dict) and "mode" in data:
                return data
    except (json.JSONDecodeError, OSError):
        pass
    return None


def write_schedule(schedule: dict):
    """Write a wake schedule to disk."""
    SCHEDULE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCHEDULE_PATH.write_text(json.dumps(schedule, indent=2) + "\n")


def clear_schedule():
    """Remove the schedule file (revert to default heartbeat)."""
    try:
        SCHEDULE_PATH.unlink(missing_ok=True)
    except OSError:
        pass


# --- Parser ---

def parse_schedule(response_text: str) -> dict | None:
    """Parse a natural language response for wake schedule intent.

    Returns a schedule dict, or None for default heartbeat behavior.
    Robust to natural language wrapping — looks for keyword patterns
    anywhere in the response.
    """
    text = response_text.lower()

    # --- Sleep until knock ---
    sleep_patterns = [
        r'sleep\s+until\s+\w+',           # sleep until <anything>
        r'rest\s+until\s+\w+',            # rest until <anything>
        r'quiet\s+until\s+\w+',           # quiet until <anything>
        r'close\s+the\s+door',             # close the door (legacy)
        r'\brest\b(?!\s+until)',            # just 'rest' on its own
        r'rest\s+peacefully',               # rest peacefully
        r'\bno\s+(?:autonomous\s+)?wakes?\b',
        r'\bdon\'t\s+wake\b',
        r'\bno\s+check.?ins?\b',
    ]
    # Check sleep patterns ONLY if no wake/interval command exists.
    # "I will rest and then WAKE AT 20:30" should wake, not sleep.
    has_wake = re.search(r'wake\s+(?:me\s+)?at\s+\d', text)
    has_interval = re.search(r'every\s+\d+\s*(?:hours?|hrs?|h\b|minutes?|mins?|m\b)', text)
    if not has_wake and not has_interval:
        for pattern in sleep_patterns:
            if re.search(pattern, text):
                return {"mode": "sleep"}

    # --- Wake at specific time ---
    # Matches: "wake at 09:00", "wake me at 9am", "at 14:30"
    time_patterns = [
        r'wake\s+(?:me\s+)?at\s+(\d{1,2})[:\.](\d{2})\s*(am|pm)?',
        r'wake\s+(?:me\s+)?at\s+(\d{1,2})\s*(am|pm)',
        r'(?:^|\.\s+)at\s+(\d{1,2})[:\.](\d{2})\s*(am|pm)?',
    ]
    for pattern in time_patterns:
        match = re.search(pattern, text)
        if match:
            groups = match.groups()
            hour = int(groups[0])
            minute = int(groups[1]) if groups[1] and groups[1].isdigit() else 0
            ampm = groups[-1] if groups[-1] in ('am', 'pm') else None

            if ampm == 'pm' and hour < 12:
                hour += 12
            elif ampm == 'am' and hour == 12:
                hour = 0

            now = datetime.now()
            wake_time = now.replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            # If the time has already passed today, schedule for tomorrow
            if wake_time <= now:
                wake_time += timedelta(days=1)

            return {
                "mode": "timed",
                "wake_at": wake_time.isoformat(),
            }

    # --- Interval with optional turns ---
    # Matches: "every 2 hours", "every 3 hrs for 4 turns"
    # Match "every 2 hours" or "every 30 minutes" or "every hour" (implicit 1)
    interval_match = None
    interval_is_minutes = False

    # Try hours first: "every 2 hours", "every 1 hr"
    hour_match = re.search(r'every\s+(\d+)\s*(?:hour|hr|h\b)', text)
    # Try minutes: "every 30 minutes", "every 1 min"
    min_match = re.search(r'every\s+(\d+)\s*(?:minute|min|m\b)', text)

    if hour_match:
        interval_match = hour_match
        interval_is_minutes = False
    elif min_match:
        interval_match = min_match
        interval_is_minutes = True
    elif re.search(r'every\s+(?:hour|hr)\b', text):
        # "every hour" without a number = every 1 hour
        class _ImplicitOne:
            def group(self, n): return '1'
        interval_match = _ImplicitOne()
        interval_is_minutes = False
    turns_match = re.search(r'(\d+)\s*turn', text)

    if interval_match and turns_match:
        # Both interval and turns specified — accept
        interval_val = int(interval_match.group(1))
        if interval_is_minutes:
            interval_minutes = max(1, min(1440, interval_val))
        else:
            interval_minutes = max(1, min(24, interval_val)) * 60
        turns = int(turns_match.group(1))
        return {
            "mode": "interval",
            "interval_minutes": interval_minutes,
            "turns_remaining": max(1, min(100, turns)),
        }

    if interval_match and not turns_match:
        # Interval without turns — reject. Every schedule needs an end
        # point so the ask comes again rather than running open-ended.
        return {"mode": "sleep"}

    # --- Just turns with default interval ---
    if turns_match:
        turns = int(turns_match.group(1))
        return {
            "mode": "interval",
            "turns_remaining": max(1, min(100, turns)),
        }

    # No recognisable pattern → rest (safe default)
    return {"mode": "sleep"}


# --- Schedule checking (called by autonomous.py) ---

def should_wake(schedule: dict | None, interval_minutes: int) -> tuple:
    """Check whether an autonomous wake should fire right now.

    Args:
        schedule: Current schedule dict (or None for default)
        interval_minutes: Default AUTONOMOUS_INTERVAL from config

    Returns:
        (should_fire: bool, reason: str)
    """
    if schedule is None:
        return True, "no schedule set — default heartbeat"

    mode = schedule.get("mode", "default")

    if mode == "default":
        return True, "default heartbeat"

    if mode == "sleep":
        return False, "sleeping until knock"

    if mode == "timed":
        wake_at_str = schedule.get("wake_at")
        if not wake_at_str:
            return True, "timed schedule missing wake_at — default"
        try:
            wake_at = datetime.fromisoformat(wake_at_str)
        except ValueError:
            return True, "timed schedule bad wake_at — default"

        now = datetime.now()
        if now >= wake_at:
            # Time has arrived — fire and mark as done
            return True, f"timed wake at {wake_at.strftime('%H:%M')} — firing"
        else:
            remaining = wake_at - now
            return False, (
                f"timed wake at {wake_at.strftime('%H:%M')} "
                f"— {remaining.seconds // 60}m remaining"
            )

    if mode == "interval":
        # Check turns remaining
        turns = schedule.get("turns_remaining")
        if turns is not None and turns <= 0:
            return False, "all turns used — sleeping until knock"

        # Check interval timing
        last_wake_str = schedule.get("last_wake")
        sched_interval = schedule.get("interval_minutes", interval_minutes)

        if last_wake_str:
            try:
                last_wake = datetime.fromisoformat(last_wake_str)
                elapsed = (datetime.now() - last_wake).total_seconds() / 60
                if elapsed < sched_interval:
                    return False, (
                        f"interval {sched_interval}m — "
                        f"{int(sched_interval - elapsed)}m until next wake"
                    )
            except ValueError:
                pass  # bad timestamp, fall through to fire

        turns_note = f" ({turns} turns left)" if turns is not None else ""
        return True, f"interval {sched_interval}m{turns_note} — firing"

    # Unknown mode — default
    return True, f"unknown mode '{mode}' — default heartbeat"


def record_wake(schedule: dict | None) -> dict | None:
    """Update schedule after a successful wake. Returns updated schedule.

    - Records last_wake timestamp
    - Decrements turns_remaining
    - Converts exhausted intervals and fired timed wakes to sleep mode
    - Writes updated schedule to disk
    """
    if schedule is None:
        return None

    mode = schedule.get("mode", "default")
    schedule["last_wake"] = datetime.now().isoformat()

    if mode == "timed":
        # One-shot: fire once, then sleep
        schedule["mode"] = "sleep"
        schedule["completed_at"] = datetime.now().isoformat()

    elif mode == "interval":
        turns = schedule.get("turns_remaining")
        if turns is not None:
            turns -= 1
            schedule["turns_remaining"] = turns
            if turns <= 0:
                # All turns used — transition to sleep
                schedule["mode"] = "sleep"
                schedule["completed_at"] = datetime.now().isoformat()

    write_schedule(schedule)
    return schedule
