"""Tests run the real MCP server in-process through the MCP client,
so they check what a model would actually see (schemas, JSON, error text)."""

from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta

import pytest
from mcp import Client

from crm_mcp import db, logic, server


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    path = tmp_path / "crm.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(server, "LOG_PATH", tmp_path / "calls.jsonl")
    monkeypatch.setattr(server, "READ_ONLY", False)
    db.reset_and_seed(path)
    yield


def call(tool: str, args: dict | None = None):
    async def run():
        async with Client(server.mcp) as c:
            return await c.call_tool(tool, args or {})
    res = asyncio.run(run())
    text = res.content[0].text
    if res.is_error:
        return {"_error": text}
    return json.loads(text)


# ---------------------------------------------------------------- pure logic

def test_stale_rule_uses_stage_limit():
    t = date(2026, 1, 31)
    base = {"stage": "negotiation", "value": 1000, "close_date": "2026-03-01",
            "next_step": "x", "next_step_date": "2026-02-05"}
    fresh = logic.assess_deal({**base, "last_activity_at": "2026-01-25"}, t)  # 6 days
    stale = logic.assess_deal({**base, "last_activity_at": "2026-01-20"}, t)  # 11 days > 7
    assert fresh.reasons == []
    assert "No activity for 11 days" in stale.reasons[0]


def test_pipeline_math():
    s = logic.pipeline_summary([
        {"stage": "proposal", "value": 10_000},
        {"stage": "negotiation", "value": 20_000},
        {"stage": "won", "value": 5_000},
        {"stage": "lost", "value": 7_000},
    ])
    assert s["open_pipeline_value"] == 30_000
    assert s["weighted_forecast"] == 5_000 + 15_000
    assert s["win_rate"] == 0.5


# ---------------------------------------------------------------- tools over MCP

def test_tools_are_listed_with_annotations():
    async def run():
        async with Client(server.mcp) as c:
            return (await c.list_tools()).tools
    tools = {t.name: t for t in asyncio.run(run())}
    assert set(tools) == {"search_deals", "get_account", "get_pipeline_summary",
                          "get_deals_needing_attention", "log_activity", "update_deal"}
    assert tools["search_deals"].annotations.read_only_hint is True
    assert tools["update_deal"].annotations.destructive_hint is True


def test_attention_finds_showcase_deals():
    r = call("get_deals_needing_attention", {"min_value": 40_000})
    companies = {d["company"] for d in r["deals"]}
    assert {"Meridian Hotels", "Acme Logistics"} <= companies
    assert all(d["reasons"] for d in r["deals"])


def test_search_filters_and_truncation_note():
    r = call("search_deals", {"limit": 3})
    assert r["returned"] == 3 and "more not shown" in r["note"]
    r = call("search_deals", {"owner": "priya", "min_value": 20_000})
    assert r["deals"] and all(d["owner"] == "Priya Shah" and d["value"] >= 20_000 for d in r["deals"])


def test_unknown_owner_lists_valid_ones():
    r = call("get_pipeline_summary", {"owner": "Bob"})
    assert "Valid owners" in r["_error"] and "Priya Shah" in r["_error"]


def test_company_typo_gets_suggestion():
    r = call("get_account", {"company": "Meridan Hotel"})
    assert "Did you mean" in r["_error"] and "Meridian Hotels" in r["_error"]


def test_get_account_partial_name():
    r = call("get_account", {"company": "meridian"})
    assert r["company"]["name"] == "Meridian Hotels"
    assert r["contacts"] and r["deals"]


def test_log_activity_clears_staleness():
    before = call("get_account", {"company": "Acme Logistics"})
    deal = next(d for d in before["deals"] if d["deal_id"] == 1)
    assert deal["days_since_activity"] >= 18
    tomorrow = (db.today() + timedelta(days=1)).isoformat()
    r = call("log_activity", {"deal_id": 1, "type": "call", "summary": "Spoke to CFO, contract approved in principle",
                              "next_step": "Send contract", "next_step_date": tomorrow})
    assert r["deal"]["days_since_activity"] == 0
    assert r["remaining_issues"] == []


def test_log_activity_rejects_past_next_step():
    r = call("log_activity", {"deal_id": 1, "type": "note", "summary": "x",
                              "next_step": "y", "next_step_date": "2020-01-01"})
    assert "in the past" in r["_error"]


def test_update_deal_previews_then_confirms():
    preview = call("update_deal", {"deal_id": 1, "stage": "won"})
    assert preview["status"] == "preview"
    assert preview["changes"]["stage"] == {"from": "negotiation", "to": "won"}
    # nothing written yet
    assert call("search_deals", {"company": "Acme", "stage": "negotiation"})["total_matches"] >= 1
    done = call("update_deal", {"deal_id": 1, "stage": "won", "confirm": True})
    assert done["status"] == "updated" and done["deal"]["stage"] == "won"
    again = call("update_deal", {"deal_id": 1, "stage": "lost", "confirm": True})
    assert "already won" in again["_error"]


def test_bad_deal_id_explains_fix():
    r = call("update_deal", {"deal_id": 9999, "value": 10})
    assert "does not exist" in r["_error"] and "search_deals" in r["_error"]


def test_read_only_mode_blocks_writes(monkeypatch):
    monkeypatch.setattr(server, "READ_ONLY", True)
    r = call("log_activity", {"deal_id": 1, "type": "note", "summary": "x"})
    assert "read-only" in r["_error"]


def test_calls_are_logged():
    call("get_pipeline_summary")
    lines = server.LOG_PATH.read_text().strip().splitlines()
    entry = json.loads(lines[-1])
    assert entry["tool"] == "get_pipeline_summary" and entry["ok"] and entry["est_tokens"] > 0
