"""Load the demo dataset into a HubSpot account (used by scripts/seed_hubspot.py).

The records come from the same deterministic generator as the SQLite demo, so
both backends tell the same story. Every company, contact and deal created here
carries the custom property mcp_demo_id, which is how --reset finds them again.
"""

from __future__ import annotations

import difflib
import sqlite3
import tempfile
from pathlib import Path

from . import db, hubspot
from .hubspot import ACTIVITY_OBJECTS, ACTIVITY_TEXT, ACTIVITY_TO_DEAL, OBJECTS, PROPERTIES

DEMO_KEY = "mcp_demo_id"
STAGE_IDS = {"lead": "appointmentscheduled", "qualified": "qualifiedtobuy", "proposal": "presentationscheduled",
             "negotiation": "contractsent", "won": "closedwon", "lost": "closedlost"}
# HUBSPOT_DEFINED association types used on create.
CONTACT_TO_COMPANY, DEAL_TO_COMPANY, DEAL_TO_CONTACT = 1, 5, 3

CUSTOM_PROPERTIES = {
    "deals": [
        {"name": "mcp_sales_rep", "label": "Sales rep (demo)", "type": "enumeration", "fieldType": "select",
         "options": [{"label": o, "value": o, "displayOrder": i} for i, o in enumerate(sorted(db.OWNERS))]},
        {"name": "mcp_next_step_date", "label": "Next step date", "type": "date", "fieldType": "date"},
        {"name": "mcp_last_activity_date", "label": "Last activity date (demo)", "type": "date", "fieldType": "date"},
        {"name": DEMO_KEY, "label": "Demo record id", "type": "string", "fieldType": "text"},
    ],
    "companies": [{"name": DEMO_KEY, "label": "Demo record id", "type": "string", "fieldType": "text"}],
    "contacts": [{"name": DEMO_KEY, "label": "Demo record id", "type": "string", "fieldType": "text"}],
}
GROUPS = {"deals": "dealinformation", "companies": "companyinformation", "contacts": "contactinformation"}


# --------------------------------------------------------------------------- records


def demo_rows() -> dict[str, list[dict]]:
    """The demo dataset as plain rows, generated through the SQLite seed."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "seed.db"
        db.reset_and_seed(path)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        rows = {t: [dict(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY id")]
                for t in ("companies", "contacts", "deals", "activities")}
        conn.close()
    return rows


def company_props(row: dict, industry_value: str | None) -> dict:
    props = {"name": row["name"], "city": row["city"], DEMO_KEY: f"company-{row['id']}",
             "numberofemployees": row["size"].split("-")[0].rstrip("+")}
    if industry_value:
        props["industry"] = industry_value
    return props


def contact_props(row: dict) -> dict:
    first, last = row["name"].split(" ", 1)
    props = {"firstname": first, "lastname": last, "jobtitle": row["title"], "email": row["email"],
             DEMO_KEY: f"contact-{row['id']}"}
    if row["is_decision_maker"]:
        props["hs_buying_role"] = "DECISION_MAKER"
    return props


def deal_props(row: dict, stage_ids: dict[str, str]) -> dict:
    return {
        "dealname": row["title"], "amount": str(row["value"]), "dealstage": stage_ids[row["stage"]],
        "pipeline": "default", "closedate": f"{row['close_date']}T00:00:00Z",
        hubspot.OWNER_PROP: row["owner"], hubspot.LAST_ACTIVITY_PROP: row["last_activity_at"],
        hubspot.NEXT_STEP_PROP: row["next_step"] or "", hubspot.NEXT_STEP_DATE_PROP: row["next_step_date"] or "",
        DEMO_KEY: f"deal-{row['id']}",
    }


def activity_props(row: dict) -> tuple[str, dict]:
    obj_type = ACTIVITY_OBJECTS[row["type"]]
    ts = f"{row['created_at']}T12:00:00Z"
    props = {"hs_timestamp": ts, ACTIVITY_TEXT[obj_type]: row["summary"]}
    title = f"{row['type'].title()} by {row['created_by']}"
    if obj_type == "calls":
        props["hs_call_title"] = title
    elif obj_type == "emails":
        props.update({"hs_email_subject": title, "hs_email_direction": "EMAIL", "hs_email_status": "SENT"})
    elif obj_type == "meetings":
        props.update({"hs_meeting_title": title, "hs_meeting_start_time": ts, "hs_meeting_end_time": ts,
                      "hs_meeting_outcome": "COMPLETED"})
    return obj_type, props


def _link(to_id: int | str, type_id: int) -> dict:
    return {"to": {"id": str(to_id)}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": type_id}]}


# --------------------------------------------------------------------------- HubSpot steps


def ensure_properties(log=print) -> None:
    for obj_type, props in CUSTOM_PROPERTIES.items():
        for prop in props:
            if hubspot._request("GET", f"{PROPERTIES}/{obj_type}/{prop['name']}"):
                continue
            hubspot._request("POST", f"{PROPERTIES}/{obj_type}", {**prop, "groupName": GROUPS[obj_type]})
            log(f"  created {obj_type} property {prop['name']}")


def existing_demo_ids(obj_type: str) -> list[str]:
    ids, after = [], None
    while True:
        body = {"filterGroups": [{"filters": [{"propertyName": DEMO_KEY, "operator": "HAS_PROPERTY"}]}],
                "properties": [DEMO_KEY], "limit": 200}
        if after:
            body["after"] = after
        page = hubspot._request("POST", f"{OBJECTS}/{obj_type}/search", body) or {}
        ids += [r["id"] for r in page.get("results", [])]
        after = ((page.get("paging") or {}).get("next") or {}).get("after")
        if not after:
            return ids


def archive(obj_type: str, ids: list) -> None:
    for i in range(0, len(ids), 100):
        hubspot._request("POST", f"{OBJECTS}/{obj_type}/batch/archive", {"inputs": [{"id": str(x)} for x in ids[i:i + 100]]})


def remove_demo_records(log=print) -> None:
    """Archive (HubSpot recycle bin) every record this loader created, plus the deals' activities."""
    deal_ids = existing_demo_ids("deals")
    if deal_ids:
        for obj_type in ACTIVITY_OBJECTS.values():
            linked = hubspot._assoc("deals", obj_type, deal_ids)
            archive(obj_type, sorted({a for acts in linked.values() for a in acts}))
    for obj_type in ("deals", "contacts", "companies"):
        ids = deal_ids if obj_type == "deals" else existing_demo_ids(obj_type)
        archive(obj_type, ids)
        log(f"  archived {len(ids)} {obj_type}")


def stage_ids() -> dict[str, str]:
    """Our stages -> stage ids in the account's default pipeline."""
    known = hubspot._stages()
    return {ours: sid if known.get(sid, {}).get("pipeline") == "default" else hubspot._stage_id(ours, "default")
            for ours, sid in STAGE_IDS.items()}


def industry_values(labels: set[str]) -> dict[str, str | None]:
    """Our industry labels -> the closest option in HubSpot's industry list (or None)."""
    options = {o["label"]: o["value"] for o in (hubspot._request("GET", f"{PROPERTIES}/companies/industry") or {}).get("options", [])}
    out = {}
    for label in labels:
        match = difflib.get_close_matches(label, list(options), n=1, cutoff=0.5)
        out[label] = options[match[0]] if match else None
    return out


def _create(obj_type: str, inputs: list[dict]) -> dict[str, str]:
    """Batch-create and return demo key -> new HubSpot id (activities have no key)."""
    out = {}
    for i in range(0, len(inputs), 100):
        res = hubspot._request("POST", f"{OBJECTS}/{obj_type}/batch/create", {"inputs": inputs[i:i + 100]}) or {}
        for r in res.get("results", []):
            key = (r.get("properties") or {}).get(DEMO_KEY)
            if key:
                out[key] = r["id"]
    return out


def seed(rows: dict[str, list[dict]] | None = None, log=print) -> dict[str, int]:
    rows = rows or demo_rows()
    stages = stage_ids()
    industries = industry_values({c["industry"] for c in rows["companies"]})

    companies = _create("companies", [{"properties": company_props(c, industries[c["industry"]])}
                                      for c in rows["companies"]])
    cid = {c["id"]: companies[f"company-{c['id']}"] for c in rows["companies"]}
    log(f"  created {len(companies)} companies")

    contacts = _create("contacts", [{"properties": contact_props(c), "associations": [_link(cid[c["company_id"]], CONTACT_TO_COMPANY)]}
                                    for c in rows["contacts"]])
    pid = {c["id"]: contacts[f"contact-{c['id']}"] for c in rows["contacts"]}
    log(f"  created {len(contacts)} contacts")

    deals = _create("deals", [{"properties": deal_props(d, stages),
                               "associations": [_link(cid[d["company_id"]], DEAL_TO_COMPANY)]
                               + ([_link(pid[d["primary_contact_id"]], DEAL_TO_CONTACT)] if d["primary_contact_id"] else [])}
                              for d in rows["deals"]])
    did = {d["id"]: deals[f"deal-{d['id']}"] for d in rows["deals"]}
    log(f"  created {len(deals)} deals")

    by_type: dict[str, list[dict]] = {}
    for a in rows["activities"]:
        obj_type, props = activity_props(a)
        by_type.setdefault(obj_type, []).append(
            {"properties": props, "associations": [_link(did[a["deal_id"]], ACTIVITY_TO_DEAL[obj_type])]})
    for obj_type, inputs in by_type.items():
        _create(obj_type, inputs)
    log(f"  created {len(rows['activities'])} activities (calls, emails, meetings, notes)")
    hubspot.clear_caches()
    return {"companies": len(companies), "contacts": len(contacts), "deals": len(deals),
            "activities": len(rows["activities"])}
