# MML Audit — Findings (snapshot)

A 15-minute read summarizing what a static audit of the ALS MML checkout
revealed. Generated from the artifacts under `audit/data/`; see
`audit/browser.html` for an interactive view of the same data.

**Scope.** `mml/` core + `machine/ALS/{StorageRing,Booster,BTS,GTB,Common}`.
2,424 `.m` files, ~11k files in the full checkout (other facilities ignored).
**Method.** Pure static analysis: file index, AO/AD assignment manifest from
`alsinit.m` + `setoperationalmode.m` + 3 helpers, static call graph
(paren + paren-less), per-file family/AD cross-references, directory-based
subsystem clustering. **What this audit can NOT do:** confirm runtime
liveness, resolve dynamic dispatch (`feval`/`eval`/string-built names),
inspect EPICS state. Those gaps are surfaced explicitly in the data, not
papered over.

## Top-level shape of the codebase

| count | what                                                   |
| ----: | ------------------------------------------------------ |
| 2,424 | `.m` files in scope                                    |
|   485 | files in generic MML core (`mml/`)                     |
| 1,939 | files in ALS-specific tree                             |
|    37 | AO families declared (BPM, HCM, VCM, QF, …)            |
|    51 | AD machine-config paths (Energy, MCF, Circumference, …) |
|    16 | operational modes (TopOff, Two-Bunch, …)               |
|    48 | directory-level subsystems                             |
|   267 | files use dynamic dispatch (`feval`/`eval`/`str2func`) |
|   314 | files where `function <name>` ≠ `<filename>.m`         |
|   426 | files in archive folders (`Old`/`LegacyFiles`/`_Attic`/`old`) |
| 70,003 | static call edges resolved in-tree (paren + paren-less) |

## The AO is the schema

Everything the MML manipulates is keyed by **family**, and the AO struct
is the registry: 36 families across `alsinit.m` (2,793 direct
`AO.<family>.<field>` assignments) plus 1 family added by helpers (`BPM`,
built by `buildmmlbpmfamily.m`). Each family has a per-field record:
`Monitor`/`Setpoint` PV channels, hardware-↔-physics unit conversions,
`MemberOf` group membership, `DeviceList`, `Status` array, etc.

**This makes the AO the natural API contract for any Python rewrite.**
The schema can be exported as JSON (`audit/data/family_schema.json` —
1.2 MB, 37 entries) and consumed directly as a device registry.

The sibling **AD** struct holds machine-wide metadata: `Energy`, `MCF`,
`Circumference`, `HarmonicNumber`, `OperationalMode`, `Chromaticity.Golden`,
hysteresis state, plus pointers to OpsData files (LOCO, LFB, TFB, THC,
lattice). It's surprisingly **encapsulated** — only 594 (file × AD-path)
direct references across the whole tree. Most code reads it via
`getfamilydata`, which means the AD layer can become a clean read-only
config in any port without breaking many call sites.

## Operational modes

The 16 modes are all in `setoperationalmode.m`. Their definitions can be
read as small per-mode deltas: **mode 1 (TopOff, production) is 8
assignments** — `AD.ATModel`, `AD.Chromaticity.Golden`, `AD.Energy`,
`AD.HysteresisBranch`, `AD.InjectionEnergy`, `AD.OperationalMode`,
`AD.PseudoSingleBunch`, `AO.TUNE.Monitor.Golden`. That's the entire spec
of "what makes TopOff TopOff" in eight lines.

| mode | name                              | overrides |
| ---: | --------------------------------- | --------: |
|    1 | 1.9 GeV, TopOff (production)      |         8 |
|    2 | 1.9 GeV, Inject at 1.353          |         7 |
|    3 | 1.9 GeV, Inject at 1.23           |         7 |
|    4 | 1.9 GeV, High Tune                |         7 |
|    5 | 1.9 GeV, Low Tune                 |         7 |
|    6 | 1.9 GeV, Two-Bunch                |         8 |
|    7 | 1.5 GeV, High Tune                |        31 |
|    8 | 1.5 GeV, Isochronous Sections     |        14 |
|    9 | 1.5 GeV, Inject at 1.353          |         7 |
|   10 | 1.9 GeV, Low Emittance Mode       |         9 |
|   99 | 1.9 GeV, High Tune Mode           |         9 |
|  100 | 1.9 GeV, Christoph Mode           |         8 |
|  101 | 1.9 GeV, Model                    |         8 |
|  888 | Pseudo-Single Bunch (0.18, 0.25)  |        23 |
|  999 | 1.9 GeV, Greg Mode                |        25 |
| 9999 | 1.9 GeV, Tom Mode                 |         8 |

**Audit observation.** Three modes are named after people: `Christoph`
(100), `Greg` (999), `Tom` (9999). They are in the production
`setoperationalmode.m` and they touch real AO/AD state. Worth a follow-up
with whoever currently maintains the file to confirm they should still be
there.

**`99 = 1.9 GeV, High Tune Mode`** appears to be a duplicate of `4 = 1.9
GeV, High Tune`. Their effective configurations differ slightly but the
naming collision is confusing.

## ALS divergence from the upstream framework is much smaller than expected

Only **four** ALS files override functions defined in `mml/` core:

- `magstep` — per-sub-machine variants in BTS, GTB, GTB/GTFC, mml
- `quadplotall` — ALS storage-ring variant
- `setorbitsetup` — ALS storage-ring variant
- `Contents` — documentation file, harmless

Everything else in the ALS tree is **additive**, not modificational. The
core MML grammar (`getsp`, `setsp`, `getam`, `family2*`, `*2channel`) is
unchanged from what Greg Portmann's framework provides. Where ALS has
diverged, the divergence is concentrated in:

1. **`alsinit.m`** (6,428 lines) — the per-facility AO/AD declaration
2. **`setoperationalmode.m`** (5,138 lines) — the per-mode overrides
3. **Net-new ALS-only tooling** — `arplot_*` archive plots,
   `srcontrol_*` orbit controllers, `aaplot_*` automatic plots, LFB
   tools, FAD tooling, etc.

This is a meaningful finding for any Python-port discussion: the core
verb/noun grammar has stayed canonical, so a port that targets that
grammar can serve all MML-using facilities (not just ALS) with minimal
per-facility divergence in the *core* — the per-facility layer is
genuinely additive and can be ported as a separate concern.

## The MML verb vocabulary, discovered by usage

The top callees (by in-tree call count) form a tight vocabulary. This is
what any Python middle layer needs to provide an equivalent of:

| calls  | verb                | purpose                                       |
| -----: | ------------------- | --------------------------------------------- |
|  7,021 | `setpv`             | direct PV write                               |
|  5,083 | `getpv`             | direct PV read                                |
|  4,447 | `setpvonline`       | online PV write (path-resolved EPICS bridge)  |
|  3,802 | `getpvonline`       | online PV read                                |
|  2,956 | `getfamilydata`     | family registry lookup (central)              |
|  2,454 | `now`               | EPICS time (overrides MATLAB built-in!)       |
|  1,218 | `getsp`             | setpoint read                                 |
|  1,186 | `getsrstate`        | storage-ring state                            |
|    816 | `family2channel`    | family → PV name                              |
|    796 | `setsp`             | setpoint write                                |
|    771 | `getgolden`         | golden-value read                             |
|    736 | `getam`             | monitor (analog) read                         |
|    717 | `family2dev`        | family → device list                          |
|    709 | `getname_als`       | naming convention                             |
|    710 | `getdcct`           | beam current                                  |
|    576 | `family2datastruct` | family → struct accessor                      |
|    519 | `setfamilydata`     | family registry write                         |
|    373 | `getenergy`         | beam energy                                   |
|    320 | `setff`             | feed-forward                                  |
|    310 | `stepsp`            | step setpoint by delta                        |

Plus `monitor*` polling functions, `ramp*` ramp-table verbs, and the
`family2*` / `*2channel` conversion family. ~50 verbs cover the bulk of
operational use; we have them all in `mml/`.

## EPICS-backend plugin architecture (preserve this in any rewrite)

The MML factors EPICS-protocol access into pluggable backends. Each
backend lives in `mml/online/<backend>/` and exports the same verb
surface. The path-precedence machinery in `setpathmml.m` picks which
backend is active at runtime.

Detected backends:

| backend       | files | example exports                        |
| ------------- | ----: | -------------------------------------- |
| `labca`       |     6 | EPICS via LabCA (ALS default)          |
| `mca`         |     9 | Multi-Channel Analyzer / Sergei Chevtsov |
| `mca_asp`     |     9 | ASP-specific MCA variant               |
| `opc`         |     5 | OPC                                    |
| `opc_sps`     |    10 | SPS-specific OPC variant               |
| `sca`         |     5 | Simple CA                              |
| `slc`         |     9 | SLC control system                     |
| `tango`       |     3 | Tango (Soleil, others)                 |
| `tls_ctl`     |     3 | Taiwan Light Source                    |
| `ucode`       |     7 | Soleil ucode                           |
| `umer_ws`     |     2 | UMER websocket                         |
| `lnls/lnls1`  |     5 | LNLS sirius                            |

This is a clean strategy-pattern design. Any Python port should preserve
it — the equivalent would be an `EpicsBackend` Protocol with concrete
implementations per facility, selected via config.

## EPICS time functions shadow MATLAB built-ins

`machine/ALS/Common/EPICS_Time_Functions/` contains five files that
**deliberately override MATLAB built-ins**:

- `datenum.m`, `datestr.m`, `datevec.m`, `now.m`, `weekday.m`

Once `setpathals.m` puts that directory on the path, all calls to
`datestr(...)` etc. resolve to the EPICS versions, which handle EPICS
timestamps natively. This is a non-obvious architectural choice that any
port needs to be aware of — Python equivalents will need explicit EPICS
time conversion since they won't get this for free via path shadowing.

## Subsystem clusters — where to focus a port or audit

Top-cohesion clusters (most self-contained, easiest to port as a unit):

| cohesion | files | cluster                                |
| -------: | ----: | -------------------------------------- |
|   0.80   |    7  | `mml/online/ucode`                     |
|   0.57   |   13  | `machine/ALS/Common/private`           |
|   0.43   |  156  | `machine/ALS/StorageRing/LFB/LFBTools` |
|   0.43   |   37  | `machine/ALS/Common/BPM/NSLS2`         |
|   0.43   |    9  | `mml/online/mca_asp`                   |
|   0.42   |    9  | `mml/online/mca`                       |
|   0.38   |    9  | `mml/online/slc`                       |
|   0.30   |   25  | `machine/ALS/GTB/GTFC`                 |

The LFB toolset is a substantial subsystem (156 files) and 43%
self-contained — a natural unit for either a focused audit or a port
target.

Zero-cohesion clusters worth investigating:

| files | lines  | cluster                                  | likely reason                   |
| ----: | -----: | ---------------------------------------- | ------------------------------- |
|    89 |   ?    | `Common/BPM/Working/`                    | working/WIP scratch directory   |
|    62 |  ~25k  | `StorageRing/Lattices/`                  | AT lattice files, dynamic-load  |
|    19 | 30,119 | `StorageRing/Topoff/`                    | **avg 1,585 lines per file** — large, isolated tooling. Worth investigating. |
|    19 |   ?    | `mml/simulators/at/changes/`             | one-off AT toolbox patches      |
|    17 |   886  | `mml/@AccObj/`                           | class methods (excluded from global index by design) |
|     6 |   ?    | `mml/online/labca/`                      | path-resolved, called dynamically |

## Archive folders — 426 files, 4 conventions, treat with care

There are **four parallel archive conventions** in the tree:

- `Old/` — 220 files
- `LegacyFiles/` — 121 files (this one IS on the path; `alsinit.m`
  literally `addpath`s it)
- `_Attic/` — 59 files
- `old/` (lowercase) — 26 files

**`setoperationalmode.m` has at least 6 historical copies.** Current
version + four in `_Attic/` (dated Sep/Oct/Nov/Dec 2024) + two in `Old/`
(dated 2015 and 2021). Similar story for `alsinit.m`: current +
`alsinit_20260115_backup.m` next to it. The naming reveals that *folder-
based version control* is happening informally — worth either committing
to git history or accepting and tooling around.

116 files in the whole tree have explicit date stamps in their names
(`_YYYYMMDD` or `_YYYY`). All are de facto historical copies.

## Dead-code candidates (best-effort static)

The static call graph identifies **1,275 files with zero in-tree callers
from live (non-archive) code**, with paren-less calls accounted for.
*This is not a death certificate* — categories:

1. **Entry-point scripts** — `alslaunchpad.m`, `argui.m`, the `aaplot_*`
   / `arplot_*` files humans launch directly. Real, just not statically
   called.
2. **Path-shadowed alternatives** — `getpvonline.m` has 14 versions, one
   per backend; only one wins at runtime. The other 13 look orphaned.
3. **AT lattice files** — `StorageRing/Lattices/` is loaded by
   filename-as-name from `aoinit.m`. Dynamic dispatch hides the edges.
4. **GUI callbacks** — files invoked by figure handles, not by source
   code. Static analysis can't see these.
5. **Genuinely orphaned** — probably the largest single category,
   but verifying requires either runtime coverage or a knowledgeable
   physicist's review.

The browser's "Files" tab, sorted by `in (live)`, surfaces this list.
**Recommendation:** rather than auto-deleting, hand the list to a senior
physicist for triage; flag the 116 dated-suffix files and the 4 archive
folders as low-risk first cuts.

## Reliability of the static call graph

| metric                              | value                                 |
| ----------------------------------- | ------------------------------------- |
| total callsites detected            | ~415,000 (paren + paren-less)         |
| resolved to in-tree files           | 70,003                                |
| unresolved (built-ins / toolbox)    | ~345,000                              |
| files with `feval`/`eval`/`str2func` | 267 (~11%)                           |
| files with name ≠ filename mismatch | 314                                   |

The unresolved share is high because MATLAB is *enormously*
built-in-heavy (length, size, sprintf, plot, etc.). The in-tree edge
count is what matters and we trust it. False positives are limited by
the local-variable filter (paren-less) and class-method exclusion
(`set`/`get`/`disp`/`plot` correctly route to MATLAB built-ins, not
`@AccObj/` methods). False negatives mostly come from dynamic dispatch
and command-syntax calls.

## Recommendations for next steps

1. **Validate the artifacts against a live MATLAB session.** Run
   `aoinit` + `setoperationalmode` for each of the 16 modes, dump
   `getao`/`getad` to a `.mat`, compare against `family_schema.json` /
   `ad_schema.json`. This closes the "static parser approximation"
   loop and validates exactly which fields are computed vs. declared.
2. **Run MATLAB profiler over a single representative shift.** Combine
   that runtime coverage with our `files_with_zero_live_callers` list
   to get a defensible dead-code report. This is the one piece that
   genuinely needs operator time.
3. **Walk the personal modes (`100`/`999`/`9999`) and the duplicate
   `99`** with whoever currently owns `setoperationalmode.m`. These are
   small file-modifying actions, not architectural ones.
4. **Audit the `StorageRing/Topoff/` subsystem.** 30k lines across 19
   files with zero internal cohesion is unusual; it's either
   misclassified or worth understanding as a discrete tooling layer.
5. **Treat the AO/AD schemas and the verb catalog as the formal
   contract for any Python port discussion.** They are the only
   facility-agnostic, statically-derivable description of "what MML
   does" — concrete enough to design against.

## Where to look in the data

| question                                     | open in browser                                              |
| -------------------------------------------- | ------------------------------------------------------------ |
| What does AO.BPMx contain?                   | AO families → BPMx                                           |
| What does Mode 1 (TopOff) actually do?       | Operational modes → 1                                        |
| What files set `AD.Energy`?                  | AD paths → Energy → "Files referencing"                      |
| Who calls `getmachineconfig`?                | Files → search "getmachineconfig" → click row                |
| What's tightly coupled to BPM family?        | AO families → BPM → "Files referencing" sorted by ref count  |
| Where is mode-1's `ATModel` value declared?  | Operational modes → 1 → click value                          |
| Which subsystem owns LFB tooling?            | (subsystems view; coming soon to browser)                    |

For ad-hoc questions, the raw JSON is in `audit/data/`. The richest
files are `family_schema.json` (37 family entries with full field
trees), `mode_overrides.json` (per-mode delta), and the
`call_graph_*` triple (nodes, edges_agg, summary).

---

**Snapshot date:** 2026-05-27.
**Source-tree state:** as downloaded from the GitLab cron-backup
(no upstream reference for diff).
**Reproducibility:** re-run `python3 audit/extractors/0[1-9]*.py` then
`python3 audit/extractors/06_build_browser.py` to refresh all
artifacts.
