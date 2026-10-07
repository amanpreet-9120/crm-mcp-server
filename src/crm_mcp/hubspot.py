"""HubSpot backend: the same data-access functions as db.py, backed by the HubSpot CRM API.

Select it with CRM_BACKEND=hubspot and HUBSPOT_SERVICE_KEY=<key>. Every function
returns the same shapes as db.py, so server.py and logic.py are unchanged.

Field mapping (deal):
    title -> dealname            value -> amount            close_date -> closedate
    stage -> dealstage           (mapped to lead/qualified/proposal/negotiation/won/lost, see _our_stage)
    owner -> OWNER_PROP          (custom "mcp_sales_rep" by default; set HUBSPOT_OWNER_PROPERTY=hubspot_owner_id
                                  to use real HubSpot users)
    next_step -> hs_next_step    next_step_date -> NEXT_STEP_DATE_PROP
    last_activity_at -> LAST_ACTIVITY_PROP
Activities are HubSpot calls, emails, meetings and notes associated with the deal.

Writes are not transactional across API calls: add_activity creates the engagement,
then updates the deal. HubSpot search is eventually consistent, so deals we just
wrote are overlaid from a direct read for a short time (see _fresh).
"""

from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from typing import Any

BASE_URL = os.environ.get("HUBSPOT_BASE_URL", "https://api.hubapi.com")
OWNER_PROP = os.environ.get("HUBSPOT_OWNER_PROPERTY", "mcp_sales_rep")
LAST_ACTIVITY_PROP = os.environ.get("HUBSPOT_LAST_ACTIVITY_PROPERTY", "mcp_last_activity_date")
NEXT_STEP_DATE_PROP = os.environ.get("HUBSPOT_NEXT_STEP_DATE_PROPERTY", "mcp_next_step_date")
NEXT_STEP_PROP = "hs_next_step"
# HubSpot calculates these itself when activities are logged; never write them.
CALCULATED = {"notes_last_updated", "notes_last_contacted", "hs_lastmodifieddate"}

# Numbered paths are supported until 2027 (v4: March, v3: September). Move to
# date-versioned paths here when HubSpot finalises them for every endpoint.
OBJECTS = "/crm/v3/objects"
ASSOCIATIONS = "/crm/v4/associations"
PIPELINES = "/crm/v3/pipelines/deals"
PROPERTIES = "/crm/v3/properties"
OWNERS = "/crm/v3/owners"

DEAL_PROPS = ["dealname", "amount", "dealstage", "pipeline", "closedate", "createdate",
              NEXT_STEP_PROP, NEXT_STEP_DATE_PROP, LAST_ACTIVITY_PROP, OWNER_PROP]
COMPANY_PROPS = ["name", "industry", "numberofemployees", "city"]
CONTACT_PROPS = ["firstname", "lastname", "jobtitle", "email", "hs_buying_role"]

# Default HubSpot sales pipeline -> our stages. Stages not listed (custom pipelines)
# are mapped from their win probability, see _our_stage.
STAGE_MAP = {
    "appointmentscheduled": "lead",
    "qualifiedtobuy": "qualified",
    "presentationscheduled": "proposal",
    "decisionmakerboughtin": "proposal",
    "contractsent": "negotiation",
    "closedwon": "won",
    "closedlost": "lost",
}

# Engagement objects, the property holding their text, and the engagement->deal association type.
ACTIVITY_OBJECTS = {"call": "calls", "email": "emails", "meeting": "meetings", "note": "notes"}
ACTIVITY_TEXT = {"calls": "hs_call_body", "emails": "hs_email_text", "meetings": "hs_meeting_body", "notes": "hs_note_body"}
ACTIVITY_TO_DEAL = {"calls": 206, "emails": 210, "meetings": 212, "notes": 214}

CACHE_SECONDS = 60
FRESH_SECONDS = 120


class HubSpotError(RuntimeError):
    pass


# --------------------------------------------------------------------------- HTTP


def _token() -> str:
    token = os.environ.get("HUBSPOT_SERVICE_KEY") or os.environ.get("HUBSPOT_ACCESS_TOKEN")
    if not token:
        raise HubSpotError("HUBSPOT_SERVICE_KEY is not set. Add it to .env (see .env.example).")
    return token


def _request(method: str, path: str, body: dict | None = None, params: dict | None = None) -> Any:
    """One HubSpot API call with retries on rate limits and server errors."""
    url = BASE_URL + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(5):
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {_token()}", "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(float(e.headers.get("Retry-After") or 0) or 0.5 * 2 ** attempt)
                continue
            detail = e.read().decode(errors="replace")[:500]
            raise HubSpotError(f"HubSpot {method} {path} failed with {e.code}: {detail}") from None
    raise HubSpotError(f"HubSpot {method} {path} kept failing after retries")


def _cached(key: str, load):
    hit = _cache.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    value = load()
    _cache[key] = (time.monotonic() + CACHE_SECONDS, value)
    return value


_cache: dict[str, tuple[float, Any]] = {}
_fresh: dict[int, tuple[float, dict]] = {}


def clear_caches() -> None:
    _cache.clear()
    _fresh.clear()


# --------------------------------------------------------------------------- conversions


def _day(value: Any) -> str | None:
    """HubSpot date/datetime (ISO string or epoch ms) -> 'YYYY-MM-DD'."""
    if value in (None, ""):
        return None
    s = str(value)
    if s.isdigit():
        return datetime.fromtimestamp(int(s) / 1000, tz=timezone.utc).date().isoformat()
    return s[:10]


def _ms(d: date | str) -> str:
    """Date -> epoch milliseconds at midnight UTC, the form HubSpot date filters expect."""
    d = date.fromisoformat(d) if isinstance(d, str) else d
    return str(int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000))


def _text(value: str | None) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", value or "")).strip()


def _stages() -> dict[str, dict]:
    """Every deal stage id -> {pipeline, stage(ours), probability, label}."""
    def load():
        out = {}
        for p in (_request("GET", PIPELINES) or {}).get("results", []):
            for s in p.get("stages", []):
                meta = s.get("metadata") or {}
                out[s["id"]] = {"pipeline": p["id"], "label": s.get("label", s["id"]),
                                "probability": float(meta.get("probability") or 0),
                                "closed": str(meta.get("isClosed")).lower() == "true"}
        for sid, s in out.items():
            s["stage"] = _our_stage(sid, s["probability"], s["closed"])
        return out
    return _cached("stages", load)


def _our_stage(stage_id: str, probability: float, closed: bool) -> str:
    if stage_id in STAGE_MAP:
        return STAGE_MAP[stage_id]
    if closed:
        return "won" if probability >= 1 else "lost"
    if probability < 0.2:
        return "lead"
    if probability < 0.4:
        return "qualified"
    if probability < 0.7:
        return "proposal"
    return "negotiation"


def _stage_id(stage: str, pipeline: str | None) -> str:
    """Our stage -> a HubSpot stage id in the deal's pipeline (default pipeline first)."""
    stages = _stages()
    for sid, s in stages.items():
        if s["stage"] == stage and (pipeline is None or s["pipeline"] == pipeline):
            return sid
    raise HubSpotError(f"No HubSpot deal stage maps to '{stage}' in pipeline {pipeline}.")


def _owner_names() -> dict[str, str]:
    """HubSpot user id -> full name (only used when OWNER_PROP is hubspot_owner_id)."""
    def load():
        out, after = {}, None
        while True:
            page = _request("GET", OWNERS, params={"limit": 100, **({"after": after} if after else {})}) or {}
            for o in page.get("results", []):
                out[str(o["id"])] = f"{o.get('firstName', '')} {o.get('lastName', '')}".strip() or o.get("email", "")
            after = ((page.get("paging") or {}).get("next") or {}).get("after")
            if not after:
                return out
    return _cached("owners", load)


def _owner(props: dict) -> str:
    raw = props.get(OWNER_PROP) or ""
    if OWNER_PROP == "hubspot_owner_id":
        return _owner_names().get(str(raw), "Unassigned") if raw else "Unassigned"
    return raw or "Unassigned"


def _owner_value(name: str) -> str:
    """Owner name -> the value stored in OWNER_PROP (for search filters)."""
    if OWNER_PROP == "hubspot_owner_id":
        return next((i for i, n in _owner_names().items() if n == name), name)
    return name


def _company(obj: dict) -> dict:
    p = obj["properties"]
    return {"id": int(obj["id"]), "name": p.get("name") or "", "industry": _industry_label(p.get("industry")),
            "size": str(p.get("numberofemployees") or ""), "city": p.get("city") or ""}


def _industry_label(value: str | None) -> str:
    if not value:
        return ""
    labels = _cached("industry", lambda: {o["value"]: o["label"] for o in
                                           (_request("GET", f"{PROPERTIES}/companies/industry") or {}).get("options", [])})
    return labels.get(value, value.replace("_", " ").title())


def _deal(obj: dict, company: dict | None, contact_id: int | None) -> dict:
    p = obj["properties"]
    stage = _stages().get(p.get("dealstage") or "", {}).get("stage", "lead")
    created = _day(p.get("createdate"))
    return {
        "id": int(obj["id"]),
        "company_id": company["id"] if company else None,
        "primary_contact_id": contact_id,
        "title": p.get("dealname") or "",
        "value": int(float(p.get("amount") or 0)),
        "stage": stage,
        "owner": _owner(p),
        "created_at": created,
        "close_date": _day(p.get("closedate")) or created,
        "last_activity_at": _day(p.get(LAST_ACTIVITY_PROP)) or created,
        "next_step": p.get(NEXT_STEP_PROP) or None,
        "next_step_date": _day(p.get(NEXT_STEP_DATE_PROP)),
        "company": company["name"] if company else "(no company)",
        "pipeline": p.get("pipeline"),
    }


# --------------------------------------------------------------------------- object helpers


def _assoc(from_type: str, to_type: str, ids: list[int | str]) -> dict[int, list[int]]:
    """Batch-read associations: from id -> [to ids]."""
    out: dict[int, list[int]] = {int(i): [] for i in ids}
    for chunk in _chunks(list(ids), 1000):
        res = _request("POST", f"{ASSOCIATIONS}/{from_type}/{to_type}/batch/read",
                       {"inputs": [{"id": str(i)} for i in chunk]}) or {}
        for r in res.get("results", []):
            out[int(r["from"]["id"])] = [int(t["toObjectId"]) for t in r.get("to", [])]
    return out


def _batch_read(obj_type: str, ids: list[int], props: list[str]) -> list[dict]:
    out = []
    for chunk in _chunks(sorted(set(ids)), 100):
        res = _request("POST", f"{OBJECTS}/{obj_type}/batch/read",
                       {"inputs": [{"id": str(i)} for i in chunk], "properties": props}) or {}
        out += res.get("results", [])
    return out


def _chunks(items: list, n: int):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def _companies() -> dict[int, dict]:
    """All companies, cached briefly (used for name lookup and suggestions). Capped at 2,000."""
    def load():
        out, after = {}, None
        for _ in range(20):
            params = {"limit": 100, "properties": COMPANY_PROPS, **({"after": after} if after else {})}
            page = _request("GET", f"{OBJECTS}/companies", params=params) or {}
            for obj in page.get("results", []):
                out[int(obj["id"])] = _company(obj)
            after = ((page.get("paging") or {}).get("next") or {}).get("after")
            if not after:
                break
        return out
    return _cached("companies", load)


def _hydrate(objs: list[dict]) -> list[dict]:
    """Raw deal objects -> our deal dicts, with company and primary contact attached."""
    if not objs:
        return []
    ids = [int(o["id"]) for o in objs]
    to_company = _assoc("deals", "companies", ids)
    to_contact = _assoc("deals", "contacts", ids)
    known = _companies()
    missing = {c for cs in to_company.values() for c in cs if c not in known}
    extra = {int(o["id"]): _company(o) for o in _batch_read("companies", list(missing), COMPANY_PROPS)} if missing else {}
    out = []
    for o in objs:
        i = int(o["id"])
        cid = (to_company.get(i) or [None])[0]
        company = known.get(cid) or extra.get(cid) if cid else None
        out.append(_deal(o, company, (to_contact.get(i) or [None])[0]))
    return out


def _remember(deal_id: int) -> dict | None:
    """Re-read a deal we just wrote and overlay it on search results until search catches up."""
    deal = get_deal(deal_id)
    if deal:
        _fresh[deal_id] = (time.monotonic() + FRESH_SECONDS, deal)
    return deal


# --------------------------------------------------------------------------- data access (same API as db.py)


def list_owners() -> list[str]:
    if OWNER_PROP == "hubspot_owner_id":
        return sorted(set(_owner_names().values()))
    prop = _cached(f"prop:{OWNER_PROP}", lambda: _request("GET", f"{PROPERTIES}/deals/{OWNER_PROP}") or {})
    options = [o["label"] for o in prop.get("options", []) if not o.get("hidden")]
    return sorted(options) or sorted({d["owner"] for d in find_deals()})


def list_company_names() -> list[str]:
    return [c["name"] for c in _companies().values()]


def get_company(company_id: int) -> dict | None:
    if company_id in _companies():
        return _companies()[company_id]
    obj = _request("GET", f"{OBJECTS}/companies/{company_id}", params={"properties": COMPANY_PROPS})
    return _company(obj) if obj else None


def get_company_by_name(name: str) -> dict | None:
    return next((c for c in _companies().values() if c["name"] == name), None)


def get_deal(deal_id: int) -> dict | None:
    obj = _request("GET", f"{OBJECTS}/deals/{deal_id}", params={"properties": DEAL_PROPS})
    return _hydrate([obj])[0] if obj else None


def find_deals(
    *,
    company_id: int | None = None,
    company: str | None = None,
    stages: list[str] | None = None,
    exclude_stages: list[str] | None = None,
    owner: str | None = None,
    min_value: int | None = None,
    inactive_since: date | None = None,
    close_between: tuple[date, date] | None = None,
    sort_by: str | None = None,
) -> list[dict]:
    """Same contract as db.find_deals. Filters run in HubSpot search where possible,
    then again in Python so overlaid fresh writes are filtered consistently."""
    if company_id is not None:
        deal_ids = _assoc("companies", "deals", [company_id]).get(company_id, [])
        objs = _batch_read("deals", deal_ids, DEAL_PROPS) if deal_ids else []
    else:
        objs = _search_deals(_filters(stages, exclude_stages, owner, min_value, inactive_since, close_between))
    by_id = {d["id"]: d for d in _hydrate(objs)}

    now = time.monotonic()
    for deal_id, (expires, fresh) in list(_fresh.items()):
        if expires < now:
            del _fresh[deal_id]
        elif company_id is None or fresh["company_id"] == company_id:
            by_id[deal_id] = fresh

    def keep(d: dict) -> bool:
        return ((company_id is None or d["company_id"] == company_id)
                and (not company or company.lower() in d["company"].lower())
                and (not stages or d["stage"] in stages)
                and (not exclude_stages or d["stage"] not in exclude_stages)
                and (not owner or d["owner"] == owner)
                and (min_value is None or d["value"] >= min_value)
                and (inactive_since is None or d["last_activity_at"] <= inactive_since.isoformat())
                and (close_between is None
                     or close_between[0].isoformat() <= d["close_date"] <= close_between[1].isoformat()))

    rows = sorted((d for d in by_id.values() if keep(d)), key=lambda d: d["id"])
    if sort_by == "value":
        rows.sort(key=lambda d: -d["value"])
    elif sort_by == "last_activity":
        rows.sort(key=lambda d: d["last_activity_at"])
    elif sort_by == "close_date":
        rows.sort(key=lambda d: d["close_date"])
    return rows


def _filters(stages, exclude_stages, owner, min_value, inactive_since, close_between) -> list[dict]:
    f: list[dict] = []
    all_stages = _stages()
    if stages:
        f.append({"propertyName": "dealstage", "operator": "IN",
                  "values": [sid for sid, s in all_stages.items() if s["stage"] in stages] or ["__none__"]})
    if exclude_stages:
        excluded = [sid for sid, s in all_stages.items() if s["stage"] in exclude_stages]
        if excluded:
            f.append({"propertyName": "dealstage", "operator": "NOT_IN", "values": excluded})
    if owner:
        f.append({"propertyName": OWNER_PROP, "operator": "EQ", "value": _owner_value(owner)})
    if min_value is not None:
        f.append({"propertyName": "amount", "operator": "GTE", "value": str(min_value)})
    if inactive_since is not None:
        f.append({"propertyName": LAST_ACTIVITY_PROP, "operator": "LTE", "value": _ms(inactive_since)})
    if close_between is not None:
        f.append({"propertyName": "closedate", "operator": "BETWEEN", "value": _ms(close_between[0]),
                  "highValue": str(int(_ms(close_between[1])) + 86_399_999)})
    return f


def _search_deals(filters: list[dict]) -> list[dict]:
    out, after = [], None
    while True:
        body = {"filterGroups": [{"filters": filters}] if filters else [], "properties": DEAL_PROPS, "limit": 200,
                "sorts": [{"propertyName": "hs_object_id", "direction": "ASCENDING"}]}
        if after:
            body["after"] = after
        page = _request("POST", f"{OBJECTS}/deals/search", body) or {}
        out += page.get("results", [])
        after = ((page.get("paging") or {}).get("next") or {}).get("after")
        if not after or len(out) >= 10_000:
            return out


def list_contacts(company_id: int) -> list[dict]:
    ids = _assoc("companies", "contacts", [company_id]).get(company_id, [])
    rows = []
    for obj in sorted(_batch_read("contacts", ids, CONTACT_PROPS), key=lambda o: int(o["id"])):
        p = obj["properties"]
        rows.append({
            "contact_id": int(obj["id"]),
            "name": f"{p.get('firstname') or ''} {p.get('lastname') or ''}".strip(),
            "title": p.get("jobtitle") or "",
            "email": p.get("email") or "",
            "is_decision_maker": "DECISION_MAKER" in (p.get("hs_buying_role") or "").split(";"),
        })
    rows.sort(key=lambda r: not r["is_decision_maker"])
    return rows


def recent_activities(company_id: int, limit: int = 10) -> list[dict]:
    deal_ids = _assoc("companies", "deals", [company_id]).get(company_id, [])
    if not deal_ids:
        return []
    acts = []
    for kind, obj_type in ACTIVITY_OBJECTS.items():
        links = _assoc("deals", obj_type, deal_ids)
        owner_of = {a: d for d, actions in links.items() for a in actions}
        if not owner_of:
            continue
        text_prop = ACTIVITY_TEXT[obj_type]
        for obj in _batch_read(obj_type, list(owner_of), ["hs_timestamp", text_prop, "hubspot_owner_id"]):
            p = obj["properties"]
            owner_id = p.get("hubspot_owner_id")
            acts.append({
                "deal_id": owner_of[int(obj["id"])],
                "type": kind,
                "summary": _text(p.get(text_prop)),
                "date": _day(p.get("hs_timestamp")),
                "by": _owner_names().get(str(owner_id), "HubSpot user") if owner_id else "HubSpot user",
                "_id": int(obj["id"]),
            })
    acts.sort(key=lambda a: (a["date"] or "", a["_id"]), reverse=True)
    for a in acts:
        a.pop("_id")
    return acts[:limit]


def add_activity(deal_id: int, type: str, summary: str, on: date, by: str,
                 next_step: str | None = None, next_step_date: str | None = None) -> int:
    """Create a call/email/meeting/note on the deal, then update its last activity and next step."""
    obj_type = ACTIVITY_OBJECTS[type]
    ts = f"{on.isoformat()}T{datetime.now(timezone.utc).strftime('%H:%M:%S')}Z"
    props = {"hs_timestamp": ts, ACTIVITY_TEXT[obj_type]: summary}
    if obj_type == "calls":
        props["hs_call_title"] = f"Call logged by {by}"
    elif obj_type == "emails":
        props.update({"hs_email_subject": f"Email logged by {by}", "hs_email_direction": "EMAIL", "hs_email_status": "SENT"})
    elif obj_type == "meetings":
        props.update({"hs_meeting_title": f"Meeting logged by {by}", "hs_meeting_start_time": ts,
                      "hs_meeting_end_time": ts, "hs_meeting_outcome": "COMPLETED"})
    created = _request("POST", f"{OBJECTS}/{obj_type}", {
        "properties": props,
        "associations": [{"to": {"id": str(deal_id)}, "types": [
            {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": ACTIVITY_TO_DEAL[obj_type]}]}],
    })
    deal_props: dict[str, str] = {}
    if LAST_ACTIVITY_PROP not in CALCULATED:
        deal_props[LAST_ACTIVITY_PROP] = on.isoformat()
    if next_step:
        deal_props[NEXT_STEP_PROP] = next_step
        deal_props[NEXT_STEP_DATE_PROP] = next_step_date or ""
    if deal_props:
        _request("PATCH", f"{OBJECTS}/deals/{deal_id}", {"properties": deal_props})
    _remember(deal_id)
    return int(created["id"])


def update_deal(deal_id: int, changes: dict) -> None:
    """Same contract as db.update_deal. None clears a field."""
    field = {"value": "amount", "close_date": "closedate", "next_step": NEXT_STEP_PROP,
             "next_step_date": NEXT_STEP_DATE_PROP}
    bad = set(changes) - set(field) - {"stage"}
    if bad:
        raise ValueError(f"not updatable: {sorted(bad)}")
    props: dict[str, str] = {}
    for key, value in changes.items():
        if key == "stage":
            current = get_deal(deal_id)
            props["dealstage"] = _stage_id(value, current.get("pipeline") if current else None)
        else:
            props[field[key]] = "" if value is None else str(value)
    _request("PATCH", f"{OBJECTS}/deals/{deal_id}", {"properties": props})
    _remember(deal_id)
