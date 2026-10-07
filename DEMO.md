# Demo script: MCP in action

A 3-minute walkthrough that shows two things at once: what Claude can do with a CRM, and the MCP features that make it work. Each scene ends with the line to say about MCP.

**Best setup:** Claude (Code or Desktop) on one side of the screen, the HubSpot Deals board on the other. When Claude changes something, the viewer sees it land in HubSpot. The SQLite demo works for every scene too; only the HubSpot moments are skipped.

---

## Before you record

- [ ] **Fresh data.** HubSpot: `python scripts/seed_hubspot.py --reset`, then wait a minute for HubSpot to index. SQLite: `python -m crm_mcp --reset` (or just restart; `CRM_RESET_ON_START=1` in Docker).
- [ ] **Check it.** `python scripts/check_hubspot.py` should list Meridian Hotels, Acme Logistics and Northwind Freight as the top three.
- [ ] **Warm up.** Ask one throwaway question first. The first HubSpot call loads stages and companies (~4 s); after that answers take ~2 s.
- [ ] **New session.** Start Claude Code in the project folder (or restart Claude Desktop) so the `crm` server is connected. In Claude Code, `/mcp` should show `crm` as connected with 6 tools.
- [ ] **Screen.** Large font, notifications off, HubSpot open on the Deals board.

---

## The script

**0. The setup line (10 s)**

> "This is an MCP server I built. It gives Claude six tools for a sales CRM, here connected to a live HubSpot account. Claude decides which tool to call; the server does the CRM work."

**1. Finding risk (30 s)**

> Which deals over $40k need attention this week, and why?

Claude returns Meridian Hotels ($72k: no activity for 15 days, close date passed, no next step) and Acme Logistics ($48k: 18 days quiet in negotiation, contract step overdue).

*MCP point: "Claude picked `get_deals_needing_attention` from the tool list by itself. The server ranked the deals and wrote the reasons, so Claude didn't read 44 deals to work it out. One call, about 500 tokens, versus ~6,700 to hand Claude every deal and activity."*

**2. Exact numbers (20 s)**

> What's our weighted forecast, and how does Marcus compare to the team?

*MCP point: "The server's instructions tell Claude never to add up deals itself. The totals come from code, so they're exact every time."* On HubSpot, point at the board: the weighted amounts match the ones under each column, because the server uses HubSpot's own stage probabilities.

**3. Writing from real context (30 s)**

> Draft a short follow-up email to the decision maker at Meridian Hotels.

Claude calls `get_account`, picks the contact flagged as decision maker and references the real activity history.

*MCP point: "There's no `draft_email` tool on purpose. The server supplies the context; writing is what the model is good at."*

**4. Recovering from mistakes (15 s)**

> Pull up Meridan Hotel.

The server answers "Did you mean: Meridian Hotels?" and Claude corrects itself.

*MCP point: "Errors are written for the model, so it fixes its own call instead of getting stuck."*

**5. Doing work in the CRM (30 s)**

> I just called Acme. The CFO approved the contract in principle. Log it and set the next step to send the contract tomorrow.

Switch to HubSpot and open the Acme deal: **the call is on the deal's timeline** with the next step set. Back in Claude:

> Does Acme still need attention?

It's gone from the list.

*MCP point: "That's a write tool. The client asked me before running it, and the server labels which tools change data and which only read, so clients and admins can treat them differently."*

**6. Safety on big changes (30 s)**

> Mark the Acme deal as won.

Claude shows a **preview**: stage change and forecast impact (+$4.8k on HubSpot, which uses its own probabilities; +$12k on the SQLite demo). Nothing has changed yet. Say yes; the deal moves to Closed Won on the HubSpot board.

*MCP point: "`update_deal` is marked destructive and only returns a preview until it's called again with confirm. The model can't change data without you seeing the diff."*

**7. One server, many clients (20 s)**

Show the landing page of the hosted copy (the Render URL) and its one-line connect command.

*MCP point: "The same server runs locally for Claude Code and Desktop, or hosted over HTTP so anyone can connect with one command, from Claude.ai too. That's the point of MCP: build the integration once, use it from any client."*

**Close (10 s)**

> "Swapping HubSpot for Salesforce or your own database means rewriting one module. The tools, rules and safety checks stay the same."

---

## MCP features this demo covers

| MCP feature | Where it shows up |
|---|---|
| **Tools** | Six task-shaped tools; Claude chooses them from plain English (scenes 1–6) |
| **Tool annotations** | Tools are labelled read-only or destructive (`update_deal`), so clients can apply different approval rules (scenes 5–6) |
| **Server instructions** | "Never add up deals yourself", "show the preview before confirming" (scenes 2, 6) |
| **Structured results** | JSON with pre-computed reasons and totals (scenes 1–2) |
| **Errors for the model** | "Did you mean…?" (scene 4) |
| **Resources** | `crm://playbook`, the exact rules (bonus below) |
| **Prompts** | `weekly_pipeline_review` (bonus below) |
| **Transports** | stdio locally, Streamable HTTP when hosted (scene 7) |

## Bonus scenes

- **Prompts:** in Claude Code type `/mcp__crm__weekly_pipeline_review` (in Claude Desktop, pick it from the **+** menu). Claude runs the full Monday review: summary, top risks, two draft emails, three actions.
- **Resources:** attach the playbook (in Claude Code type `@` and pick `crm://playbook`; in Claude Desktop use the **+** menu), then ask *"Why was Meridian flagged?"* Claude explains it with the actual rules.
- **Observability:** open `/stats` on the hosted copy, or `logs/tool_calls.jsonl` locally. Every tool call with its latency and token size.
- **More questions:** "Which proposals have gone quiet for more than two weeks?" · "Show me everything closing in the next 30 days, sorted by date." · "Run the weekly pipeline review for Priya."

## If something goes wrong on camera

- **A HubSpot answer looks out of date right after a change:** HubSpot search takes a few seconds to index. Ask again; deals Claude just changed are already handled.
- **The first answer is slow:** that's the warm-up (see the checklist), or Render's free tier waking up (~30–60 s). Open the hosted URL a minute before you start.
- **The data story has drifted** (e.g. "19 days" instead of "18"): demo dates count from the day you loaded them. Re-run the reset.
