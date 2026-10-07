"""Load the demo dataset into your HubSpot account.

Usage:
    python scripts/seed_hubspot.py            # first load
    python scripts/seed_hubspot.py --reset    # archive the previous demo records, then load fresh ones

Needs HUBSPOT_SERVICE_KEY in .env. Dates are relative to today, so run --reset
before a demo to bring "18 days since last activity" etc. back to the scripted story.
Only records this script created (marked with the mcp_demo_id property) are archived;
archived records go to HubSpot's recycle bin.
"""

from __future__ import annotations

import argparse
import sys

from crm_mcp import hubspot, hubspot_seed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reset", action="store_true", help="archive previous demo records first")
    args = parser.parse_args()

    if hubspot.OWNER_PROP != "mcp_sales_rep" or hubspot.LAST_ACTIVITY_PROP != "mcp_last_activity_date":
        sys.exit("The demo loader needs the default HUBSPOT_*_PROPERTY settings. Unset them in .env and retry.")

    print("Checking access and custom properties...")
    hubspot._token()
    hubspot_seed.ensure_properties()

    existing = {t: len(hubspot_seed.existing_demo_ids(t)) for t in ("companies", "contacts", "deals")}
    if any(existing.values()) and not args.reset:
        found = ", ".join(f"{n} {t}" for t, n in existing.items() if n)
        sys.exit(f"Demo records already exist ({found}). Run with --reset to replace them.")
    if args.reset:
        print("Archiving previous demo records...")
        hubspot_seed.remove_demo_records()

    print("Loading demo data...")
    counts = hubspot_seed.seed()
    print(f"Done: {counts}")
    print("HubSpot search can take a minute to index new records before the tools see them.")


if __name__ == "__main__":
    try:
        main()
    except hubspot.HubSpotError as e:
        sys.exit(f"HubSpot error: {e}")
