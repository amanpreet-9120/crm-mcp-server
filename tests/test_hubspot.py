"""The HubSpot backend must give the same answers as SQLite on the same data.

The demo data is loaded into a fake HubSpot API through the real seeding code
(hubspot_seed.seed), then every tool runs against both backends.
"""

from __future__ import annotations

import asyncio
import io
import json
import urllib.error

import pytest
from mcp import Client

from crm_mcp import db, hubspot, hubspot_seed, server
from fake_hubspot import FakeHubSpot


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "crm.db")
    monkeypatch.setattr(server, "LOG_PATH", tmp_path / "calls.jsonl")
    monkeypatch.setattr(server, "READ_ONLY", False)
    db.reset_and_seed()
    api = FakeHubSpot()
    monkeypatch.setattr(hubspot, "_request", api)
    hubspot.clear_caches()
    hubspot_seed.ensure_properties(log=lambda *_: None)
    hubspot_seed.seed(log=lambda *_: None)
    yield api
    hubspot.clear_caches()


def run(backend, calls):
    server.store = backend
    async def go():
        out = []
        async with Client(server.mcp) as c:
            for name, args in calls:
                r = await c.call_tool(name, args)
                text = r.content[0].text
                out.append({"_error": text} if r.is_error else json.loads(text))
        return out
    try:
        return asyncio.run(go())
    finally:
        server.store = db


READS = [
    ("search_deals", {}), ("search_deals", {"company": "acme"}), ("search_deals", {"stage": "won"}),
    ("search_deals", {"include_closed": True, "limit": 50}), ("search_deals", {"owner": "marcus", "sort_by": "close_date"}),
    ("search_deals", {"min_value": 30000, "sort_by": "last_activity"}), ("search_deals", {"inactive_days": 14}),
    ("search_deals", {"closing_within_days": 30, "sort_by": "close_date"}), ("search_deals", {"company": "zzz"}),
    ("search_deals", {"owner": "Bob"}),
    ("search_deals", {"stage": "proposal", "owner": "Sofia", "min_value": 10000, "inactive_days": 5, "closing_within_days": 60}),
    ("get_pipeline_summary", {}), ("get_pipeline_summary", {"owner": "priya"}),
    ("get_deals_needing_attention", {}), ("get_deals_needing_attention", {"min_value": 40000, "limit": 3}),
    ("get_deals_needing_attention", {"owner": "Marcus Lee", "limit": 50}),
    ("get_account", {"company": "Meridan Hotel"}), ("get_account", {"company": "999"}),
]


# Forecast figures use HubSpot's own stage probabilities, so they differ by design (tested below).
PROBABILITY_FIELDS = {"weighted_value", "weighted_forecast", "stage_probabilities", "forecast_impact"}


def without_probabilities(value):
    if isinstance(value, dict):
        return {k: without_probabilities(v) for k, v in value.items() if k not in PROBABILITY_FIELDS}
    if isinstance(value, list):
        return [without_probabilities(v) for v in value]
    return value


def test_reads_match_sqlite(fake):
    assert without_probabilities(run(hubspot, READS)) == without_probabilities(run(db, READS))


def test_get_account_matches_sqlite(fake):
    calls = [("get_account", {"company": name}) for name in ("Meridian Hotels", "acme", "13", "Dunmore Retail Group")]
    for want, got in zip(run(db, calls), run(hubspot, calls)):
        assert got["company"]["name"] == want["company"]["name"]
        assert got["deals"] == want["deals"]
        assert got["contacts"] == want["contacts"]
        strip = lambda acts: sorted((a["deal_id"], a["type"], a["summary"], a["date"]) for a in acts)
        assert strip(got["recent_activity"]) == strip(want["recent_activity"])


def test_write_flow_matches_sqlite(fake):
    calls = [
        ("log_activity", {"deal_id": 1, "type": "call", "summary": "CFO approved", "next_step": "Send contract",
                          "next_step_date": "2099-01-01"}),
        ("get_deals_needing_attention", {"limit": 50}),
        ("update_deal", {"deal_id": 1, "stage": "won"}),
        ("update_deal", {"deal_id": 1, "stage": "won", "confirm": True}),
        ("search_deals", {"company": "acme", "include_closed": True}),
        ("log_activity", {"deal_id": 1, "type": "note", "summary": "x", "next_step": "y"}),
        ("update_deal", {"deal_id": 2, "value": 99000, "close_date": "2099-02-02", "confirm": True}),
        ("get_pipeline_summary", {}),
        ("update_deal", {"deal_id": 999, "value": 5}),
    ]
    want = run(db, calls)
    got = run(hubspot, calls)
    for w, g in zip(want, got):
        w.pop("activity_id", None), g.pop("activity_id", None)
    assert without_probabilities(got) == without_probabilities(want)
    deal = fake.objects["deals"][1]
    assert deal["dealstage"] == "closedwon" and "hs_next_step" not in deal


def test_fresh_writes_survive_search_lag(fake, monkeypatch):
    stale = hubspot._search_deals([])                      # what search returns before indexing catches up
    monkeypatch.setattr(hubspot, "_search_deals", lambda filters: stale)
    before = run(hubspot, [("get_deals_needing_attention", {"limit": 50})])[0]
    assert 1 in [d["deal_id"] for d in before["deals"]]
    run(hubspot, [("log_activity", {"deal_id": 1, "type": "call", "summary": "Approved", "next_step": "Send contract",
                                    "next_step_date": "2099-01-01"})])
    after = run(hubspot, [("get_deals_needing_attention", {"limit": 50})])[0]
    assert 1 not in [d["deal_id"] for d in after["deals"]]


def test_activities_are_real_hubspot_engagements(fake):
    run(hubspot, [("log_activity", {"deal_id": 3, "type": "meeting", "summary": "Kickoff held"})])
    meeting_id = max(fake.objects["meetings"])
    m = fake.objects["meetings"][meeting_id]
    assert m["hs_meeting_body"] == "Kickoff held" and m["hs_meeting_outcome"] == "COMPLETED"
    assert 3 in fake.links[("meetings", "deals")][meeting_id]


def test_stage_mapping_handles_custom_pipelines():
    assert hubspot._our_stage("contractsent", 0.9, False) == "negotiation"
    assert hubspot._our_stage("custom_1", 0.1, False) == "lead"
    assert hubspot._our_stage("custom_2", 0.5, False) == "proposal"
    assert hubspot._our_stage("custom_won", 1.0, True) == "won"
    assert hubspot._our_stage("custom_lost", 0.0, True) == "lost"


def test_reset_archives_only_demo_records(fake):
    other = fake.create("deals", {"dealname": "Real customer deal", "dealstage": "qualifiedtobuy"})
    hubspot_seed.remove_demo_records(log=lambda *_: None)
    assert list(fake.objects["deals"]) == [int(other["id"])]
    assert not fake.objects["companies"] and not fake.objects["notes"]


def test_request_retries_rate_limits(monkeypatch):
    monkeypatch.setenv("HUBSPOT_SERVICE_KEY", "test-key")
    monkeypatch.setattr(hubspot.time, "sleep", lambda s: None)
    attempts = []

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def urlopen(req, timeout):
        attempts.append(req.get_header("Authorization"))
        if len(attempts) == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "slow down", {"Retry-After": "1"}, io.BytesIO(b""))
        return Resp(b'{"ok": true}')

    monkeypatch.setattr(hubspot.urllib.request, "urlopen", urlopen)
    assert hubspot._request("GET", "/crm/v3/owners") == {"ok": True}
    assert attempts == ["Bearer test-key"] * 2


def test_missing_key_explains_fix(monkeypatch):
    monkeypatch.setenv("HUBSPOT_SERVICE_KEY", "")
    with pytest.raises(hubspot.HubSpotError, match="HUBSPOT_SERVICE_KEY is not set"):
        hubspot._token()


def test_backend_errors_reach_the_model(fake, monkeypatch):
    def down(*a, **k):
        raise hubspot.HubSpotError("HubSpot GET /crm/v3/objects/deals/1 failed with 401: expired key")
    monkeypatch.setattr(hubspot, "_request", down)
    monkeypatch.setattr(server, "BACKEND_ERRORS", (hubspot.HubSpotError,))
    hubspot.clear_caches()
    r = run(hubspot, [("get_pipeline_summary", {})])[0]
    assert "could not be reached" in r["_error"] and "401" in r["_error"]


def test_forecast_uses_hubspot_stage_probabilities(fake):
    """Weighted numbers must match what HubSpot shows: amount x the stage's probability."""
    probability = {st["id"]: float(st["metadata"]["probability"]) for st in __import__("fake_hubspot").PIPELINE["stages"]}
    open_deals = [d for d in fake.objects["deals"].values() if d["dealstage"] not in ("closedwon", "closedlost")]
    expected = sum(round(float(d["amount"]) * probability[d["dealstage"]]) for d in open_deals)
    summary = run(hubspot, [("get_pipeline_summary", {})])[0]
    assert summary["weighted_forecast"] == expected
    assert summary["stage_probabilities"]["Appointment Scheduled"] == 0.2
    sqlite = run(db, [("get_pipeline_summary", {})])[0]
    assert sqlite["stage_probabilities"]["lead"] == 0.1          # SQLite keeps the playbook defaults


def test_preview_impact_uses_hubspot_probabilities(fake):
    preview = run(hubspot, [("update_deal", {"deal_id": 1, "stage": "won"})])[0]   # Contract Sent 90% -> Won 100%
    assert preview["forecast_impact"] == round(48000 * 1.0) - round(48000 * 0.9)


def test_playbook_shows_backend_probabilities(fake):
    server.store = hubspot
    try:
        text = server.playbook()
    finally:
        server.store = db
    assert "Contract Sent: 90%" in text and "pipeline settings" in text
