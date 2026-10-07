"""CRM MCP server: lets Claude (or any MCP client) work a sales pipeline.

Design rules (see DESIGN.md for the reasoning):
  * Few, task-shaped tools instead of one tool per table.
  * Math and rules live in logic.py; the model only reasons and writes.
  * Errors tell the model how to fix its call (suggestions, valid values).
  * Writes are explicit: update_deal previews until confirm=true.
  * Every call is logged with latency and output size.
"""

from __future__ import annotations

import argparse
import functools
import json
import logging
import os
import sys
import time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import db, logic

log = logging.getLogger("crm_mcp")

LOG_PATH = Path(os.environ.get("CRM_LOG_PATH", Path(__file__).resolve().parents[2] / "logs" / "tool_calls.jsonl"))
READ_ONLY = os.environ.get("CRM_READ_ONLY", "0") == "1"

INSTRUCTIONS = """\
CRM for a B2B sales team. All money values are whole US dollars; dates are ISO (YYYY-MM-DD).
- "What should I focus on / what's at risk" -> get_deals_needing_attention (reasons are pre-computed; quote them, don't recompute).
- Totals, forecast, win rate -> get_pipeline_summary. Never add up deals yourself.
- Before writing to a deal, find its id with search_deals or get_account. Never guess ids.
- update_deal returns a preview unless confirm=true. Show the preview to the user and only confirm after they agree.
- Drafting emails or call notes is your job: fetch context with get_account, then write.
"""

mcp = MCPServer(name="crm", title="Sales CRM", version="1.0.0", instructions=INSTRUCTIONS)

# --------------------------------------------------------------------------- observability

_stats: dict[str, dict[str, float]] = defaultdict(lambda: {"calls": 0, "errors": 0, "total_ms": 0.0, "total_chars": 0})
_recent: deque[dict] = deque(maxlen=50)


def _record(entry: dict) -> None:
    s = _stats[entry["tool"]]
    s["calls"] += 1
    s["errors"] += 0 if entry["ok"] else 1
    s["total_ms"] += entry["duration_ms"]
    s["total_chars"] += entry["result_chars"]
    _recent.append(entry)
    log.info(json.dumps(entry))
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:  # read-only filesystem on some hosts: stderr log is enough
        pass


def observed(fn):
    """Log every tool call: args, success, latency, and output size (~tokens)."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        ok, error, result = True, None, None
        try:
            result = fn(*args, **kwargs)
            return result
        except ToolError as e:
            ok, error = False, str(e)
            raise
        except Exception as e:  # unexpected: still log it, then let the SDK hide details from the client
            ok, error = False, f"{type(e).__name__}: {e}"
            raise
        finally:
            chars = len(json.dumps(result, default=str)) if result is not None else 0
            _record({
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "tool": fn.__name__,
                "args": kwargs,
                "ok": ok,
                "error": error,
                "duration_ms": round((time.perf_counter() - start) * 1000, 2),
                "result_chars": chars,
                "est_tokens": chars // 4,
            })

    return wrapper


# --------------------------------------------------------------------------- helpers

def _resolve_owner(owner: str | None) -> str | None:
    if not owner:
        return None
    owners = db.list_owners()
    matches = [o for o in owners if owner.lower() in o.lower()]
    if len(matches) == 1:
        return matches[0]
    raise ToolError(f"Owner '{owner}' is {'ambiguous' if matches else 'unknown'}. Valid owners: {', '.join(owners)}.")


def _resolve_company(company: str) -> dict:
    if company.strip().isdigit():
        row = db.get_company(int(company))
        if row:
            return row
    names = db.list_company_names()
    exact = [n for n in names if n.lower() == company.lower()]
    partial = [n for n in names if company.lower() in n.lower()]
    pick = exact or (partial if len(partial) == 1 else [])
    if pick:
        return db.get_company_by_name(pick[0])
    hint = logic.suggest(company, names)
    raise ToolError(
        f"No single company matches '{company}'."
        + (f" Did you mean: {', '.join(hint)}?" if hint else " Use search_deals to browse companies.")
    )


def _get_deal(deal_id: int) -> dict:
    deal = db.get_deal(deal_id)
    if not deal:
        raise ToolError(f"Deal {deal_id} does not exist. Find valid ids with search_deals or get_account.")
    return deal


def _compact(deal: dict, today: date) -> dict:
    return {
        "deal_id": deal["id"],
        "company": deal["company"],
        "title": deal["title"],
        "value": deal["value"],
        "stage": deal["stage"],
        "owner": deal["owner"],
        "close_date": deal["close_date"],
        "days_since_activity": logic.days_between(deal["last_activity_at"], today),
        "next_step": deal["next_step"],
        "next_step_date": deal["next_step_date"],
    }


def _parse_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ToolError(f"{field} must be an ISO date like {db.today().isoformat()}, got '{value}'.") from None


def _require_writable() -> None:
    if READ_ONLY:
        raise ToolError("This CRM server is running in read-only mode; writes are disabled. Tell the user.")


READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)

# --------------------------------------------------------------------------- read tools


@mcp.tool(annotations=READ)
@observed
def search_deals(
    company: str | None = None,
    stage: Literal["lead", "qualified", "proposal", "negotiation", "won", "lost"] | None = None,
    owner: str | None = None,
    min_value: int | None = None,
    inactive_days: int | None = None,
    closing_within_days: int | None = None,
    include_closed: bool = False,
    sort_by: Literal["value", "last_activity", "close_date"] = "value",
    limit: int = 15,
) -> dict[str, Any]:
    """Find deals with filters. Returns compact rows with deal_id for follow-up calls.

    company: partial name match. owner: rep name or first name.
    inactive_days: only deals with no activity for at least this many days.
    closing_within_days: only deals whose close_date is within the next N days.
    Won/lost deals are excluded unless include_closed=true or stage is won/lost.
    """
    today = db.today()
    limit = max(1, min(limit, 50))
    rows = db.find_deals(
        company=company,
        stages=[stage] if stage else None,
        exclude_stages=None if stage or include_closed else ["won", "lost"],
        owner=_resolve_owner(owner),
        min_value=min_value,
        inactive_since=today - timedelta(days=inactive_days) if inactive_days is not None else None,
        close_between=(today, today + timedelta(days=closing_within_days)) if closing_within_days is not None else None,
        sort_by=sort_by,
    )
    out: dict[str, Any] = {"total_matches": len(rows), "returned": min(len(rows), limit),
                           "deals": [_compact(r, today) for r in rows[:limit]]}
    if len(rows) > limit:
        out["note"] = f"{len(rows) - limit} more not shown. Narrow with owner, stage or min_value, or raise limit (max 50)."
    if not rows:
        out["note"] = "No deals match. Try removing a filter."
    return out


@mcp.tool(annotations=READ)
@observed
def get_account(company: str) -> dict[str, Any]:
    """Everything about one company: contacts (with decision makers flagged), all deals, and the 10 latest activities.

    company: name (partial is fine if unique) or numeric company id. Use this before drafting any email or call plan.
    """
    today = db.today()
    co = _resolve_company(company)
    contacts = db.list_contacts(co["id"])
    deals = db.find_deals(company_id=co["id"], sort_by="value")
    acts = db.recent_activities(co["id"], limit=10)
    deal_rows = []
    for d in deals:
        row = _compact(d, today)
        row.pop("company")
        att = logic.assess_deal(d, today)
        if att.reasons:
            row["attention"] = att.reasons
        deal_rows.append(row)
    return {"company": co, "contacts": contacts, "deals": deal_rows, "recent_activity": acts}


@mcp.tool(annotations=READ)
@observed
def get_pipeline_summary(owner: str | None = None) -> dict[str, Any]:
    """Pipeline totals by stage, open pipeline value, weighted forecast and win rate (all pre-computed).

    owner: optional rep name to scope to one person. Use this for any totals - do not sum deals yourself.
    """
    resolved = _resolve_owner(owner)
    deals = db.find_deals(owner=resolved)
    return {"owner": resolved or "all", "as_of": db.today().isoformat(), **logic.pipeline_summary(deals)}


@mcp.tool(annotations=READ)
@observed
def get_deals_needing_attention(owner: str | None = None, min_value: int = 0, limit: int = 10) -> dict[str, Any]:
    """Open deals that need action, ranked by priority, each with plain-English reasons.

    Rules: stale (no activity beyond the stage limit), close date passed, no next step, next step overdue.
    High-value deals (>= $25,000) rank higher. Read crm://playbook for the exact thresholds.
    """
    today = db.today()
    limit = max(1, min(limit, 50))
    deals = db.find_deals(exclude_stages=["won", "lost"], min_value=min_value, owner=_resolve_owner(owner))
    flagged = []
    for d in deals:
        att = logic.assess_deal(d, today)
        if att.reasons:
            flagged.append({**_compact(d, today), "priority": att.priority, "score": att.score, "reasons": att.reasons})
    flagged.sort(key=lambda x: (-x["score"], -x["value"]))
    at_risk_value = sum(f["value"] for f in flagged)
    return {
        "as_of": today.isoformat(),
        "flagged_deals": len(flagged),
        "value_at_risk": at_risk_value,
        "deals": flagged[:limit],
        **({"note": f"{len(flagged) - limit} more flagged deals not shown."} if len(flagged) > limit else {}),
    }


# --------------------------------------------------------------------------- write tools


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False))
@observed
def log_activity(
    deal_id: int,
    type: Literal["call", "email", "meeting", "note"],
    summary: str,
    next_step: str | None = None,
    next_step_date: str | None = None,
    logged_by: str = "Claude",
) -> dict[str, Any]:
    """Record a call/email/meeting/note on a deal and optionally set the next step.

    This updates the deal's last activity date. next_step_date must be today or later (YYYY-MM-DD).
    """
    _require_writable()
    today = db.today()
    summary = summary.strip()
    if not summary:
        raise ToolError("summary is empty. Describe what happened in one or two sentences.")
    if len(summary) > 1000:
        raise ToolError("summary is over 1000 characters. Shorten it to the key facts.")
    if next_step_date:
        nsd = _parse_date(next_step_date, "next_step_date")
        if nsd < today:
            raise ToolError(f"next_step_date {nsd} is in the past. Use {today.isoformat()} or later.")
        if not next_step:
            raise ToolError("next_step_date was given without next_step. Say what the next step is.")
    deal = _get_deal(deal_id)
    if deal["stage"] in ("won", "lost") and next_step:
        raise ToolError(f"Deal {deal_id} is {deal['stage']}; it can't have a next step. Log the activity without one.")
    activity_id = db.add_activity(deal_id, type, summary, on=today, by=logged_by,
                                  next_step=next_step, next_step_date=next_step_date)
    updated = _get_deal(deal_id)
    return {"activity_id": activity_id, "deal": _compact(updated, today),
            "remaining_issues": logic.assess_deal(updated, today).reasons}


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True))
@observed
def update_deal(
    deal_id: int,
    stage: Literal["lead", "qualified", "proposal", "negotiation", "won", "lost"] | None = None,
    value: int | None = None,
    close_date: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Change a deal's stage, value or close date.

    Two-step by design: call with confirm=false (default) to get a preview of the change,
    show it to the user, and call again with confirm=true only after they agree.
    """
    _require_writable()
    today = db.today()
    if stage is None and value is None and close_date is None:
        raise ToolError("Nothing to change. Pass at least one of stage, value, close_date.")
    if value is not None and value <= 0:
        raise ToolError("value must be a positive whole-dollar amount.")
    deal = _get_deal(deal_id)
    changes: dict[str, Any] = {}
    if stage and stage != deal["stage"]:
        if deal["stage"] in ("won", "lost"):
            raise ToolError(f"Deal {deal_id} is already {deal['stage']} and closed. Create a new deal instead of reopening.")
        changes["stage"] = stage
    if value is not None and value != deal["value"]:
        changes["value"] = value
    if close_date:
        cd = _parse_date(close_date, "close_date")
        if cd < today and (stage or deal["stage"]) not in ("won", "lost"):
            raise ToolError(f"close_date {cd} is in the past for an open deal. Pick {today.isoformat()} or later.")
        if cd.isoformat() != deal["close_date"]:
            changes["close_date"] = cd.isoformat()
    if changes.get("stage") in ("won", "lost") and "close_date" not in changes:
        changes["close_date"] = today.isoformat()
    if not changes:
        return {"deal_id": deal_id, "status": "no_change", "message": "Deal already has these values."}

    diff = {k: {"from": deal[k], "to": v} for k, v in changes.items()}
    if not confirm:
        new_stage = changes.get("stage", deal["stage"])
        new_value = changes.get("value", deal["value"])
        return {
            "status": "preview",
            "deal_id": deal_id,
            "company": deal["company"],
            "changes": diff,
            "forecast_impact": logic.weighted(new_value, new_stage) - logic.weighted(deal["value"], deal["stage"]),
            "next": "Show this to the user. If they approve, call update_deal again with the same arguments and confirm=true.",
        }
    closing = {"next_step": None, "next_step_date": None} if changes.get("stage") in ("won", "lost") else {}
    db.update_deal(deal_id, {**changes, **closing})
    updated = _get_deal(deal_id)
    return {"status": "updated", "changes": diff, "deal": _compact(updated, today)}


# --------------------------------------------------------------------------- resource + prompt


@mcp.resource("crm://playbook", name="playbook", title="Sales playbook rules", mime_type="text/markdown")
def playbook() -> str:
    """The exact rules the attention and forecast tools apply."""
    stale = "\n".join(f"- {s}: {d} days" for s, d in logic.STALE_AFTER_DAYS.items())
    prob = "\n".join(f"- {s}: {int(p * 100)}%" for s, p in logic.STAGE_PROBABILITY.items())
    return (
        "# Sales playbook\n\n## Stale after (no activity)\n" + stale +
        "\n\n## Forecast probability by stage\n" + prob +
        f"\n\n## High value\nDeals >= ${logic.HIGH_VALUE:,} get extra priority when flagged.\n"
        "\n## Hygiene\nEvery open deal needs a next step with a date, and a close date in the future.\n"
    )


@mcp.prompt(title="Weekly pipeline review")
def weekly_pipeline_review(owner: str = "") -> str:
    """A ready-made Monday pipeline review for one rep or the whole team."""
    who = f"for {owner}" if owner else "for the whole team"
    return (
        f"Run a weekly pipeline review {who}.\n"
        "1. Call get_pipeline_summary and report open pipeline, weighted forecast and win rate in 3 lines.\n"
        "2. Call get_deals_needing_attention and list the top 5 with their reasons.\n"
        "3. For the top 2, call get_account and draft a short, specific follow-up email to the decision maker.\n"
        "4. End with 3 concrete actions for this week. Do not change any data."
    )


# --------------------------------------------------------------------------- HTTP extras


@mcp.custom_route("/", methods=["GET"], include_in_schema=False)
async def landing(_request):
    """Human-facing page: what this server is and how to connect Claude to it."""
    from starlette.responses import HTMLResponse
    return HTMLResponse((Path(__file__).parent / "landing.html").read_text())


@mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health(_request):
    from starlette.responses import JSONResponse
    return JSONResponse({"status": "ok", "read_only": READ_ONLY, "today": db.today().isoformat()})


@mcp.custom_route("/stats", methods=["GET"], include_in_schema=False)
async def stats(_request):
    from starlette.responses import JSONResponse
    per_tool = {
        name: {
            "calls": int(s["calls"]),
            "errors": int(s["errors"]),
            "avg_ms": round(s["total_ms"] / s["calls"], 2) if s["calls"] else 0,
            "avg_est_tokens": int(s["total_chars"] / 4 / s["calls"]) if s["calls"] else 0,
        }
        for name, s in sorted(_stats.items())
    }
    return JSONResponse({"tools": per_tool, "recent_calls": list(_recent)[-20:]})


class BearerAuth:
    """Optional shared-token auth for the /mcp endpoint (set CRM_MCP_TOKEN)."""

    def __init__(self, app, token: str):
        self.app, self.token = app, token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/mcp"):
            headers = dict(scope.get("headers") or [])
            if headers.get(b"authorization", b"").decode() != f"Bearer {self.token}":
                from starlette.responses import JSONResponse
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_http_app(host: str = "127.0.0.1"):
    app = mcp.streamable_http_app(host=host, stateless_http=True, json_response=True)
    token = os.environ.get("CRM_MCP_TOKEN")
    return BearerAuth(app, token) if token else app


# --------------------------------------------------------------------------- entrypoint


def main() -> None:
    parser = argparse.ArgumentParser(description="CRM MCP server")
    parser.add_argument("--transport", choices=["stdio", "http"], default=os.environ.get("CRM_TRANSPORT", "stdio"))
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--reset", action="store_true", help="Reseed the demo database before starting")
    args = parser.parse_args()

    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if args.reset or os.environ.get("CRM_RESET_ON_START") == "1" or not db.DEFAULT_DB_PATH.exists():
        counts = db.reset_and_seed()
        log.info("seeded demo data: %s", counts)

    if args.transport == "stdio":
        mcp.run("stdio")
    else:
        import uvicorn
        log.info("serving MCP at http://%s:%s/mcp (auth: %s)", args.host, args.port,
                 "bearer token" if os.environ.get("CRM_MCP_TOKEN") else "none")
        uvicorn.run(build_http_app(args.host), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
