# CRM MCP Server

An MCP server that lets Claude work a B2B sales pipeline. Ask in plain English, and Claude finds at-risk deals, summarises the pipeline, drafts follow-ups from real account context, and logs activity, with a preview-then-confirm step before it changes anything.

It runs locally (stdio) for Claude Desktop / Claude Code, or as a hosted HTTP endpoint anyone can connect to with one command.

**Data sources:** a built-in SQLite demo (default) or a live **HubSpot** account (`CRM_BACKEND=hubspot`). Same tools, same answers.

> **Demo data:** 24 fictional companies, 42 contacts, 44 deals and ~130 activities, regenerated deterministically with dates relative to today, so the demo always has the same story whenever you run it.

---

## What you can ask

| Ask Claude | Tools it uses |
|---|---|
| "Which deals over $40k need attention this week, and why?" | `get_deals_needing_attention` |
| "What's our weighted forecast? How is Marcus doing?" | `get_pipeline_summary` |
| "Draft a follow-up to the decision maker at Meridian Hotels." | `get_account` → Claude writes |
| "I just called Acme, contract approved in principle. Log it and set next step to send the contract tomorrow." | `log_activity` |
| "Mark the Acme deal as won." | `update_deal` (preview → you approve → confirm) |
| "Run the weekly pipeline review." | `weekly_pipeline_review` prompt |

See **[DEMO.md](DEMO.md)** for a 2-minute demo script.

## Tools

| Tool | Type | What it does |
|---|---|---|
| `search_deals` | read | Filter by company, stage, owner, value, inactivity, closing window. Says when results are truncated. |
| `get_account` | read | One company: contacts (decision makers flagged), deals with issues, last 10 activities. |
| `get_pipeline_summary` | read | Totals by stage, weighted forecast, win rate. Pre-computed. |
| `get_deals_needing_attention` | read | Ranked open deals with plain-English reasons (stale, overdue, no next step). |
| `log_activity` | write | Adds a call/email/meeting/note, updates last activity, sets the next step. |
| `update_deal` | write | Change stage/value/close date. **Returns a preview unless `confirm=true`.** |

Also: resource `crm://playbook` (the exact rules), prompt `weekly_pipeline_review`, and HTTP routes `/` (landing page with connect instructions and live usage), `/health` and `/stats` (per-tool calls, errors, latency, output tokens).

---

## Run it

Requires Python 3.10+. On macOS the system `python3` is often 3.9; use e.g. `python3.13` from Homebrew.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest                       # 26 tests, ~1s
```

### Connect to Claude Code (local)

```bash
claude mcp add crm -- "$(pwd)/.venv/bin/python" -m crm_mcp
```

### Connect to Claude Desktop (local)

Settings → Developer → Edit Config, add:

```json
{
  "mcpServers": {
    "crm": {
      "command": "/ABSOLUTE/PATH/TO/crm-mcp-server/.venv/bin/python",
      "args": ["-m", "crm_mcp"]
    }
  }
}
```

### Run as an HTTP server

```bash
python -m crm_mcp --transport http --port 8000 --reset
# MCP endpoint: http://localhost:8000/mcp   stats: http://localhost:8000/stats
python scripts/smoke_http.py http://localhost:8000/mcp     # end-to-end check
```

---

## Deploy (public link)

The included `Dockerfile` runs anywhere that runs containers. With **Render**: push this folder to GitHub → New → Blueprint → pick the repo (it reads `render.yaml`, which deploys an open, read-only demo). **Railway** and **Fly.io** also detect the Dockerfile directly.

Then anyone can connect:

```bash
claude mcp add --transport http crm https://YOUR-APP.onrender.com/mcp
```

| Env var | Default | Purpose |
|---|---|---|
| `CRM_MCP_TOKEN` | unset | If set, `/mcp` requires `Authorization: Bearer <token>`. Clients add `--header "Authorization: Bearer <token>"`. |
| `CRM_READ_ONLY` | `0` | `1` disables both write tools (safe public demo). |
| `CRM_RESET_ON_START` | `0` (`1` in Docker) | Reseed demo data on every start. |
| `CRM_DB_PATH` | `data/crm.db` | SQLite file location. |
| `PORT` | `8000` | HTTP port (set automatically by most hosts). |

> For a public client demo, either set a token and share it privately, or leave it open with `CRM_READ_ONLY=1`. Claude.ai custom connectors need either no auth or OAuth, so use the open read-only option there.

---

## Use it with HubSpot

The same six tools run against a real HubSpot account. Deals, companies and contacts map to HubSpot objects, and activities are real HubSpot calls, emails, meetings and notes on the deal timeline.

1. **Create a free HubSpot CRM account** (a normal account, not a developer account).
2. **Create a Service Key:** Settings → Integrations → Service Keys. Give it read and write scopes for deals, companies and contacts (`crm.objects.deals.*`, `crm.objects.companies.*`, `crm.objects.contacts.*`), plus read and write schema scopes for the same three (`crm.schemas.deals.*`, `crm.schemas.companies.*`, `crm.schemas.contacts.*`) so the server can read stages and the loader can add its custom properties. If a scope is missing, the check script prints HubSpot's error naming it.
3. **Put the key in `.env`** (copy `.env.example`):
   ```
   CRM_BACKEND=hubspot
   HUBSPOT_SERVICE_KEY=your-key
   ```
4. **Load the demo data and check it:**
   ```bash
   python scripts/seed_hubspot.py          # --reset to replace earlier demo records
   python scripts/check_hubspot.py         # read-only check through a real MCP client
   ```
5. Restart Claude Code / Desktop. The `crm` server now reads and writes HubSpot.

**How fields map:** `dealname`, `amount`, `dealstage`, `closedate` and `hs_next_step` are standard HubSpot properties. Stages in the default pipeline map to lead / qualified / proposal / negotiation / won / lost; stages in custom pipelines are mapped from their win probability. The loader adds three deal properties for the demo (`mcp_sales_rep`, `mcp_last_activity_date`, `mcp_next_step_date`). For a client's real portal, point the `HUBSPOT_*_PROPERTY` settings at their fields, e.g. `HUBSPOT_OWNER_PROPERTY=hubspot_owner_id` for real HubSpot users.

**Good to know:** demo dates are relative to the day you load them, so run `seed_hubspot.py --reset` before a demo. HubSpot search takes a few seconds to index changes; the server overlays deals it just wrote so follow-up questions still see the update. `--reset` only archives records the loader created (to HubSpot's recycle bin).

---

## Project layout

```
src/crm_mcp/
  server.py   MCP tools, resource, prompt, logging, auth, HTTP app
  landing.html  page served at / for people who open the URL in a browser
  logic.py    business rules: staleness, priority, forecast (plain Python, unit-tested)
  db.py       SQLite backend: data access functions, schema, demo seed
  hubspot.py  HubSpot backend: the same functions over the HubSpot CRM API
  hubspot_seed.py  loads the demo dataset into a HubSpot account
tests/        tests that go through a real MCP client, incl. a fake HubSpot API for backend parity
scripts/      HTTP smoke test, HubSpot loader and check
DESIGN.md     why it's built this way
DEMO.md       demo script
```

## Adapting it for a client

HubSpot works out of the box (above). For another system (Salesforce, Pipedrive, Postgres), add a module next to `db.py` and `hubspot.py` with the same functions. All data access goes through about ten plain functions (`find_deals`, `get_deal`, `add_activity`, `update_deal`…), and `server.py` contains no SQL, so the tools, validation and error messages carry over unchanged. Keep those function signatures, and change the thresholds in `logic.py` to match their sales process. The design notes in [DESIGN.md](DESIGN.md) are the part that carries over.
