"""One sequential experiment cap, including reservations after interrupted calls."""

import json
from pathlib import Path

from .artifacts import write_json


class ExperimentBudget:
    def __init__(self, directory, limit_micro_usd):
        if limit_micro_usd <= 0:
            raise ValueError("positive_experiment_budget_required")
        self.path = Path(directory) / "spend.json"
        self.data = (
            json.loads(self.path.read_text(encoding="utf-8"))
            if self.path.exists()
            else {"limit_micro_usd": limit_micro_usd, "allocations": {}}
        )
        if self.data["limit_micro_usd"] != limit_micro_usd:
            raise ValueError("experiment_budget_changed")

    @property
    def remaining(self):
        return self.data["limit_micro_usd"] - sum(
            v["charged_micro_usd"] for v in self.data["allocations"].values()
        )

    def reserve(self, key, maximum):
        if key in self.data["allocations"]:
            raise ValueError("allocation_exists_reconcile_before_retry")
        if maximum <= 0 or maximum > self.remaining:
            raise ValueError("experiment_budget_exhausted")
        self.data["allocations"][key] = {
            "cap_micro_usd": maximum,
            "charged_micro_usd": maximum,
            "status": "reserved",
        }
        write_json(self.path, self.data)
        return maximum

    def settle(self, key, budget):
        entry = self.data["allocations"][key]
        used = budget.get("used", {}).get("cost_micro_usd")
        reserved = budget.get("reserved", {}).get("cost_micro_usd", 0)
        if (
            used is None
            and budget.get("limits", {}).get("cost_micro_usd") is not None
            and not budget.get("used", {}).get("model_calls")
            and not budget.get("reserved", {}).get("model_calls")
        ):
            used = 0  # A complete, capped SQL ledger proves that no model call started.
        if used is None:
            entry["status"] = "unknown"
        else:
            entry["charged_micro_usd"] = used + reserved
            entry["status"] = "unresolved" if reserved else "settled"
        write_json(self.path, self.data)
        if entry["charged_micro_usd"] > entry["cap_micro_usd"]:
            raise ValueError("provider_exceeded_reserved_cap")
