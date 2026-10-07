# 2-minute demo script

Run `python -m crm_mcp --reset` once first (or deploy with `CRM_RESET_ON_START=1`) so the data is fresh. Connect the server to Claude Desktop or Claude Code, then go through these in order.

**1. The hook (30s)**
> Which deals over $40k need attention this week, and why?

Claude returns Meridian Hotels ($72k, close date passed, no next step) and Acme Logistics ($48k, 18 days silent in negotiation, contract step overdue), with reasons. *Point out: it didn't scan 44 deals itself. The server ranked them.*

**2. The numbers (15s)**
> What's our weighted forecast, and how does Marcus compare?

*Point out: totals come from code, so they're exact every time.*

**3. The writing (30s)**
> Draft a short follow-up email to the decision maker at Meridian Hotels about the data migration proposal.

Claude calls `get_account`, picks the decision maker, references the real history.

**4. Logging work (20s)**
> I just called Acme. The CFO approved the contract in principle. Log it and set the next step to send the contract tomorrow.

Then ask again: *"Does Acme still need attention?"* It's gone from the list.

**5. Safety (20s)**
> Mark the Acme deal as won.

Claude shows a **preview** (stage change, +$12k forecast impact) and asks you to confirm. Say yes. *Point out: the model can't change data without the user seeing the diff.*

**6. Under the hood (optional, 15s)**
Open `http://localhost:8000/stats` (HTTP mode) or `logs/tool_calls.jsonl`. Every call, its latency and its token size.

---

### Bonus prompts
- "Run the weekly pipeline review for Priya." (uses the built-in prompt)
- "Which proposals have gone quiet for more than two weeks?"
- "Show me everything closing in the next 30 days, sorted by date."
- Try a typo: "Pull up Meridan Hotel." The server suggests the right name.
