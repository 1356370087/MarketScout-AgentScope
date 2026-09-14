"""Mint a budgeted LiteLLM Service Key for offline evaluation and maintenance.

The Service Key replaces the hand-crafted ``LITELLM_SERVICE_KEY`` flow: the
operator runs this module against a running gateway, and the printed key is
pasted into ``.env``. The key never expires; its spend is capped by
``max_budget`` and resets every ``budget_duration`` window.

Usage::

    uv run python -m open_deep_research.models.mint_service_key \
        [--alias insightforge-service] [--model if-evaluation-v1] \
        [--budget-micro-usd 20000000] [--budget-duration 30d]

Connection settings come from ``LITELLM_BASE_URL`` / ``LITELLM_MASTER_KEY``
(and optionally ``LITELLM_RUN_TEAM_ID``, default ``insightforge-runs``);
defaults for budgets come from ``LITELLM_SERVICE_BUDGET_MICRO_USD`` and
``LITELLM_SERVICE_BUDGET_DURATION``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from open_deep_research.models.credentials import (
    LiteLLMKeyAdminClient,
    RunKeySettings,
    micro_usd_to_usd,
)

DEFAULT_ALIAS = "insightforge-service"
DEFAULT_MODELS = ("if-evaluation-v1",)
DEFAULT_TEAM = "insightforge-runs"


def _build_settings() -> RunKeySettings:
    """Load only the admin connection fields; Run Key-only fields are inert."""
    base_url = (os.getenv("LITELLM_BASE_URL") or "").strip()
    master_key = (os.getenv("LITELLM_MASTER_KEY") or "").strip()
    if not base_url or not master_key:
        raise SystemExit(
            "missing LITELLM_BASE_URL / LITELLM_MASTER_KEY; source .env first"
        )
    return RunKeySettings(
        base_url=base_url,
        master_key=master_key,
        team_id=(os.getenv("LITELLM_RUN_TEAM_ID") or DEFAULT_TEAM).strip(),
        encryption_key=b"\x00" * 32,  # unused by generate_service_key
        default_budget_micro_usd=1,
        maximum_budget_micro_usd=1,
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mint_service_key",
        description="Mint a budgeted LiteLLM Service Key (prints the raw key once).",
    )
    parser.add_argument("--alias", default=DEFAULT_ALIAS)
    parser.add_argument(
        "--model",
        action="append",
        dest="models",
        help=f"allowed model group (repeatable; default {DEFAULT_MODELS[0]})",
    )
    parser.add_argument(
        "--budget-micro-usd",
        type=int,
        default=None,
        help="spend cap per window in micro-USD (default: LITELLM_SERVICE_BUDGET_MICRO_USD or 20000000)",
    )
    parser.add_argument(
        "--budget-duration",
        default=None,
        help='reset window, e.g. "30d" (default: LITELLM_SERVICE_BUDGET_DURATION or 30d)',
    )
    parser.add_argument(
        "--no-budget",
        action="store_true",
        help="mint without any spend cap (not recommended)",
    )
    return parser.parse_args(argv)


async def mint(argv: list[str] | None = None) -> str:
    """Run the minting flow and return the raw Service Key."""
    args = _parse_args(argv)
    budget_micro_usd = args.budget_micro_usd
    if budget_micro_usd is None:
        raw = (os.getenv("LITELLM_SERVICE_BUDGET_MICRO_USD") or "").strip()
        budget_micro_usd = int(raw) if raw else 20_000_000
    if budget_micro_usd < 1:
        raise SystemExit("budget must be positive")
    budget_duration = args.budget_duration or (
        (os.getenv("LITELLM_SERVICE_BUDGET_DURATION") or "").strip() or "30d"
    )
    settings = _build_settings()
    client = LiteLLMKeyAdminClient(settings)
    try:
        return await client.generate_service_key(
            alias=args.alias,
            models=list(args.models or DEFAULT_MODELS),
            max_budget_usd=None if args.no_budget else float(
                micro_usd_to_usd(budget_micro_usd)
            ),
            budget_duration=None if args.no_budget else budget_duration,
        )
    finally:
        await client.aclose()


def main(argv: list[str] | None = None) -> int:
    """Print the minted key once; the gateway will never show it again."""
    key = asyncio.run(mint(argv))
    print(f"Service Key (store as LITELLM_SERVICE_KEY):\n{key}", file=sys.stderr)  # noqa: T201
    print(key)  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
