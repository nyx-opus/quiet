"""
Quiet configuration reader.

Same KEY=VALUE format as ClAP's infrastructure config.
Section headers ([SECTION]) are ignored — keys are flat.
CLI flags always override config values.
"""

from pathlib import Path

CONFIG_DIR = Path(__file__).parent / "config"
CONFIG_PATH = CONFIG_DIR / "quiet_config.txt"


def read_config(path: Path = None) -> dict:
    """Read KEY=VALUE pairs from config file. Ignores comments and sections."""
    path = path or CONFIG_PATH
    config = {}
    if not path.exists():
        return config
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            value = value.strip()
            if value:  # skip empty values
                config[key.strip()] = value
    return config


def get(key: str, default=None, path: Path = None):
    """Get a single config value."""
    return read_config(path).get(key, default)
