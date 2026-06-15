# MML Audit MCP Server — Demo Script

A read-only MCP server over the static-audit artifacts + the live `.m` tree.
In the meeting, Claude Code stands in for **Osprey** — in production the same
server registers in Osprey's MCP multiplexer instead of `.mcp.json`.

---

## Setup (one time, before the meeting)

1. The server and config are already in the repo:
   - `audit/mcp/server.py` — the server (stdlib-only, no install)
   - `.mcp.json` — registers it for Claude Code
2. **Restart Claude Code** in this directory so it picks up `.mcp.json`.
   Confirm with `/mcp` — you should see **mml-audit** with 20 tools.
3. Dry-run the CLI fallback once (below) and **screen-record it** as a safety net.

---

## The live demo (in Claude Code) — 3 questions, ~3 min

Ask these in plain English. Claude picks the tool, calls the server, answers
grounded in the data. Each one makes a distinct point.

**1. The data model + cross-reference** — *"the MML's structure is now queryable"*
> *What fields does the BPM family have, and which files reference it?*

Exercises `get_family` + `files_for_family`. Watch for: 53 fields, 360
referencing files, and the **three separated tiers** (verified facts vs. general
physics vs. open operator questions). Point out the tiering out loud — the agent
never states a guess as fact.

**2. The call graph** — *"the thing you can't paste into a prompt"*
> *Who calls getmachineconfig, and how often?*

Exercises `callers_of`. The point: this comes from a **405,809-callsite / 69 MB**
graph. No agent could ingest that; it *queries* instead. 40 callers, weighted.

**3. Live source** — *"reads the current tree, not a frozen copy"*
> *Show me the first 40 lines of getmachineconfig.m.*

Exercises `read_source`. The browser ships a frozen, compressed copy; the server
reads the file on disk **now**, path-confined to the repo.

*(Optional 4th, the overview opener: "List the AO families with their device
counts." → `list_families`, 37 families.)*

---

## CLI fallback (no agent — if the live demo hiccups)

Runs the same tools directly. Use this if `/mcp` won't connect on the meeting
machine. (Pipe through `python3 -m json.tool` or just let it print.)

```bash
python3 audit/mcp/server.py list                                  # the 5 tools
python3 audit/mcp/server.py call list_families                    # 37 families
python3 audit/mcp/server.py call get_family BPM                   # 3 tiers
python3 audit/mcp/server.py call files_for_family BPM             # 360 files
python3 audit/mcp/server.py call callers_of getmachineconfig.m    # 40 callers
python3 audit/mcp/server.py call read_source mml/getmachineconfig.m 1 40
```

---

## Talking points to land

- **Same knowledge base, two views.** `browser.html` is the human view; this MCP
  server is the agent view. Both read the *same* `audit/data/` artifacts — no
  duplication, no drift.
- **~1 day of work.** Zero dependencies, ~600 lines. The full mapping-table
  surface — **all 20 tools** — is now built (the 3-question demo above exercises
  just 5 of them). The other 15 cover the rest of the data model and inventory:
  - **Data model:** `list_ad_paths`/`get_ad_path` (the AD half — Energy, MCF,
    OperationalMode), `list_modes`/`get_mode` (the 16 operational modes),
    `list_groups`/`get_group` (the 82 MemberOf groups, with the reverse
    group→members index), `get_verb` (verb usage + annotation tiers).
  - **Inventory:** `stats`, `get_file`/`search_files`, `list_subsystems`,
    `list_workflows`/`get_workflow`.
  - **Relationships & source:** `callees_of` (forward call edges),
    `search_source` (full-text grep over the live tree).
  Every one is a thin function over an already-loaded artifact, same pattern as
  the original 5 — and `get_verb`/`get_ad_path` preserve the verified-vs-conjecture
  tiering so an agent never states a guess as fact.
- **Plugs into Osprey.** MCP is exactly how Osprey ingests external knowledge.
  Swap `.mcp.json` for Osprey's manifest and it's the production integration.
- **Read-only by design.** No write tools — writes belong to Osprey's hook-based
  approval chain, never a knowledge server.

---

## If someone asks "how do I add a tool?"

Add one entry to the `TOOLS` registry in `server.py`: a handler function over an
already-loaded artifact + a description + an input schema. No protocol code to
touch. That's the whole extension story.
