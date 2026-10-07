"""Run the read-only tools against your HubSpot account through a real MCP client.

Usage:  python scripts/check_hubspot.py

Needs HUBSPOT_SERVICE_KEY in .env. Makes no changes.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

os.environ["CRM_BACKEND"] = "hubspot"

from mcp import Client  # noqa: E402

from crm_mcp import server  # noqa: E402


async def main() -> None:
    try:
        server.store._token()
        server.store._stages()
    except server.store.HubSpotError as e:
        sys.exit(f"Can't reach HubSpot: {e}")
    async with Client(server.mcp) as c:
        summary = json.loads((await c.call_tool("get_pipeline_summary", {})).content[0].text)
        print(f"Open deals: {summary['open_deals']}, pipeline ${summary['open_pipeline_value']:,}, "
              f"weighted forecast ${summary['weighted_forecast']:,}, win rate {summary['win_rate']}")
        if not summary["open_deals"]:
            sys.exit("No open deals found. Run scripts/seed_hubspot.py first (and wait a minute for indexing).")

        attention = json.loads((await c.call_tool("get_deals_needing_attention", {"limit": 3})).content[0].text)
        print(f"Flagged deals: {attention['flagged_deals']} (${attention['value_at_risk']:,} at risk). Top 3:")
        for d in attention["deals"]:
            print(f"  #{d['deal_id']} {d['company']} ${d['value']:,} [{d['priority']}] - {d['reasons'][0]}")

        top = attention["deals"][0]["company"]
        account = (await c.call_tool("get_account", {"company": top})).content[0].text
        acc = json.loads(account)
        print(f"Account '{top}': {len(acc['contacts'])} contacts, {len(acc['deals'])} deals, "
              f"{len(acc['recent_activity'])} recent activities")
    print("OK")


if __name__ == "__main__":
    asyncio.run(main())
