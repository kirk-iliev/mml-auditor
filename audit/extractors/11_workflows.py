#!/usr/bin/env python3
"""
Workflow inference (slice 11 of the audit).

Tries to identify the high-value procedures / workflows physicists & operators
actually invoke, by clustering files via naming conventions and propagating
through the call graph. No LLM in the loop — pure static analysis over data
that earlier extractors already produced.

For each candidate workflow we compute three tiers:

  OWNED          files in the workflow's naming group (e.g. topoff_*)
  WORKFLOW-      files reachable from OWNED via the call graph, but called
  SPECIFIC       by few other workflows — the workflow's private dependencies
  SHARED         files reachable from OWNED but also called by many other
                 workflows — infrastructure (getpv/setpv/getfamilydata/etc.)

For a migration conversation OWNED + SPECIFIC is the relevant scope; SHARED
ports once as part of the API-surface effort.

Discovery passes:
  1. Naming-prefix histogram — every filename's first underscore-separated
     segment. Prefixes used by >=3 in-scope non-archive files are candidate
     workflow tags, after filtering an explicit blacklist of utility verbs
     and framework-boilerplate prefixes.
  2. Substring keyword search — a small explicit list of known workflow
     names (loco, orbit, etc.) that don't share a clean prefix. Catches
     things like LOCO (`measlocodata.m`, `loco_*`, `applications/loco/...`).
     Only adds workflows not already discovered by the prefix pass.

Output: audit/data/workflows.json
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict, deque
from pathlib import Path

REPO = Path("/home/kiliev/Documents/Code/LBL/mmlt")
DATA = REPO / "audit" / "data"
OUT = DATA / "workflows.json"

# Filename prefixes that are utility verbs or framework boilerplate, NOT
# workflows. Excluded from candidate workflow tags. Kept conservative —
# overly aggressive blacklisting hides real procedures.
PREFIX_BLACKLIST = {
    # utility verbs (the API surface — variants like setsp_OnControlMagnet
    # shouldn't show up as workflows)
    "get", "set", "step", "find", "is", "has", "make", "build", "run", "do",
    "update", "check", "monitor", "mon", "add", "addmenu", "copy", "load",
    "save", "show", "view", "cd", "calc", "sound", "switch", "cmd", "sweep",
    "family", "channel", "dev", "elem", "common", "raw", "hw", "physics",
    "real", "am", "sp", "mm", "amp", "k", "ip", "input",
    "ramp", "gap", "fit", "write", "read",
    "getsp", "setsp", "getam", "setam", "stepsp", "getpv", "setpv",
    "getpvonline", "setpvonline", "getname", "setname",
    "linktime2datenum", "datestr", "datenum",
    # initialization / framework / launchpads
    "alsinit", "aoinit", "btsinit", "gtbinit", "cfinit", "fofbinit",
    "bpminit", "ocsinit", "hwinit", "pbpminit", "srinit", "lfbinit",
    "boosterinit", "topoffinit",
    "alslaunchpad", "argui", "launchpad",
    "mml", "mml2edm", "als", "alsinfo", "alslat", "alssummary", "alspath",
    "alsthread", "alsfitnuy9", "alslocofit",
    # tests / one-off / too generic
    "isbooster", "isstoragering", "isfamily", "isepics", "islabca",
    "isaccelerator", "ismca", "istango", "istransport",
    "no", "cc", "eta", "qm", "test",
    # plain "plot" prefix is too generic — there's no single "plot workflow"
    "plot",
    # documentation pseudo-scripts
    "readme",
    # output verbs that aren't workflows themselves
    "remote", "reset", "compute", "output", "arread", "arselect", "archive",
    "addaoprefix", "buildmml", "buildmmlbpmfamily", "buildmmlcaenfastps",
    "buildmmlfamily", "buildopsdatafiles", "buildmenu", "buildedmapps",
    # single-procedure files that have many dated copies but aren't a workflow
    "setoperationalmode",
    # generic words
    "as", "or", "and", "a", "the", "these", "this", "that",
}

# Treat lattice / model / config-data directories the same way as archive
# folders for workflow-owned purposes. Files here are static data consumed
# by workflows, not workflows themselves.
DATA_FOLDER_PARTS = {"Lattices", "lattices"}

# Workflows above this many owned files are flagged as subsystem-scale
# rather than single-procedure-scale (still useful, just different unit).
SUBSYSTEM_THRESHOLD = 50

# Substring-keyword discovery for workflows that don't share a clean prefix.
# Cast a moderately wide net — the reachability/sharing classifier sorts out
# noise downstream, and over-inclusion is cheaper than missing a real
# workflow.
WORKFLOW_KEYWORDS = {
    "loco":       "LOCO (Linear Optics from Closed Orbits) measurement & fitting",
    "orbit":      "Orbit measurement & correction",
    "topoff":     "Top-off injection (production fill mode)",
    "inject":     "Beam injection procedures",
    "dispersion": "Dispersion measurement & correction",
    "response":   "Response-matrix measurement",
    "tune":       "Tune measurement & correction",
    "chro":       "Chromaticity measurement & correction",
    "lifetime":   "Beam-lifetime measurement",
    "aperture":   "Aperture / dynamic-aperture scans",
    "scan":       "Parameter scans",
    "skew":       "Skew-quadrupole operations",
    "feedforward":"Feedforward correction (e.g. ID feedforward)",
    "feedback":   "Closed-loop feedback (orbit/RF/tune)",
    "bba":        "BBA (Beam-Based Alignment) procedures",
}

# A prefix-clustered candidate becomes a workflow only if at least this
# many in-scope, non-archive files share it.
MIN_OWNED_FILES = 3

# A reachable (non-owned) file is "shared" infrastructure if more than this
# fraction of all workflows reach it. Otherwise it's workflow-specific.
SHARED_THRESHOLD_FRAC = 0.4

# Cap reachability BFS so transitive closure doesn't drag in the entire
# call graph for every workflow. Six hops is empirically enough to reach
# the leaves through ~5 layers of MML grammar.
MAX_REACH_DEPTH = 6


def load_files() -> dict[str, dict]:
    out = {}
    with (DATA / "file_index.jsonl").open() as f:
        for line in f:
            r = json.loads(line)
            parts = Path(r["path"]).parts
            archive = r["in_legacy_folder"] or any(
                p in {"Old", "old", "_Attic", "Attic"} for p in parts
            )
            is_data_folder = any(p in DATA_FOLDER_PARTS for p in parts)
            out[r["path"]] = {
                "name": r["name"],
                "lines": r["line_count"],
                "kind": r["kind"],
                "archive": archive,
                "is_data_folder": is_data_folder,
                "in_class_folder": r["in_class_folder"],
                "summary": r["summary"],
            }
    return out


# Match a date-stamped filename suffix so we can collapse historical
# revisions onto one canonical name. Recognizes `_YYYYMMDD`, `_YYYY-MM`,
# `_YYYYMMDD_label`, `_save_YYYYMMDD`, `_backup_YYYYMMDD`, and bare
# `_YYYY` years (4-digit years from 1990 onwards).
DATE_SUFFIX_RE = re.compile(
    r"(?:_(?:save|backup|old|pre|new))?"
    r"_(?:(?:19|20)\d{2})(?:[-_]?\d{2})?(?:[-_]?\d{2})?(?:[_-].*)?$",
    re.IGNORECASE,
)


def canonical_name(filename_stem: str) -> str:
    """Strip a date-stamp suffix so historical copies collapse to one name.

    `topoff_injection_newtimingsystem_20231114` -> `topoff_injection_newtimingsystem`
    `alsinit_20260115_backup` -> `alsinit`
    `srcontrol_20210625` -> `srcontrol`
    Files without a date stamp pass through unchanged.
    """
    return DATE_SUFFIX_RE.sub("", filename_stem) or filename_stem


def load_callees() -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    with (DATA / "call_graph_edges_agg.json").open() as f:
        for e in json.load(f):
            out[e["source"]].append(e["target"])
    parenless = DATA / "parenless_call_edges.jsonl"
    if parenless.exists():
        with parenless.open() as f:
            for line in f:
                e = json.loads(line)
                out[e["caller"]].append(e["callee_path"])
    return out


def load_family_refs() -> dict[str, list[tuple[str, int]]]:
    out: dict[str, list[tuple[str, int]]] = defaultdict(list)
    p = DATA / "family_refs.jsonl"
    if not p.exists():
        return out
    with p.open() as f:
        for line in f:
            r = json.loads(line)
            out[r["file"]].append((r["family"], r["total_refs"]))
    return out


def reachable_from(seeds: set[str], adj: dict[str, list[str]],
                   files: dict[str, dict]) -> set[str]:
    visited: set[str] = set()
    queue: deque = deque((s, 0) for s in seeds)
    while queue:
        node, depth = queue.popleft()
        if node in visited:
            continue
        visited.add(node)
        if depth >= MAX_REACH_DEPTH:
            continue
        for target in adj.get(node, []):
            if target in visited:
                continue
            meta = files.get(target, {})
            if meta.get("in_class_folder") or meta.get("archive"):
                continue
            queue.append((target, depth + 1))
    return visited


def discover(files: dict[str, dict]) -> list[dict]:
    """Two-pass discovery: prefix histogram + substring keywords."""
    workflows: list[dict] = []

    # PASS 1 — first-segment prefix histogram.
    prefix_owned: dict[str, set[str]] = defaultdict(set)
    for path, meta in files.items():
        if meta["archive"] or meta["in_class_folder"] or meta["is_data_folder"]:
            continue
        name = meta["name"].lower()
        prefix = name.split("_", 1)[0] if "_" in name else name
        if prefix in PREFIX_BLACKLIST:
            continue
        prefix_owned[prefix].add(path)
    for prefix, owned in prefix_owned.items():
        if len(owned) < MIN_OWNED_FILES:
            continue
        workflows.append({
            "id": f"prefix:{prefix}",
            "name": prefix,
            "kind": "prefix",
            "description": "",
            "owned": sorted(owned),
        })

    # PASS 2 — substring keywords. Skip ones already detected as a prefix
    # cluster (those just get their description backfilled).
    existing = {w["name"]: w for w in workflows}
    for kw, desc in WORKFLOW_KEYWORDS.items():
        if kw in existing:
            existing[kw]["description"] = desc
            continue
        owned = {
            path for path, meta in files.items()
            if not (meta["archive"] or meta["in_class_folder"] or meta["is_data_folder"])
            and kw in meta["name"].lower()
        }
        if len(owned) < MIN_OWNED_FILES:
            continue
        workflows.append({
            "id": f"keyword:{kw}",
            "name": kw,
            "kind": "keyword",
            "description": desc,
            "owned": sorted(owned),
        })

    return workflows


def classify_reach(workflows: list[dict], adj: dict[str, list[str]],
                   files: dict[str, dict]) -> None:
    """Mutates each workflow with `reachable`, then partitions reachable
    into `specific` vs `shared` based on how many workflows reach each
    file."""
    for w in workflows:
        w["reachable"] = reachable_from(set(w["owned"]), adj, files)

    coverage: dict[str, int] = defaultdict(int)
    for w in workflows:
        for f in w["reachable"]:
            coverage[f] += 1
    threshold = max(2, int(len(workflows) * SHARED_THRESHOLD_FRAC))

    for w in workflows:
        owned_set = set(w["owned"])
        specific, shared = [], []
        for f in sorted(w["reachable"] - owned_set):
            (shared if coverage[f] >= threshold else specific).append(f)
        w["specific"] = specific
        w["shared"] = shared
        # Keep reachable as a sorted list for JSON output.
        w["reachable"] = sorted(w["reachable"])


def attach_caller_info(workflows: list[dict],
                       adj: dict[str, list[str]]) -> None:
    """For each workflow, record which other files in its OWN scope
    (owned ∪ specific) call each file. Lets the browser show
    'called by foo.m, bar.m' inline so the dependency chain is visible.

    Capped at MAX_CALLERS_PER_FILE per entry — callers beyond that count
    are summarized as 'and N more' on the browser side.
    """
    reverse: dict[str, set[str]] = defaultdict(set)
    for src, targets in adj.items():
        for t in targets:
            reverse[t].add(src)

    MAX_CALLERS_PER_FILE = 12
    for w in workflows:
        scope = set(w["owned"]) | set(w["specific"])
        callers_map: dict[str, list[str]] = {}
        truncated: dict[str, int] = {}
        for f in scope:
            within = sorted(reverse.get(f, set()) & scope)
            # Don't bother recording self-loops or empties.
            within = [c for c in within if c != f]
            if not within:
                continue
            if len(within) > MAX_CALLERS_PER_FILE:
                truncated[f] = len(within) - MAX_CALLERS_PER_FILE
                within = within[:MAX_CALLERS_PER_FILE]
            callers_map[f] = within
        w["callers_in_scope"] = callers_map
        w["callers_truncated"] = truncated


def attach_metrics(workflows: list[dict], adj: dict[str, list[str]],
                   files: dict[str, dict],
                   family_refs: dict[str, list[tuple[str, int]]]) -> None:
    for w in workflows:
        owned_set = set(w["owned"])
        specific_set = set(w["specific"])
        scope = owned_set | specific_set

        w["owned_count"] = len(owned_set)
        w["specific_count"] = len(specific_set)
        w["shared_count"] = len(w["shared"])
        w["owned_lines"] = sum(
            files.get(p, {}).get("lines", 0) for p in owned_set
        )
        w["specific_lines"] = sum(
            files.get(p, {}).get("lines", 0) for p in specific_set
        )

        # Collapse dated copies onto a canonical name to surface "this
        # workflow has N historical revisions" as a distinct signal.
        canon_groups: dict[str, list[str]] = defaultdict(list)
        for p in owned_set:
            stem = files.get(p, {}).get("name", "")
            canon_groups[canonical_name(stem)].append(p)
        w["unique_procedures"] = len(canon_groups)
        w["historical_revisions"] = sum(
            len(v) - 1 for v in canon_groups.values() if len(v) > 1
        )
        # Top dated-copy clusters — interesting audit signal.
        revs = sorted(
            (len(v), k, v) for k, v in canon_groups.items() if len(v) > 1
        )
        w["historical_clusters"] = [
            {"canonical": k, "count": n, "files": sorted(v)[-5:]}
            for n, k, v in revs[-5:]
        ]
        w["scale"] = "subsystem" if w["owned_count"] > SUBSYSTEM_THRESHOLD else "procedure"

        # Top AO families touched across owned + specific scope.
        fam_total: dict[str, int] = defaultdict(int)
        for path in scope:
            for fam, n in family_refs.get(path, []):
                fam_total[fam] += n
        w["top_families"] = sorted(fam_total.items(), key=lambda kv: -kv[1])[:8]

        # Top in-tree verbs called from scope (resolved by filename stem of
        # call targets).
        verb_count: dict[str, int] = defaultdict(int)
        for caller in scope:
            for target in adj.get(caller, []):
                name = files.get(target, {}).get("name")
                if name and target != caller:
                    verb_count[name] += 1
        w["top_verbs"] = sorted(verb_count.items(), key=lambda kv: -kv[1])[:12]

        # Lead entry point — largest owned file, preferring ones with help
        # text so the description we surface is meaningful.
        ranked_leads = sorted(
            owned_set,
            key=lambda p: (
                1 if files.get(p, {}).get("summary") else 0,
                files.get(p, {}).get("lines", 0),
            ),
            reverse=True,
        )
        if ranked_leads:
            lead = ranked_leads[0]
            w["lead_file"] = lead
            if not w["description"]:
                summary = files.get(lead, {}).get("summary", "").strip()
                if summary:
                    w["description"] = summary
        else:
            w["lead_file"] = None

        # Compact representative_paths for the summary view — small enough
        # to inline in a table cell.
        w["sample_owned"] = sorted(owned_set)[:5]


def main() -> int:
    files = load_files()
    adj = load_callees()
    family_refs = load_family_refs()

    workflows = discover(files)
    classify_reach(workflows, adj, files)
    attach_caller_info(workflows, adj)
    attach_metrics(workflows, adj, files, family_refs)

    # Rank workflows by a rough "size" combining owned count and lines.
    workflows.sort(key=lambda w: -(w["owned_count"] * 10 + w["owned_lines"] // 100))

    out = {
        "workflow_count": len(workflows),
        "shared_threshold_fraction": SHARED_THRESHOLD_FRAC,
        "max_reach_depth": MAX_REACH_DEPTH,
        "workflows": workflows,
    }
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False))

    print(f"wrote {len(workflows)} inferred workflows to {OUT.relative_to(REPO)}\n")
    print(f"{'kind':9s} {'name':16s} {'own':>4s} {'spec':>5s} {'lines':>7s}  description")
    for w in workflows[:30]:
        print(f"  {w['kind']:7s} {w['name']:16s} "
              f"{w['owned_count']:4d} {w['specific_count']:5d} {w['owned_lines']:7d}  "
              f"{(w['description'] or '')[:80]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
