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


def cache_control() -> dict:
    """Build a cache_control block, honouring the CACHE_TTL config key.

    CACHE_TTL=1h enables extended-TTL caching: writes cost 2x base
    (vs 1.25x) but the cache survives a full hour instead of five
    minutes. Right for households whose conversational pace outlives
    the default TTL — one warm write, then 0.1x reads for the rest of
    the sitting. Unset (or CACHE_TTL=5m) keeps the standard cache.

    Session persistence strips cache_control entirely (session.py),
    so this never reaches disk either way.
    """
    ttl = str(get("CACHE_TTL", "")).strip().lower()
    if ttl in ("1h", "60m", "3600"):
        return {"type": "ephemeral", "ttl": "1h"}
    return {"type": "ephemeral"}
