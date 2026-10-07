"""Connect to a running server over HTTP exactly like a real client would.

Usage:  python scripts/smoke_http.py http://localhost:8000/mcp [token]
"""

import asyncio
import json
import sys

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client


async def main(url: str, token: str | None) -> None:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx2.AsyncClient(headers=headers, timeout=30) as http:
        async with Client(streamable_http_client(url, http_client=http)) as client:
            tools = (await client.list_tools()).tools
            print("tools:", ", ".join(t.name for t in tools))
            res = await client.call_tool("get_deals_needing_attention", {"limit": 3})
            data = json.loads(res.content[0].text)
            print(f"flagged deals: {data['flagged_deals']}, value at risk: ${data['value_at_risk']:,}")
            for d in data["deals"]:
                print(f"  #{d['deal_id']} {d['company']} ${d['value']:,} [{d['priority']}] - {d['reasons'][0]}")
            print("OK")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
