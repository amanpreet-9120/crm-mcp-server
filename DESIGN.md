# Design notes

Why this server looks the way it does. Every point here is a trade-off you'll face on any MCP server, not just a CRM.

## 1. Six task-shaped tools, not one per table

The obvious design is CRUD per table: `list_companies`, `get_contact`, `list_deals`, `list_activities`… That pushes the joining, filtering and judging onto the model, which means more calls, more tokens and more chances to get it wrong.

Instead, each tool matches a question a salesperson actually asks:

- "What needs my attention?" → `get_deals_needing_attention`
- "Tell me everything about this account" → `get_account` (company + contacts + deals + recent activity in one call)
- "How's the pipeline?" → `get_pipeline_summary`

**Measured on the demo data** (≈ characters ÷ 4):

| Approach | Tokens into the model |
|---|---|
| `get_deals_needing_attention` (ranked, with reasons) | ~1,250 |
| Dumping all deals + activities for the model to judge | ~9,500 |
| All 6 tool definitions (sent on every request) | ~1,450 |

That's ~7× less context for the most common question, and the answer is already correct.

## 2. Math and rules in code, judgment in the model

| In code (`logic.py`) | In the model |
|---|---|
| Is a deal stale? (limits per stage) | Which deal to tackle first given the user's situation |
| Weighted forecast, win rate, totals | Explaining what the numbers mean |
| Priority score | Writing the follow-up email |
| Date validation, allowed stage moves | Turning "I just called Acme…" into a structured activity |

Rule of thumb: **if two runs should always give the same answer, it belongs in code.** Models are good at language and judgment and unreliable at arithmetic over 40 rows. The server's instructions say it outright: "Never add up deals yourself."

That's also why there is **no `draft_email` tool**. Writing is the model's strength. The server's job is to hand it the right context (`get_account`).

## 3. Errors are written for the model

A bare `404` or a stack trace leaves the agent stuck. Every expected failure says what to do next:

- `get_account("Meridan Hotel")` → *"No single company matches 'Meridan Hotel'. Did you mean: Meridian Hotels?"*
- `get_pipeline_summary(owner="Bob")` → *"Owner 'Bob' is unknown. Valid owners: Marcus Lee, Priya Shah, Sofia Alvarez."*
- `log_activity(next_step_date="2020-01-01")` → *"next_step_date 2020-01-01 is in the past. Use 2026-10-07 or later."*

These go back as `ToolError`s, so the model reads them and retries. Unexpected crashes are logged in full on the server but hidden from the client.

Results do the same: truncated searches say *"22 more not shown. Narrow with owner, stage or min_value…"*

## 4. Writes are explicit and reversible-by-design

- Read tools are marked `read_only_hint`. `update_deal` is marked `destructive_hint`, so clients can ask for approval.
- `update_deal` is **two-step**: without `confirm=true` it returns a preview (diff plus forecast impact) and the instruction to show it to the user. The model can't silently close a deal.
- Business rules are enforced in code, not prompts: you can't reopen a won/lost deal, set a past close date on an open deal, or schedule a next step in the past.
- `CRM_READ_ONLY=1` turns off all writes for public demos.

## 5. State lives in the database, not the conversation

Nothing important is held in the model's context. Every call reads current state from SQLite, and every write is committed before returning. A new conversation, a crashed agent or a second user sees the same truth. The `crm://playbook` resource exposes the rules themselves, so the model can explain *why* a deal was flagged without guessing.

## 6. Observability from day one

Every tool call is logged (JSONL file + stderr) with arguments, success/error, latency and output size. `/stats` aggregates it per tool:

```json
{"get_deals_needing_attention": {"calls": 12, "errors": 0, "avg_ms": 0.9, "avg_est_tokens": 1250}}
```

This answers the questions that decide whether an agent survives in production: which tools does the model actually use, which ones error, and which ones bloat the context (and the bill).

## 7. What I deliberately didn't build

- **No multi-agent setup.** One model with six good tools handles every workflow here. Splitting into "analyst" and "writer" agents would add hand-offs to debug and double the context cost for no gain.
- **No vector database.** The data is structured, so SQL filters beat semantic search.
- **No generic `run_sql` tool.** It's flexible, but the model would have to learn the schema every time and it opens the door to bad writes. Task-shaped tools are safer and cheaper.
- **No OAuth (yet).** A shared bearer token is enough for a demo. For a real client deployment the next step is per-user OAuth so activity is logged under the right rep.

## Tests

`tests/test_tools.py` goes through a real MCP client, so it checks what the model actually sees: tool list and annotations, JSON shapes, the exact error text, the preview/confirm flow, read-only mode and logging. The rules in `logic.py` are tested as plain functions.
