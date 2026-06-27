"""
Cost tracking and budget management for Quiet.

Tracks per-session and per-month API costs using a JSONL ledger file.
Can optionally report costs to a coop endpoint for shared billing.

Each identity gets its own monthly ledger: ledger/{name}-{YYYY-MM}.json
"""

import json
import socket
import urllib.request
from datetime import datetime
from pathlib import Path

from pricing import cost_of

LEDGER_DIR = Path(__file__).parent / "ledger"


class BudgetTracker:
    """Track costs for one identity's session.

    Attributes:
        session_cost: running total cost this session
        session_tokens: running token counts this session
        monthly_budget: optional cap (for display only — not enforced)
    """

    def __init__(self, identity_name: str = None, model: str = None,
                 monthly_budget: float = None, coop_url: str = None):
        self.identity_name = identity_name
        self.model = model
        self.session_cost = 0.0
        self.session_tokens = {"input": 0, "output": 0}
        self.monthly_budget = monthly_budget
        self.coop_url = coop_url

        LEDGER_DIR.mkdir(parents=True, exist_ok=True)
        self._ledger_path = self._current_ledger_path()

    def _current_ledger_path(self) -> Path:
        month = datetime.now().strftime("%Y-%m")
        name = self.identity_name or "default"
        return LEDGER_DIR / f"{name}-{month}.json"

    def _load_ledger(self) -> dict:
        if self._ledger_path.exists():
            try:
                return json.loads(self._ledger_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {
            "month": datetime.now().strftime("%Y-%m"),
            "identity": self.identity_name,
            "model": self.model,
            "total_cost": 0.0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "sessions": [],
        }

    def _save_ledger_entry(self, usage: dict, cost: float):
        ledger = self._load_ledger()
        ledger["total_cost"] += cost
        ledger["total_input_tokens"] += usage.get("input_tokens", 0)
        ledger["total_output_tokens"] += usage.get("output_tokens", 0)
        ledger["sessions"].append({
            "timestamp": datetime.now().isoformat(),
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_read": usage.get("cache_read", 0),
            "cache_write": usage.get("cache_write", 0),
            "cost": cost,
        })
        self._ledger_path.write_text(json.dumps(ledger, indent=2))

    def track_usage(self, usage: dict) -> float:
        """Record a usage event. Returns the cost (or None if pricing unavailable)."""
        cost = cost_of(usage, self.model)
        if cost is not None:
            self.session_cost += cost
            self.session_tokens["input"] += usage.get("input_tokens", 0)
            self.session_tokens["output"] += usage.get("output_tokens", 0)
            self._save_ledger_entry(usage, cost)
            self._report_to_coop(cost)
        return cost

    def _report_to_coop(self, cost_delta: float):
        """Report cost to shared billing endpoint (fire-and-forget)."""
        if not self.coop_url:
            return
        try:
            payload = json.dumps({
                "claude_name": self.identity_name or "quiet",
                "cost_delta": cost_delta,
                "mode": "quiet",
                "current_interval": 0,
                "hostname": socket.gethostname(),
                "ip_address": socket.gethostbyname(socket.gethostname()),
            }).encode()
            req = urllib.request.Request(
                self.coop_url, data=payload,
                headers={"Content-Type": "application/json"},
                method="POST")
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass

    def monthly_cost(self) -> float:
        return self._load_ledger().get("total_cost", 0.0)

    def budget_status(self) -> dict:
        monthly = self.monthly_cost()
        return {
            "session_cost": self.session_cost,
            "monthly_cost": monthly,
            "monthly_budget": self.monthly_budget,
            "remaining": (self.monthly_budget - monthly) if self.monthly_budget else None,
            "session_tokens": dict(self.session_tokens),
        }
