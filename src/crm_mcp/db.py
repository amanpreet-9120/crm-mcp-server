"""SQLite storage: the only module that talks to the database.

To connect a real CRM (HubSpot, Salesforce, Pipedrive, Postgres), replace this
file and keep the functions under "data access" with the same signatures and
return shapes. Deals are dicts with the `deals` columns plus `company` (the
company name); dates are ISO strings. server.py and logic.py need no changes.

The seed uses a fixed random seed and dates *relative to today*, so the demo
always has the same shape (some stale deals, some overdue next steps) no matter
when it is run or redeployed.
"""

from __future__ import annotations

import os
import random
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

DEFAULT_DB_PATH = Path(os.environ.get("CRM_DB_PATH", Path(__file__).resolve().parents[2] / "data" / "crm.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    industry TEXT NOT NULL,
    size TEXT NOT NULL,
    city TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    name TEXT NOT NULL,
    title TEXT NOT NULL,
    email TEXT NOT NULL,
    is_decision_maker INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS deals (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    primary_contact_id INTEGER REFERENCES contacts(id),
    title TEXT NOT NULL,
    value INTEGER NOT NULL CHECK (value > 0),
    stage TEXT NOT NULL,
    owner TEXT NOT NULL,
    created_at TEXT NOT NULL,
    close_date TEXT NOT NULL,
    last_activity_at TEXT NOT NULL,
    next_step TEXT,
    next_step_date TEXT
);
CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY,
    deal_id INTEGER NOT NULL REFERENCES deals(id),
    type TEXT NOT NULL,
    summary TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deals_stage ON deals(stage);
CREATE INDEX IF NOT EXISTS idx_deals_owner ON deals(owner);
CREATE INDEX IF NOT EXISTS idx_activities_deal ON activities(deal_id);
"""


def today() -> date:
    """Single source of 'now' so tests can reason about relative dates."""
    return datetime.now(timezone.utc).date()


@contextmanager
def connect(path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(str(path or DEFAULT_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------- data access

_DEAL = "SELECT d.*, c.name AS company FROM deals d JOIN companies c ON c.id=d.company_id"
_SORT = {"value": "d.value DESC", "last_activity": "d.last_activity_at ASC", "close_date": "d.close_date ASC"}


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> dict | None:
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def list_owners() -> list[str]:
    """Distinct deal owners (sales reps), sorted."""
    with connect() as conn:
        return [r["owner"] for r in conn.execute("SELECT DISTINCT owner FROM deals ORDER BY owner")]


def list_company_names() -> list[str]:
    with connect() as conn:
        return [r["name"] for r in conn.execute("SELECT name FROM companies")]


def get_company(company_id: int) -> dict | None:
    with connect() as conn:
        return _one(conn, "SELECT * FROM companies WHERE id=?", (company_id,))


def get_company_by_name(name: str) -> dict | None:
    with connect() as conn:
        return _one(conn, "SELECT * FROM companies WHERE name=?", (name,))


def get_deal(deal_id: int) -> dict | None:
    with connect() as conn:
        return _one(conn, _DEAL + " WHERE d.id=?", (deal_id,))


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
    """Deals matching every given filter.

    company_id: exact company. company: case-insensitive partial name. owner: exact name.
    inactive_since: last activity on or before this date.
    close_between: inclusive (start, end) close-date range.
    sort_by: "value" (high first), "last_activity" (oldest first), "close_date" (soonest first).
    """
    where, params = ["1=1"], []
    if company_id is not None:
        where.append("d.company_id = ?")
        params.append(company_id)
    if company:
        where.append("c.name LIKE ?")
        params.append(f"%{company}%")
    if stages:
        where.append(f"d.stage IN ({','.join('?' * len(stages))})")
        params += stages
    if exclude_stages:
        where.append(f"d.stage NOT IN ({','.join('?' * len(exclude_stages))})")
        params += exclude_stages
    if owner:
        where.append("d.owner = ?")
        params.append(owner)
    if min_value is not None:
        where.append("d.value >= ?")
        params.append(min_value)
    if inactive_since is not None:
        where.append("d.last_activity_at <= ?")
        params.append(inactive_since.isoformat())
    if close_between is not None:
        where.append("d.close_date BETWEEN ? AND ?")
        params += [close_between[0].isoformat(), close_between[1].isoformat()]
    sql = f"{_DEAL} WHERE {' AND '.join(where)}"
    if sort_by:
        sql += f" ORDER BY {_SORT[sort_by]}"
    with connect() as conn:
        return _rows(conn, sql, tuple(params))


def list_contacts(company_id: int) -> list[dict]:
    """Contacts at one company, decision makers first."""
    with connect() as conn:
        rows = _rows(conn, "SELECT id AS contact_id, name, title, email, is_decision_maker FROM contacts "
                           "WHERE company_id=? ORDER BY is_decision_maker DESC", (company_id,))
    for r in rows:
        r["is_decision_maker"] = bool(r["is_decision_maker"])
    return rows


def recent_activities(company_id: int, limit: int = 10) -> list[dict]:
    """Latest activities across all of a company's deals, newest first."""
    with connect() as conn:
        return _rows(conn, "SELECT a.deal_id, a.type, a.summary, a.created_at AS date, a.created_by AS by "
                           "FROM activities a JOIN deals d ON d.id=a.deal_id WHERE d.company_id=? "
                           "ORDER BY a.created_at DESC, a.id DESC LIMIT ?", (company_id, limit))


def add_activity(deal_id: int, type: str, summary: str, on: date, by: str,
                 next_step: str | None = None, next_step_date: str | None = None) -> int:
    """Record an activity, bump the deal's last activity date and optionally set its next step.

    All in one transaction. Returns the new activity id.
    """
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO activities (deal_id, type, summary, created_at, created_by) VALUES (?,?,?,?,?)",
            (deal_id, type, summary, on.isoformat(), by),
        )
        conn.execute("UPDATE deals SET last_activity_at=? WHERE id=?", (on.isoformat(), deal_id))
        if next_step:
            conn.execute("UPDATE deals SET next_step=?, next_step_date=? WHERE id=?",
                         (next_step, next_step_date, deal_id))
        return cur.lastrowid


_UPDATABLE = {"stage", "value", "close_date", "next_step", "next_step_date"}


def update_deal(deal_id: int, changes: dict) -> None:
    """Set the given deal fields in one transaction. Keys must be deal columns in _UPDATABLE."""
    bad = set(changes) - _UPDATABLE
    if bad:
        raise ValueError(f"not updatable: {sorted(bad)}")
    with connect() as conn:
        conn.execute(f"UPDATE deals SET {', '.join(f'{k}=?' for k in changes)} WHERE id=?",
                     (*changes.values(), deal_id))


# --------------------------------------------------------------------------- seed

OWNERS = ["Priya Shah", "Marcus Lee", "Sofia Alvarez"]

COMPANIES = [
    ("Acme Logistics", "Logistics", "200-500", "Dubai"),
    ("Brightline Health", "Healthcare", "500-1000", "Abu Dhabi"),
    ("Cedar & Stone Realty", "Real Estate", "50-200", "Dubai"),
    ("Dunmore Retail Group", "Retail", "1000+", "Riyadh"),
    ("Everpeak Fitness", "Fitness", "50-200", "Dubai"),
    ("Falcon Air Services", "Aviation", "200-500", "Sharjah"),
    ("Greenfield Organics", "Food & Beverage", "50-200", "Muscat"),
    ("Harbor Point Capital", "Finance", "50-200", "Dubai"),
    ("Ironclad Security", "Security", "200-500", "Doha"),
    ("Juniper Learning", "Education", "50-200", "Abu Dhabi"),
    ("Kestrel Motors", "Automotive", "500-1000", "Dubai"),
    ("Lumen Dental Clinics", "Healthcare", "50-200", "Dubai"),
    ("Meridian Hotels", "Hospitality", "1000+", "Dubai"),
    ("Northwind Freight", "Logistics", "500-1000", "Jeddah"),
    ("Oasis Water Co", "Utilities", "200-500", "Al Ain"),
    ("Pinnacle Legal", "Legal", "10-50", "Dubai"),
    ("Quartz Interiors", "Design", "10-50", "Dubai"),
    ("Redwood Construction", "Construction", "500-1000", "Abu Dhabi"),
    ("Sapphire Beauty", "E-commerce", "50-200", "Dubai"),
    ("Tidewater Marine", "Marine", "200-500", "Fujairah"),
    ("Urbanly Coworking", "Real Estate", "50-200", "Dubai"),
    ("Vantage Telecom", "Telecom", "1000+", "Kuwait City"),
    ("Willow Pet Supplies", "E-commerce", "10-50", "Dubai"),
    ("Zenith Pharma", "Pharma", "500-1000", "Riyadh"),
]

FIRST = ["Aisha", "Omar", "Layla", "James", "Fatima", "Daniel", "Noor", "Ravi", "Elena", "Yusuf",
         "Hannah", "Karim", "Mei", "Tariq", "Sara", "Lucas", "Zainab", "Arjun", "Chloe", "Hamad"]
LAST = ["Haddad", "Patel", "Rahman", "Carter", "Nasser", "Kim", "Farouk", "Iyer", "Rossi", "Saleh",
        "Brooks", "Mansour", "Chen", "Aziz", "Lopez", "Grant", "Hussain", "Menon", "Dubois", "Khalifa"]
TITLES = [("CEO", 1), ("COO", 1), ("CFO", 1), ("Head of Operations", 1), ("Marketing Director", 0),
          ("IT Manager", 0), ("Procurement Lead", 0), ("Sales Ops Manager", 0)]

PRODUCTS = ["Annual platform licence", "Fleet tracking rollout", "Customer portal build",
            "Analytics add-on", "Support retainer", "Multi-site expansion", "Pilot program",
            "Data migration project", "Training package", "Renewal + upsell"]

STAGES_OPEN = ["lead", "qualified", "proposal", "negotiation"]
NEXT_STEPS = ["Send revised proposal", "Book demo with ops team", "Follow up on pricing questions",
              "Security review call", "Get sign-off from CFO", "Share case study", "Schedule kickoff",
              "Confirm budget for Q1", "Send contract for signature"]
ACTIVITY_SUMMARIES = {
    "call": ["Discovery call, interested in reporting features", "Pricing call, asked about volume discount",
             "Check-in call, decision delayed to next month", "Call with ops lead, positive on timeline"],
    "email": ["Sent proposal PDF", "Sent follow-up with case study", "Replied to security questionnaire",
              "Shared pricing breakdown"],
    "meeting": ["Onsite demo with 4 stakeholders", "Workshop on requirements", "Contract review meeting"],
    "note": ["Champion is moving roles soon - risk", "Competitor (legacy vendor) also in evaluation",
             "Budget confirmed by finance", "Procurement needs 3 quotes"],
}


def _iso(d: date) -> str:
    return d.isoformat()


def reset_and_seed(path: Path | str | None = None, seed: int = 42) -> dict[str, int]:
    """Drop everything and load a fresh, deterministic demo dataset."""
    rng = random.Random(seed)
    t = today()
    path = Path(path or DEFAULT_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()

    with connect(path) as conn:
        conn.executescript(SCHEMA)

        for i, (name, industry, size, city) in enumerate(COMPANIES, start=1):
            conn.execute("INSERT INTO companies VALUES (?,?,?,?,?)", (i, name, industry, size, city))

        contact_id = 0
        contacts_by_company: dict[int, list[int]] = {}
        for company_id, (cname, *_rest) in enumerate(COMPANIES, start=1):
            domain = cname.lower().replace("&", "and").split()[0] + ".example"
            for title, dm in rng.sample(TITLES, rng.randint(1, 3)):
                contact_id += 1
                first, last = rng.choice(FIRST), rng.choice(LAST)
                conn.execute(
                    "INSERT INTO contacts VALUES (?,?,?,?,?,?)",
                    (contact_id, company_id, f"{first} {last}", title,
                     f"{first.lower()}.{last.lower()}@{domain}", dm),
                )
                contacts_by_company.setdefault(company_id, []).append(contact_id)

        deal_id = 0
        activity_id = 0
        for company_id in range(1, len(COMPANIES) + 1):
            for _ in range(rng.choice([1, 1, 2, 2, 3])):
                deal_id += 1
                stage = rng.choices(STAGES_OPEN + ["won", "lost"], weights=[3, 3, 3, 2, 2, 1])[0]
                value = rng.choice([4, 6, 8, 12, 15, 18, 24, 30, 45, 60, 85]) * 1000
                created = t - timedelta(days=rng.randint(20, 160))
                if stage in ("won", "lost"):
                    close = created + timedelta(days=rng.randint(15, 60))
                    close = min(close, t - timedelta(days=1))
                    last_act = close
                    next_step, next_step_date = None, None
                else:
                    # mix of healthy, stale and overdue deals
                    last_act = t - timedelta(days=rng.choice([1, 1, 2, 2, 3, 3, 4, 5, 6, 8, 12, 19, 33]))
                    close = t + timedelta(days=rng.choice([-6] + list(range(5, 76))))
                    if rng.random() < 0.1:
                        next_step, next_step_date = None, None
                    else:
                        next_step = rng.choice(NEXT_STEPS)
                        next_step_date = _iso(t + timedelta(days=rng.choice([-5, -2] + list(range(1, 15)))))
                last_act = max(last_act, created)
                contact = rng.choice(contacts_by_company[company_id])
                conn.execute(
                    "INSERT INTO deals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (deal_id, company_id, contact, rng.choice(PRODUCTS), value, stage,
                     rng.choice(OWNERS), _iso(created), _iso(close), _iso(last_act),
                     next_step, next_step_date),
                )
                # activity history ending exactly at last_activity_at
                n = rng.randint(1, 5)
                span = max((last_act - created).days, 1)
                days = sorted(rng.sample(range(span + 1), min(n, span + 1)))
                days[-1] = span
                for d in days:
                    activity_id += 1
                    typ = rng.choice(list(ACTIVITY_SUMMARIES))
                    conn.execute(
                        "INSERT INTO activities VALUES (?,?,?,?,?,?)",
                        (activity_id, deal_id, typ, rng.choice(ACTIVITY_SUMMARIES[typ]),
                         _iso(created + timedelta(days=d)), rng.choice(OWNERS)),
                    )

        # Guaranteed showcase scenarios so the demo prompts always have a good answer.
        conn.execute(
            "UPDATE deals SET value=48000, stage='negotiation', last_activity_at=?, close_date=?, "
            "next_step='Send contract for signature', next_step_date=? WHERE id=1",
            (_iso(t - timedelta(days=18)), _iso(t + timedelta(days=10)), _iso(t - timedelta(days=6))),
        )
        conn.execute(
            "UPDATE deals SET value=72000, stage='proposal', last_activity_at=?, close_date=?, "
            "next_step=NULL, next_step_date=NULL WHERE id=(SELECT MIN(id) FROM deals WHERE company_id=13)",
            (_iso(t - timedelta(days=15)), _iso(t - timedelta(days=4))),
        )
        conn.execute("UPDATE activities SET created_at=(SELECT last_activity_at FROM deals d WHERE d.id=activities.deal_id) "
                     "WHERE id IN (SELECT MAX(id) FROM activities GROUP BY deal_id)")

        counts = {tbl: conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                  for tbl in ("companies", "contacts", "deals", "activities")}
    return counts


if __name__ == "__main__":
    print(reset_and_seed())
