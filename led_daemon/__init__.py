"""LED daemon — drives LEDs based on state written by the conversation engine.

The daemon is a pure consumer: it polls a state file and updates LEDs.
It never detects or writes state. That's the engine's responsibility.

Architecture:
    Engine (Quiet) --> writes --> claude_state.json <-- reads <-- LED daemon --> drives --> LEDs

State file location configured via:
    1. CLAUDE_STATE_PATH environment variable
    2. state_file in led_config.json
    3. Default: ~/quiet/data/claude_state.json
"""
