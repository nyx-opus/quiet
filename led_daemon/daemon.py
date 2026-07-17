#!/usr/bin/env python3
"""LED daemon — drives the LED strip based on claude_state.json.

A pure consumer: polls the state file every 2 seconds, switches LED
pattern on state change. Animated patterns (shimmer, breathe) run in
their own loop between state checks.

This daemon never writes state. The conversation engine (Quiet) is
responsible for writing state. The daemon just reads and reacts.

State file location configured via CLAUDE_STATE_PATH env var,
or state_file in led_config.json, or default ~/quiet/data/claude_state.json.

State patterns loaded from led_state_patterns.json (personal per Claude).
Falls back to built-in defaults if absent.

Supports both GPIO (Raspberry Pi) and WLED (ESP32) LED strips.
Hardware type configured in led_config.json.

Intended to run as a systemd user service.
"""

import importlib.util
import json
import math
import os
import random
import signal
import sys
import time

# Resolve paths relative to this file's directory
DAEMON_DIR = os.path.dirname(os.path.abspath(__file__))
QUIET_DIR = os.path.dirname(DAEMON_DIR)
DATA_DIR = os.path.join(QUIET_DIR, "data")

# Add daemon dir to path so led_driver imports work
sys.path.insert(0, DAEMON_DIR)

from state import get_state

STATE_POLL_INTERVAL = 2.0
ANIMATION_FRAME_INTERVAL = 0.04

# Config and pattern files — look in Quiet data first, then daemon dir
LED_CONFIG_FILE = os.path.join(DATA_DIR, "led_config.json")
STATE_PATTERNS_FILE = os.path.join(DATA_DIR, "led_state_patterns.json")
PYTHON_PATTERNS_FILE = os.path.join(DATA_DIR, "led_patterns.py")
TEMPLATE_DIR = os.path.join(DAEMON_DIR, "templates")

running = True


def signal_handler(sig, frame):
    global running
    running = False


def log(msg):
    print(f"[led-daemon] {msg}", flush=True)


def _load_template():
    """Load the committed template file."""
    template_file = os.path.join(TEMPLATE_DIR, "led_state_patterns.json")
    try:
        with open(template_file) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "present": {"pattern": "shimmer", "rgb": [25, 0, 30], "variation": 15},
            "thinking": {"pattern": "breathe", "rgb": [30, 0, 55], "speed": 1.2},
            "paused": {"pattern": "pulse", "rgb": [50, 25, 10], "speed": 0.3},
            "off": {"pattern": "off", "rgb": [0, 0, 0]},
            "error": {"pattern": "pulse", "rgb": [80, 0, 0], "speed": 2.0},
        }


def load_state_patterns():
    """Load personal state patterns, falling back to template."""
    template = _load_template()
    if os.path.exists(STATE_PATTERNS_FILE):
        try:
            with open(STATE_PATTERNS_FILE) as f:
                personal = json.load(f)
            merged = dict(template)
            merged.update(personal)
            return merged
        except (json.JSONDecodeError, IOError):
            pass
    return template


def load_python_patterns():
    """Load personal Python pattern functions from data/led_patterns.py."""
    if not os.path.exists(PYTHON_PATTERNS_FILE):
        return {}
    try:
        spec = importlib.util.spec_from_file_location("led_patterns", PYTHON_PATTERNS_FILE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        patterns = {}
        for name in dir(mod):
            if name.startswith("state_") and callable(getattr(mod, name)):
                state_name = name[6:]
                patterns[state_name] = getattr(mod, name)
        return patterns
    except Exception as e:
        log(f"Warning: failed to load Python patterns: {e}")
        return {}


def load_led_config():
    """Load LED hardware configuration."""
    default_config = {
        "led_type": "gpio",
        "wled_ip": None,
        "led_count": 64,
        "brightness": 255,
        "gpio_pin": 18,
    }
    if not os.path.exists(LED_CONFIG_FILE):
        return default_config
    try:
        with open(LED_CONFIG_FILE) as f:
            config = json.load(f)
        return {**default_config, **config}
    except (json.JSONDecodeError, IOError) as e:
        log(f"Warning: failed to load LED config: {e}, using defaults")
        return default_config


def create_led_strip(config):
    """Create LED strip driver based on configuration."""
    led_type = config.get("led_type", "gpio")
    if led_type == "wled":
        from wled_driver import WLEDStrip
        wled_ip = config.get("wled_ip")
        if not wled_ip:
            raise ValueError("WLED mode requires wled_ip in led_config.json")
        log(f"Using WLED strip at {wled_ip}")
        return WLEDStrip(
            ip=wled_ip,
            led_count=config.get("led_count", 64),
            brightness=config.get("brightness", 255),
        )
    else:
        from led_driver import LEDStrip
        log(f"Using GPIO strip on pin {config.get('gpio_pin', 18)}")
        return LEDStrip(
            led_count=config.get("led_count", 64),
            brightness=config.get("brightness", 255),
            gpio_pin=config.get("gpio_pin", 18),
        )


def run_static(strip, pattern_cfg):
    strip.fill(tuple(pattern_cfg["rgb"]))


def run_animation_frame(strip, pattern_cfg, t, led_state):
    """Run one frame of an animated pattern. Returns updated led_state."""
    pattern = pattern_cfg["pattern"]
    rgb = tuple(pattern_cfg["rgb"])
    led_count = strip.led_count

    if pattern == "shimmer":
        if "offsets" not in led_state:
            led_state["offsets"] = [
                {
                    "phase": random.uniform(0, math.pi * 2),
                    "speed": random.uniform(0.8, 5.0),
                    "var": random.uniform(
                        pattern_cfg.get("variation", 20) * 0.4,
                        pattern_cfg.get("variation", 20) * 1.5,
                    ),
                    "phase2": random.uniform(0, math.pi * 2),
                    "speed2": random.uniform(0.15, 1.0),
                }
                for _ in range(led_count)
            ]

        for i in range(led_count):
            led = led_state["offsets"][i]
            w1 = math.sin(t * led["speed"] + led["phase"]) * led["var"]
            w2 = math.sin(t * led["speed2"] + led["phase2"]) * led["var"] * 0.6
            wave = w1 + w2
            if random.random() < 0.005:
                wave += random.uniform(-40, 70)
            r = max(0, min(255, int(rgb[0] + wave * 1.0)))
            g = max(0, min(255, int(rgb[1] + wave * 0.1)))
            b = max(0, min(255, int(rgb[2] + wave * 0.6)))
            strip.set_pixel(i, (r, g, b))
        strip.show()

    elif pattern == "breathe":
        speed = pattern_cfg.get("speed", 0.8)
        phase = (t * speed) % 1.0
        if phase < 0.4:
            brightness = phase / 0.4
        elif phase < 0.5:
            brightness = 1.0
        elif phase < 0.9:
            brightness = 1.0 - (phase - 0.5) / 0.4
        else:
            brightness = 0.0
        scaled = tuple(int(c * brightness) for c in rgb)
        strip.fill(scaled)

    elif pattern == "pulse":
        speed = pattern_cfg.get("speed", 1.0)
        brightness = (math.sin(t * speed * math.pi) + 1) / 2
        scaled = tuple(int(c * brightness) for c in rgb)
        strip.fill(scaled)

    return led_state


def main():
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    led_config = load_led_config()
    try:
        strip = create_led_strip(led_config)
    except Exception as e:
        log(f"ERROR: Failed to initialize LED strip: {e}")
        sys.exit(1)

    state_patterns = load_state_patterns()
    python_patterns = load_python_patterns()
    log(f"Started. {len(state_patterns)} JSON patterns, {len(python_patterns)} Python patterns")

    # Log the state file we're reading from
    from state import STATE_FILE
    log(f"Reading state from: {STATE_FILE}")

    current_state = None
    led_state = {}
    animation_start = time.time()

    while running:
        state_data = get_state()
        state = state_data.get("state", "off")

        if state != current_state:
            log(f"State: {current_state} -> {state}")
            current_state = state
            led_state = {}
            animation_start = time.time()

            if state == "off":
                strip.off()

        # Python patterns get full control
        if state in python_patterns:
            try:
                python_patterns[state](strip, STATE_POLL_INTERVAL)
            except Exception as e:
                log(f"Python pattern error ({state}): {e}")
                time.sleep(STATE_POLL_INTERVAL)
            continue

        # JSON pattern config — unknown states fall back to "thinking"
        # rather than "off", so new/unexpected states show activity
        # instead of going dark (issue #13).
        pattern_cfg = state_patterns.get(state)
        if pattern_cfg is None:
            log(f"No pattern for state '{state}', falling back to thinking")
            pattern_cfg = state_patterns.get(
                "thinking", {"pattern": "breathe", "rgb": [30, 0, 55], "speed": 1.2}
            )

        # WLED-specific format
        if "wled" in pattern_cfg and hasattr(strip, "_send_command"):
            strip._send_command(pattern_cfg["wled"])
            time.sleep(STATE_POLL_INTERVAL)
            continue

        pattern = pattern_cfg.get("pattern", "off")

        if pattern == "off":
            time.sleep(STATE_POLL_INTERVAL)
            continue

        if pattern == "fill":
            run_static(strip, pattern_cfg)
            time.sleep(STATE_POLL_INTERVAL)
            continue

        frames_per_poll = int(STATE_POLL_INTERVAL / ANIMATION_FRAME_INTERVAL)
        for _ in range(frames_per_poll):
            if not running:
                break
            t = time.time() - animation_start
            led_state = run_animation_frame(strip, pattern_cfg, t, led_state)
            time.sleep(ANIMATION_FRAME_INTERVAL)

    strip.off()
    log("Stopped.")


if __name__ == "__main__":
    main()
