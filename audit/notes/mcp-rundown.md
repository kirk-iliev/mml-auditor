# From Static Audit to an Agent-Callable API — Meeting Rundown

*One-page brief: what's in `audit/data/`, and what it takes to put an MCP*
*server in front of it so agents (e.g. Osprey / Claude Code) can query the MML.*

---

## TL;DR

The audit already produced a clean, structured **knowledge base** of the MML
(~80 MB of JSON across 21 artifacts). `browser.html` is just *one view* of it,
built for humans. Putting an **MCP server** in front of the same artifacts gives
**agents** the identical information through query tools — no data re-work, no
12 MB bundle, reads the live source tree on demand. **Estimated effort for a
working v1: ~1 day.**

MCP is the relevant target because Osprey (LBNL/ALS's own Claude Code harness)
integrates external knowledge through an **MCP-server multiplexer**. A read-only
MML knowledge server drops straight into that pattern.

---

## What's in `audit/data/` — the four layers

Everything was extracted by static parsing of the 2,424 `.m` files (pipeline
`01`–`11`). Grouped by what question each artifact answers:

### 1. The data model — *what the MML is*

| Artifact | Size | Contains |
|---|---|---|
| `family_schema.json` | 1.2 MB | **37 AO families** (BPM, HCM, QF, …). Each: every field, its distinct values, RHS kinds, source `file:line`, assignment count. |
| `ad_schema.json` | 52 KB | **51 AD machine-config paths** (Energy, MCF, ATModel, OperationalMode, directory layout…) with their possible values + sources. |
| `mode_overrides.json` | 47 KB | **16 operational modes** (TopOff, the person-named Greg/Tom/Christoph modes, the dup `99`…) and the exact per-mode field deltas. |
| `groups.json` | 138 KB | **82 MemberOf groups** — the implicit operational taxonomy the MML iterates over (e.g. `MachineConfig`, `Magnet`). Which families/fields belong to each. |
| `family_meta.json` | 47 KB | Per-family **device counts** + family-level and field-level group memberships. |
| `annotations.json` | 47 KB | Hand-authored **reference text**, 35/37 families + top 19 verbs. Deliberately tiered: `context` (general domain knowledge) vs `unknowns` (open questions for an operator). **Conjecture kept separate from fact by design.** |

### 2. The code inventory — *what the MML has*

| Artifact | Size | Contains |
|---|---|---|
| `file_index.jsonl` | 2.5 MB | **2,424 file records**: path, signature (name/args/outputs), help text, line count, kind (function/script/class_method), mtime, dynamic-dispatch flag, legacy-folder flag. |
| `subsystems.json` | 219 KB | **48 directory clusters** with internal-cohesion scores + file→cluster map. |

### 3. The behavior & relationships — *what the MML does / how it connects*

| Artifact | Size | Contains |
|---|---|---|
| `call_graph_edges.jsonl` | **69 MB** | **405,809 raw callsites** (caller, line, callee, resolved?, intra-file?). The big one — *must* be queried, can't be dumped into a prompt. |
| `call_graph_edges_agg.json` | 1.1 MB | **8,739 weighted edges** (source→target, call count). The usable summary of the above. |
| `call_graph_nodes.json` | 690 KB | **2,424 nodes** with in/out degree, kind, line count. |
| `parenless_call_edges.jsonl` | 2.7 MB | **9,985 paren-less calls** (`checkforao;` style) — MATLAB command syntax the naive parser misses. |
| `family_refs.jsonl` | 1.2 MB | **4,125 (file × family) references** — which files touch which family, with sample lines. |
| `ad_refs.jsonl` | 162 KB | **594 (file × AD-path) references.** |
| `ao_assignments.jsonl` | 1.1 MB | **3,092 raw AO/AD assignment sites** (where the struct is built — 2,799 of them in `alsinit.m` alone). |
| `ao_helper_assignments.jsonl` | 205 KB | **354 helper-injected fields** (from `buildmmlbpmfamily` / `buildmmlcaenfastps`), resolved per call-site. |
| `workflows.json` | 674 KB | **67 inferred workflows** (TopOff, LOCO, Orbit, Inject…) via prefix + keyword + reachability. |

### 4. Pre-computed summaries — *top-line numbers, ready to serve*

`file_index_summary.json`, `ao_assignments_summary.json`,
`call_graph_summary.json`, `parenless_call_summary.json` — the headline counts
(e.g. *60,018 of 405,809 callsites resolved; 97 shadowed callables; 9,868
shadowed callsites*). These power the browser's Overview tab and would back a
single `stats()` tool.

**Plus the live source:** the full `.m` tree (`mml/`, `machine/`, `online/`, …)
sits on disk. The browser ships a frozen, compressed copy of ~1,998 files; the
MCP server reads the **current** files on demand instead.

---

## How a browser tab maps to an MCP tool

The information is the same; only the delivery changes.

| Browser tab | Artifact(s) | MCP tool(s) |
|---|---|---|
| Overview | `*_summary.json` | `stats()` |
| AO families | `family_schema` + `family_meta` + `annotations` | `list_families()`, `get_family(name)` |
| AD paths | `ad_schema`, `ad_refs` | `list_ad_paths()`, `get_ad_path(p)` |
| Operational modes | `mode_overrides` | `list_modes()`, `get_mode(id)` |
| MemberOf groups | `groups` | `list_groups()`, `get_group(name)` |
| Subsystems | `subsystems` | `list_subsystems()` |
| Workflows | `workflows` | `list_workflows()`, `get_workflow(id)` |
| API surface (verbs) | call-graph aggregates + `annotations.verbs` | `get_verb(name)` |
| Files | `file_index` | `get_file(path)`, `search_files(q)` |
| Call relationships | `call_graph_edges_agg` + `parenless` + `family_refs` | `callers_of(f)`, `callees_of(f)`, `files_for_family(name)` |
| Search source / viewer | **live `.m` tree** | `search_source(q)`, `read_source(path, start?, end?)` |

→ **All information-bearing content survives.** What does *not* carry over is
pure presentation (syntax highlighting, clickable line numbers, colored tier
cards, shareable hash URLs) — agents don't need it.

---

## Steps to build the MCP server (v1, ~1 day)

1. **Pick SDK + transport** — official Python MCP SDK / FastMCP. `stdio`
   transport for a co-located agent; `HTTP/SSE` if the agent runs remote and the
   server is the file gateway. *(~30 min scaffold.)*

2. **Load artifacts at startup** — small JSON straight into memory dicts;
   index the 69 MB `call_graph_edges.jsonl` once into an in-memory adjacency map
   (or SQLite) so `callers_of` / `callees_of` are O(1). *(~2 hrs.)*

3. **Define read-only tools** — the 20 tools in the mapping table above. Each
   is a thin function over an already-loaded artifact; the schemas are the work,
   not the logic. *(~3 hrs.)* **Status: DONE — all 20 built and smoke-tested.**
   (`get_verb` is computed live from the call-graph aggregate; the 69 MB raw
   graph is still never loaded — `callees_of` is just the transpose of
   `callers_of` over the same 1.1 MB edge file.)

4. **Source access** — `read_source` / `search_source` over the live tree,
   **path-confined to the repo root** (reject `..` / out-of-root). Replaces the
   browser's embedded bundle; always current. *(~1 hr.)*

5. **Preserve the conjecture tiering** — `get_family` / `get_verb` return
   `verified` / `domain_context` / `needs_operator_input` as **distinct labeled
   fields**, so an agent never presents an open question as fact. *(design
   choice, not extra time.)*

6. **Register & test** — `.mcp.json` for local Claude Code, or Osprey's
   MCP-multiplexer manifest to upstream. Smoke-test by asking an agent real
   questions ("who calls `getmachineconfig`?", "what fields does BPM have?").
   *(~1 hr.)*

**Guardrails from the start:** read-only (no write tools — writes belong to
Osprey's hook-based approval chain, never a knowledge server); path-confined
source reads.

### Why MCP beats the alternatives

- **vs. shipping the JSON to the agent:** the call graph alone is 69 MB —
  doesn't fit in context. Agents must *query*, not ingest.
- **vs. a Skill / file references:** zero-code but breaks on the big files and
  forces every agent to re-learn the schema; fine as a stopgap.
- **vs. a REST API:** more work + hosting, and Osprey/Claude Code already speak
  MCP natively. Only worth it for non-MCP web consumers.

---

## The one-line pitch

> *We already turned the MML black box into a structured knowledge base. The
> human view is `browser.html`. The agent view is one read-only MCP server over
> the same artifacts — about a day of work, and it plugs straight into Osprey.*
