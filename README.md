# MML Auditor

A static-analysis audit of LBNL/ALS's **MATLAB Middle Layer (MML)**, plus two
ways to consume it:

- **`audit/browser.html`** — a standalone 12 MB browseable view for humans
  (open in any browser, no setup).
- **`audit/mcp/server.py`** — a read-only **MCP server** (`mml-audit`) that
  exposes the same knowledge base to an agent like Claude Code or Osprey.

Both read the *same* artifacts in `audit/data/`. The browser is the human view;
the MCP server is the agent view. There's no data duplication or drift.

> Background on what the MML is and what this audit found: see
> [`CLAUDE.md`](CLAUDE.md) and [`audit/notes/findings.md`](audit/notes/findings.md).
> The case for the MCP server: [`audit/notes/mcp-rundown.md`](audit/notes/mcp-rundown.md).

---

## Quick start on a fresh clone (macOS)

### 1. Prerequisites

| Tool | Why | Install |
| --- | --- | --- |
| **git** | clone the repo | preinstalled, or `xcode-select --install` |
| **Python 3** | runs the MCP server (stdlib-only, *no pip installs*) | preinstalled on macOS, or `brew install python` |
| **Claude Code** | the agent that talks to the MCP server | `npm i -g @anthropic-ai/claude-code` or see the Claude Code docs |

Verify Python:

```bash
python3 --version    # any 3.8+ is fine
```

### 2. Clone

```bash
git clone https://github.com/kirk-iliev/mml-auditor.git
cd mml-auditor
```

Everything the server needs is already in the repo — the JSON/JSONL artifacts
in `audit/data/`, including `source_bundle.json.gz`, a ~2.7 MB gzipped archive
of the ~2,000 ALS `.m` source files the server reads. The full 136 MB MML tree
is **not** committed here; its source travels compressed in that bundle (for the
MCP server) and embedded in `browser.html` (for the human view). There's nothing
to build or download.

### 3. Use the browser (no setup)

Just open the file:

```bash
open audit/browser.html
```

### 4. Use the MCP server with Claude Code

The repo ships [`.mcp.json`](.mcp.json), which registers the server for this
project:

```json
{
  "mcpServers": {
    "mml-audit": {
      "command": "python3",
      "args": ["audit/mcp/server.py"]
    }
  }
}
```

Steps:

1. **Start Claude Code from the repo root** (`cd mml-auditor` first) so it picks
   up `.mcp.json`.
2. Claude Code will prompt to **approve the project MCP server** the first time —
   approve `mml-audit`.
3. Confirm it's live: run `/mcp`. You should see **mml-audit** with **20 tools**.
4. Ask questions in plain English — Claude picks the right tool:
   - *What fields does the BPM family have, and which files reference it?*
   - *Who calls `getmachineconfig`, and how often?*
   - *Show me the first 40 lines of `getmachineconfig.m`.*
   - *List the AO families with their device counts.*

> The server is **read-only** — it never writes. `read_source`/`search_source`
> serve from the embedded source bundle, whose key set is a fixed allow-list, so
> path traversal is impossible by construction. (If the bundle is absent on a
> full local checkout, the server falls back to the on-disk tree with a `../`
> escape guard.)

---

## CLI fallback (no agent)

The same server runs as a plain CLI — handy for a sanity check or if `/mcp`
won't connect:

```bash
python3 audit/mcp/server.py list                                  # list all 20 tools
python3 audit/mcp/server.py call list_families                    # 37 AO families
python3 audit/mcp/server.py call get_family BPM                   # one family, 3 tiers
python3 audit/mcp/server.py call files_for_family BPM             # files referencing BPM
python3 audit/mcp/server.py call callers_of getmachineconfig.m    # weighted callers
python3 audit/mcp/server.py call read_source mml/getmachineconfig.m 1 40
```

---

## The 20 MCP tools

| Area | Tools |
| --- | --- |
| Overview | `stats` |
| AO families | `list_families`, `get_family` |
| AD machine-config paths | `list_ad_paths`, `get_ad_path` |
| Operational modes | `list_modes`, `get_mode` |
| MemberOf groups | `list_groups`, `get_group` |
| Subsystems | `list_subsystems` |
| Workflows | `list_workflows`, `get_workflow` |
| Verbs (API surface) | `get_verb` |
| Files | `get_file`, `search_files` |
| Call relationships | `callers_of`, `callees_of`, `files_for_family` |
| Source (bundled) | `search_source`, `read_source` |

`get_family` / `get_verb` / `get_ad_path` return the verified / domain-context /
needs-operator-input tiers as **distinct labeled fields**, so an agent never
presents an open question as fact.

---

## Rebuilding the audit artifacts (optional)

You only need this if the underlying `.m` tree changes — and it requires a
**full local MML checkout** on disk (the 136 MB tree is not in this branch). It
re-runs the static extractors (~60 s, Python stdlib only):

```bash
for s in audit/extractors/0{1..5}_*.py audit/extractors/0{7..9}_*.py audit/extractors/1{0,1}_*.py; do
  python3 "$s"
done
python3 audit/extractors/12_source_bundle.py    # rebuild source_bundle.json.gz (MCP source)
python3 audit/extractors/06_build_browser.py    # always last — bundles browser.html
```

See [`CLAUDE.md`](CLAUDE.md) for the full pipeline description.

---

## Repo layout

```
.mcp.json                 registers the mml-audit MCP server for Claude Code
CLAUDE.md                 project context + audit architecture summary
audit/
├── browser.html          standalone human view (12 MB, no setup)
├── data/                 JSON/JSONL artifacts (the knowledge base) +
│                         source_bundle.json.gz (~2k ALS .m files, gzipped)
├── extractors/           the static-analysis pipeline (01–12 + browser builder)
├── mcp/
│   ├── server.py         the read-only MCP server (stdlib-only, ~600 lines)
│   └── DEMO.md           3-question demo script
└── notes/
    ├── findings.md       cold-readable findings doc
    └── mcp-rundown.md    why-MCP meeting brief
```

> **Note:** this repo carries the auditor (browser + MCP server + data) without
> the 136 MB MML source tree. The MML's source travels compressed in
> `audit/data/source_bundle.json.gz` and embedded in `audit/browser.html`. To
> re-run the extractor pipeline you need a full local MML checkout on disk.
