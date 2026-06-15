# MML Audit — Project Context

This directory holds a copy of the **MATLAB Middle Layer (MML)** as deployed at
LBNL's Advanced Light Source (ALS). It was downloaded from the lab's GitLab
backup (cron-snapshotted) and is the only reference — there's no upstream
Portmann release to diff against. Treat this checkout as the source of truth.

## Goals

- **Why this work exists**: many people at the lab treat the MML as a black box.
  The aim is to inform lab-wide conversations about
  (a) Python-based alternatives to the MML, (b) high-level agentic-callable
  control interfaces, (c) general infrastructure modernization. Doing any of
  those well requires an inventory of what the MML actually is and does,
  which didn't exist before this audit.
- **Audience for the artifacts**: lab physicists/operators and a future
  Python-port effort. Some artifacts (`browser.html`,
  `audit/notes/findings.md`) are designed to be shareable as standalone
  attachments with colleagues who won't run any code.
- **Constraints**: pure static analysis only — no live MATLAB session
  available yet. Pipeline must be re-runnable locally.

## What we built (today's audit pipeline)

```
audit/
├── extractors/
│   ├── _mlab.py                    shared MATLAB-source parsing helpers
│   ├── 01_file_index.py            walk *.m → 2,424 file records
│   ├── 02_ao_assignments.py        AO/AD declarations from alsinit + setOpMode + sextupole-harmonic
│   ├── 03_helper_rebinds.py        AO mutations from buildmmlbpmfamily + buildmmlcaenfastps
│   ├── 04_family_schema.py         consolidate to per-family schema
│   ├── 05_call_graph.py            paren-call graph (class methods excluded)
│   ├── 06_build_browser.py         the HTML generator (this is the BIG file)
│   ├── 07_cross_references.py     file × family / file × AD path
│   ├── 08_parenless_calls.py       no-arg paren-less call detector
│   ├── 09_subsystems.py            directory-based subsystem clusters + cohesion
│   ├── 10_groups_devices.py        MemberOf groups + per-family device counts
│   ├── 11_workflows.py             workflow inference (prefix + keyword + reachability)
│   ├── d3.v7.min.js                inlined into browser.html
│   └── pako.min.js                 inlined for source decompression
├── data/                           all JSON/JSONL artifacts (see below)
├── notes/
│   └── findings.md                 18 KB cold-readable findings doc
└── browser.html                    standalone 12 MB browseable view
```

### Artifacts in `audit/data/`

| file                          | what                                                          |
| ----------------------------- | ------------------------------------------------------------- |
| `file_index.jsonl`            | 2,424 .m files × sig/help/lines/mtime/dyn-dispatch hints       |
| `ao_assignments.jsonl`        | 3,092 AO/AD assignment sites with mode-context                |
| `ao_helper_assignments.jsonl` | 354 helper-injected family fields                             |
| `family_schema.json`          | 37 AO families consolidated                                   |
| `ad_schema.json`              | 51 AD machine-config paths                                    |
| `mode_overrides.json`         | 16 operational modes + per-mode deltas                        |
| `call_graph_*.{json,jsonl}`   | paren-edge graph                                              |
| `parenless_call_edges.jsonl`  | 9,985 paren-less edges (e.g. `checkforao;`)                   |
| `family_refs.jsonl`           | 4,125 (file, family) cross-references                         |
| `ad_refs.jsonl`               | 594 (file, AD path) cross-references                          |
| `subsystems.json`             | 48 directory clusters with cohesion                           |
| `groups.json`                 | 82 MemberOf groups (the implicit operational taxonomy)        |
| `family_meta.json`            | per-family device counts + group memberships                  |
| `workflows.json`              | 67 inferred workflows (topoff, loco, orbit, inject, etc.)     |
| `annotations.json`            | hand-authored confidence-tiered reference text (families+verbs)|

### How to rebuild

```bash
cd /home/kiliev/Documents/Code/LBL/mmlt
for s in audit/extractors/0{1..5}_*.py audit/extractors/0{7..9}_*.py audit/extractors/1{0,1}_*.py; do
  python3 "$s"
done
python3 audit/extractors/06_build_browser.py    # always last — bundles everything
```

Total run time: ~60 seconds. The `06_build_browser.py` step inlines the
source bundle (compressed with gzip+base64 via pako) so the browser stays
single-file (~12 MB) and works fully standalone (no D3 CDN, no source fetch).

### Confidence-tiered reference annotations (`annotations.json`)

Hand-authored reference text rendered as a three-tier card on AO-family detail
pages (`#/family/<NAME>`) and a new verb-detail page (`#/verb/<NAME>`, reached
by clicking a `doc`-badged row in the API surface tab). The three tiers, by
design, keep AI/domain conjecture visually separable from verified fact:

- **Verified** (green) — rendered *live* from the extraction artifacts
  (device counts, fields, groups, call stats, resolved path). Never stored in
  `annotations.json`, so it can't drift from the code.
- **Domain context** (blue) — general accelerator-physics knowledge, explicitly
  labelled "not ALS-specific config." Hand-authored.
- **Needs operator input** (amber) — a **collapsed-by-default `<details>`**
  (the user wanted conjecture low-visibility) holding open questions for a
  physicist/operator. Hand-authored. This is the interview agenda, not gaps.

Only tiers 2+3 live in `annotations.json` (`families.<NAME>` /
`verbs.<NAME>`, each with `context` + `unknowns`; verbs also carry an
`signature_inferred` shown amber-labelled *outside* the verified tier). To
extend coverage: add entries to `annotations.json` and re-run
`06_build_browser.py` — no code changes. Currently 35/37 families (skipped
`GeV` and `BSC` as too uncertain to annotate honestly) and the top 19 verbs.
The `annotationCard()` JS helper + `.ann-*` CSS classes do the rendering.

## Architectural summary (the audit's TL;DR)

The MML's core data model is the **AO** (Accelerator Object) struct keyed by
**family** name (BPM, HCM, VCM, QF, etc., 37 in total at ALS) plus the **AD**
struct (machine-wide metadata: Energy, MCF, OperationalMode, etc., 51 paths).
Every family field is tagged with **MemberOf** groups (82 of them) which are
how the MML iterates families for operations — e.g. `getmachineconfig` walks
families with `MemberOf == 'MachineConfig'`. The verb layer
(~30 dominant verbs: setpv/getpv, getfamilydata, setpvonline/getpvonline, etc.)
operates on these families.

**Key findings worth carrying forward** (full detail in `audit/notes/findings.md`):

- **ALS overrides only 4 files** in `mml/` core (magstep, quadplotall,
  setorbitsetup, Contents). The rest of the ALS layer is *additive*, not
  modificational — meaningful for any Python-port discussion.
- **EPICS-time built-ins are silently shadowed** by `machine/ALS/Common/EPICS_Time_Functions/`
  (now, datestr, datenum, datevec, weekday). Any port needs to handle this.
- **`setoperationalmode.m` defines 16 modes**, including 3 named after people
  (`Greg`, `Tom`, `Christoph`) and a duplicate `99` that looks like a forgotten
  alias of mode 4. Mode 1 (TopOff, production) is just 8 assignments.
- **EPICS backend is plugin-style** — `mml/online/<backend>/` has 14
  implementations of `getpvonline.m`. Path order picks the active one. Preserve
  this pattern in any port.
- **Informal version-control via folder copies** is pervasive — `_Attic/`,
  `Old/`, `LegacyFiles/`, 116 dated-suffix files, 11 copies of
  `topoff_injection_newtimingsystem`. Worth pulling into real git.
- **`StorageRing/Topoff/` is the strangest cluster**: 19 files, 30,119 lines
  (avg 1,585 lines/file), zero internal cohesion. Untriaged.

## The browser

`audit/browser.html` is the consumer-facing artifact. Single 12 MB file,
opens in any browser, no setup. Tabs:

- **Overview** — top-line numbers, top callees, shadowed callables
- **Search source** — grep across all 1,998 embedded source files
- **Workflows** — 67 inferred procedures (Top-off, LOCO, Orbit, Inject, …)
- **Files** — sortable index, click-through to detail + source viewer
- **AO families** — schema browser, click into field tree
- **AD paths** — machine-config paths
- **Operational modes** — per-mode deltas
- **Subsystems** — directory clusters with cohesion
- **MemberOf groups** — the implicit operational taxonomy
- **API surface** — top 100 verbs by usage (the de-facto Python-port contract)

Navigation is hash-based: every URL like `#/family/BPM` is shareable, browser
back/forward works. Source viewer has MATLAB syntax highlighting, clickable
line numbers, jump-to-line on `file:line` references.

## Gotchas (things to not re-learn)

- **`node --check` is permissive; V8 (Chrome) catches more.** Before
  shipping browser.html changes, validate with
  `google-chrome --headless --disable-gpu --no-sandbox --virtual-time-budget=3000 --enable-logging=stderr --log-level=0 file://$(pwd)/audit/browser.html --dump-dom > /tmp/dom.html 2> /tmp/chrome.log`
  then grep for `Uncaught|SyntaxError|TypeError|ReferenceError`.
- **The `el()` helper in browser.html only flattens children one level.**
  Passing a nested array (e.g. `el('div', {}, callers.map(c => [...]))`)
  serializes inner arrays as `[object Text],[object HTMLAnchorElement]`. Use
  `flatMap` or push to a flat array before passing.
- **String + DOM-element concatenation calls `.toString()`** on the element,
  producing `[object HTMLSpanElement]`. Always pass as separate children.
- **The router's `case` statements MUST call `show*Detail()` directly**, not
  `navigateTo(...)`. Going via navigateTo from within route() causes an
  infinite hashchange loop.
- **`getmachineconfig` is the canonical 5-callee function** that exercises
  every architectural layer (family registry, MemberOf filtering, get/set
  verbs, serialization). When teaching anyone about the MML, point them
  here first — the user's colleague was right about this from the start.
- **`alsinit.m` has 2,793 `AO.*` assignment lines** — almost entirely
  declarative struct-building. That's why static parsing works at all.
- **Helpers `buildmmlbpmfamily.m` and `buildmmlcaenfastps.m`** use lowercase
  `ao` param + dynamic-field access `ao.(Family).…`. Extractor 03 resolves
  these per-call-site. Their *subfunctions* have their own local `ao` (a
  return struct) which is NOT the AccObj — extractor 03 stops processing at
  the first subfunction declaration to avoid invented families.
- **Class methods in `@AccObj/` are intentionally excluded** from the global
  callable index. OO dispatch is runtime/type-based, so name-based
  resolution would mis-route `set(handle, 'Color', 'red')` to `@AccObj/set.m`.
- **MATLAB char-matrix string lists** (e.g. `['SR01S___IG1____AM00'; ...]`)
  pad with `_` to enforce equal row widths. The padding is stripped at
  EPICS-call time. This is a MATLAB-shape artifact, not part of the PV name.

## Open follow-ups (deferred)

These need either runtime access or a physicist's input — they're not
blocked on more static analysis:

1. **Live MATLAB validation pass.** Run `aoinit` + each `setoperationalmode`
   mode, dump `getao`/`getad` to `.mat`, diff against
   `family_schema.json` / `ad_schema.json`. Closes the static-approximation
   loop and validates exactly which fields are runtime-computed.
2. **One-shift profiler trace** combined with our zero-live-caller list to
   produce a defensible dead-code report.
3. **Talk to whoever owns `setoperationalmode.m`** about the personal modes
   (100/999/9999) and the duplicate-looking `99`.
4. **Audit the `StorageRing/Topoff/` anomaly** — 30k lines, 19 files,
   zero internal cohesion. Either misclassified or worth understanding as a
   discrete tooling layer.
5. **Workflow inference review with a physicist/operator.** The 67 inferred
   workflows are a starting list; ground truth needs human review. The user
   plans to do this.

## Permissions

`.claude/settings.local.json` allows `Bash(python3 *)` and
`Skill(update-config)` (others have been pruned). If you need to run other
commands (find, grep, curl for fetching D3/pako updates, google-chrome
headless for browser validation), ask before invoking.

## Quick session-start checklist

1. Re-read this file (loaded automatically) and `audit/notes/findings.md`.
2. `git log --oneline` is NOT useful — this isn't a git repo, just a cron
   snapshot. Use `stat` / `ls -la` for file recency instead.
3. If the user mentions a specific file/family/workflow, the browser is
   usually the fastest path: open `audit/browser.html` and search.
4. If extracting fresh data, re-run the pipeline (see "How to rebuild" above)
   before reasoning from the artifacts.
