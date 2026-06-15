#!/usr/bin/env python3
"""
Static call-graph extractor (slice 5 of the audit).

Builds a static caller→callee graph over the in-scope `.m` files, resolving
each call against the callable index produced by extractor 01. MATLAB
dispatches by filename, so the canonical name of a callable IS its `.m`
filename — no symbol table required.

Scope: the same roots as extractor 01 (mml + ALS Storage/Booster/BTS/GTB
+ ALS Common). Files in archive-style folders (`Old/`, `LegacyFiles/`,
`_Attic/`, `old/`) are still PARSED for outgoing calls — leaving them out
would orphan the calls FROM live code INTO archive code, which is itself
audit signal we want to see. But they're flagged in the per-file metadata
so consumers can filter them out.

What this pass captures:
  - Paren-style calls: `funcname(args)`
  - Resolved against the callable index → in-tree edge
  - Multiple candidates (shadowing) preserved — e.g. when both
    `mml/setsp.m` and `machine/ALS/StorageRing/setsp.m` exist, we list
    both and mark the ALS one as the "winner" given the alsinit path
    order
  - Dynamic-dispatch sites (feval/eval/str2func) already counted in the
    file index; not re-counted here

Known limitations (acceptable for v1; document, don't paper over):
  - Paren-less no-arg calls (`x = getfamilylist;`) are NOT captured.
    Detecting them robustly requires per-file local-variable tracking.
    Surfaced as a TODO so a v2 pass can add them.
  - Command-syntax calls (`setfamilydata BPMx Status`) are NOT captured.
    Very rare in this codebase; skipped.
  - Calls to MATLAB built-ins, Toolbox functions, and external libs
    aren't enumerated individually — they appear in the per-file
    `unresolved_call_names` set so we can spot-check.

Outputs:
  audit/data/call_graph_edges.jsonl       one record per call site
  audit/data/call_graph_summary.json      aggregate stats
  audit/data/call_graph_nodes.json        per-file node info for vis tools
  audit/data/call_graph_edges_agg.json    aggregated (caller, callee, count) edges
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _mlab import read_lines, walk_chars

REPO = Path("/home/kiliev/Documents/Code/LBL/mmlt")
DATA = REPO / "audit" / "data"
FILE_INDEX = DATA / "file_index.jsonl"

OUT_EDGES = DATA / "call_graph_edges.jsonl"
OUT_SUMMARY = DATA / "call_graph_summary.json"
OUT_NODES = DATA / "call_graph_nodes.json"
OUT_EDGES_AGG = DATA / "call_graph_edges_agg.json"

# Path-precedence: when a callable name has multiple definitions in the tree,
# this list (lowest-priority first) determines which one the live ALS session
# resolves to. The order mirrors what alsinit.m does with addpath(..., '-begin'):
# generic mml core is at the bottom; ALS-specific dirs are added "-begin"
# (front), so they shadow.
PATH_PRECEDENCE = [
    "mml",
    "machine/ALS/Common",
    "machine/ALS/Booster",
    "machine/ALS/BTS",
    "machine/ALS/GTB",
    "machine/ALS/StorageRing",   # last = highest priority (added '-begin' last)
]

# Folders treated as "archive" — not on the path in production, per the
# user's working assumption. Files here are still PARSED (we want to see
# their outgoing calls), but their incoming-edge count is computed both
# with and without them.
ARCHIVE_FOLDER_NAMES = {"Old", "old", "LegacyFiles", "_Attic", "Attic"}

# Tokens that look like identifiers but are MATLAB language constructs —
# excluding these keeps the candidate-call list clean. We don't need the
# whole built-in list; the callable-index lookup will filter the rest.
MATLAB_KEYWORDS = frozenset({
    "if", "elseif", "else", "end", "for", "while", "switch", "case",
    "otherwise", "break", "continue", "return", "function", "global",
    "persistent", "try", "catch", "classdef", "properties", "methods",
    "events", "enumeration", "spmd", "parfor", "true", "false",
})


def file_in_archive(path_str: str) -> bool:
    return any(p in ARCHIVE_FOLDER_NAMES for p in Path(path_str).parts)


def file_root_bucket(path_str: str) -> str:
    """Return the configured ROOT this file lives under, for shadow precedence."""
    for root in PATH_PRECEDENCE:
        if path_str.startswith(root + "/"):
            return root
    return "<unknown>"


def precedence_index(root: str) -> int:
    try:
        return PATH_PRECEDENCE.index(root)
    except ValueError:
        return -1


# ---------------------------------------------------------------------------
# Build callable index from file_index.jsonl
# ---------------------------------------------------------------------------

def load_callable_index() -> tuple[dict, dict]:
    """Build:
      callable_index: name -> list of file records (sorted by path precedence,
                      highest-priority first)
      file_by_path:   absolute path-string -> the file record

    The callable name is the filename (stem). MATLAB dispatches by filename;
    the inner `function` name is irrelevant for resolution (and as the file
    index showed, 314 files have a mismatch — those inner names are
    effectively dead).
    """
    by_name: dict[str, list[dict]] = defaultdict(list)
    by_path: dict[str, dict] = {}
    if not FILE_INDEX.exists():
        sys.exit(f"error: {FILE_INDEX} missing — run extractor 01 first.")
    with FILE_INDEX.open() as f:
        for line in f:
            rec = json.loads(line)
            by_path[rec["path"]] = rec
            # Use the filename stem as the callable name. Class methods in
            # `@Foo/bar.m` are dispatched at runtime based on the receiver's
            # type — `set(handle,...)` goes to MATLAB's built-in `set`, NOT
            # to `@AccObj/set.m`, unless the first arg is an AccObj. Without
            # type info we can't tell. Excluding class methods from the
            # global index keeps `set`/`get`/`plot`/`disp` resolved to
            # "external" — that's correct behavior for the call sites we
            # CAN'T type-check.
            if rec.get("in_class_folder"):
                continue
            by_name[rec["name"]].append(rec)
    # Sort each name's candidates by precedence (highest first).
    for name, recs in by_name.items():
        recs.sort(
            key=lambda r: (
                -precedence_index(file_root_bucket(r["path"])),  # higher idx → earlier in sort
                file_in_archive(r["path"]),                        # archive last
                r["path"],
            )
        )
    return by_name, by_path


# ---------------------------------------------------------------------------
# Strip comments and strings from a file body (preserve line numbers)
# ---------------------------------------------------------------------------

def clean_code(lines: list[str]) -> list[str]:
    """Return per-line code with strings & comments replaced by spaces.

    Preserves column positions so line numbers stay accurate and identifier
    boundaries remain valid. Uses the same string-aware walker as the AO
    extractor — a `'` inside a `'...'` literal is part of the string, not
    a delimiter for the next token.
    """
    cleaned: list[str] = []
    for line in lines:
        out = list(line)
        for i, c, in_str in walk_chars(line):
            if in_str:
                out[i] = " "
            elif c == "%":
                # blank from `%` to end of line
                for j in range(i, len(out)):
                    out[j] = " "
                break
        cleaned.append("".join(out))
    return cleaned


# Identifier-followed-by-`(`. We later filter on path-precedence to avoid
# matching the declared function's own name (e.g. `function foo(x)` is
# `foo` followed by `(`, but it's a declaration not a call).
CALL_RE = re.compile(r"(?<![.\w])([A-Za-z_]\w*)\s*\(")

# Match the function-declaration head so we can ignore that one site.
FUNCDECL_RE = re.compile(r"^\s*function\b.*?\b([A-Za-z_]\w*)\s*\(")


def extract_calls_in_file(path: Path, callable_index: dict, file_rec: dict) -> list[dict]:
    """Walk a single .m file body and emit one record per paren-call site."""
    raw_lines = read_lines(path)
    code_lines = clean_code(raw_lines)
    callsites: list[dict] = []
    self_name = file_rec.get("name")

    # Determine declared subfunctions so we can mark "intra-file" calls.
    subfn_names: set[str] = set()
    for line in code_lines:
        m = re.match(r"^\s*function\b.*?\b([A-Za-z_]\w*)\s*[\(\s%]", line)
        if m:
            subfn_names.add(m.group(1))

    for line_idx, line in enumerate(code_lines, start=1):
        # Skip the function-declaration line's own name appearance.
        decl = FUNCDECL_RE.match(line)
        decl_span = decl.span() if decl else None

        for m in CALL_RE.finditer(line):
            if decl_span and m.start() < decl_span[1]:
                # within the declaration head — that's the def site, not a call
                continue
            name = m.group(1)
            if name in MATLAB_KEYWORDS:
                continue
            candidates = callable_index.get(name, [])
            if not candidates:
                callsites.append({
                    "caller": file_rec["path"],
                    "caller_line": line_idx,
                    "callee_name": name,
                    "resolved": False,
                    "intra_file": (name == self_name) or (name in subfn_names),
                })
                continue

            # Filter: a call from within the same file to its own subfunction
            # resolves to that subfunction, not to an unrelated file with the
            # same name. Same for the file's main function.
            if name == self_name or name in subfn_names:
                callsites.append({
                    "caller": file_rec["path"],
                    "caller_line": line_idx,
                    "callee_name": name,
                    "callee_path": file_rec["path"],
                    "resolved": True,
                    "intra_file": True,
                    "candidates_count": 1,
                    "shadowed": False,
                })
                continue

            primary = candidates[0]
            shadowed = len(candidates) > 1
            callsites.append({
                "caller": file_rec["path"],
                "caller_line": line_idx,
                "callee_name": name,
                "callee_path": primary["path"],
                "resolved": True,
                "intra_file": False,
                "candidates_count": len(candidates),
                "shadowed": shadowed,
                "shadow_candidates": (
                    [c["path"] for c in candidates] if shadowed else None
                ),
            })
    return callsites


# ---------------------------------------------------------------------------
# Aggregate views for visualization
# ---------------------------------------------------------------------------

def build_aggregates(callsites: list[dict], callable_index: dict, file_by_path: dict) -> dict:
    """Return per-file and per-edge aggregates suitable for downstream tools."""
    # In/out-degree per file.
    out_count: dict[str, int] = defaultdict(int)
    in_count: dict[str, int] = defaultdict(int)
    in_count_live_only: dict[str, int] = defaultdict(int)
    unresolved_names: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    edges: dict[tuple[str, str], int] = defaultdict(int)

    for cs in callsites:
        caller = cs["caller"]
        out_count[caller] += 1
        if cs["resolved"]:
            callee = cs["callee_path"]
            edges[(caller, callee)] += 1
            in_count[callee] += 1
            if not file_in_archive(caller):
                in_count_live_only[callee] += 1
        else:
            unresolved_names[caller][cs["callee_name"]] += 1

    # Files with ZERO incoming live callers — potential dead code (subject to
    # the usual static-analysis caveats: dynamic dispatch, GUI callbacks,
    # script-style entry points loaded by external launchers).
    all_paths = set(file_by_path)
    no_live_callers = sorted(
        p for p in all_paths
        if not file_in_archive(p) and in_count_live_only[p] == 0
    )

    # Per-callable popularity (resolved edges by callee NAME).
    callee_name_counts: dict[str, int] = defaultdict(int)
    for cs in callsites:
        if cs["resolved"] and not cs["intra_file"]:
            callee_name_counts[cs["callee_name"]] += 1

    top_callees = sorted(
        callee_name_counts.items(), key=lambda kv: kv[1], reverse=True
    )[:30]

    # Shadowed names: callables defined in multiple in-scope files.
    shadowed = {
        name: [r["path"] for r in recs]
        for name, recs in callable_index.items()
        if len(recs) > 1
    }

    summary = {
        "total_callsites": len(callsites),
        "resolved_callsites": sum(1 for c in callsites if c["resolved"]),
        "unresolved_callsites": sum(1 for c in callsites if not c["resolved"]),
        "intra_file_callsites": sum(1 for c in callsites if c.get("intra_file")),
        "shadowed_callsites": sum(1 for c in callsites if c.get("shadowed")),
        "distinct_unresolved_names": len(
            {c["callee_name"] for c in callsites if not c["resolved"]}
        ),
        "shadowed_callable_count": len(shadowed),
        "shadowed_callable_samples": dict(list(shadowed.items())[:15]),
        "files_with_zero_live_callers": len(no_live_callers),
        "files_with_zero_live_callers_examples": no_live_callers[:20],
        "top_callees_by_usage": [{"name": n, "calls": c} for n, c in top_callees],
        "top_callers_by_fanout": sorted(
            [{"path": p, "out": c} for p, c in out_count.items()],
            key=lambda x: x["out"], reverse=True,
        )[:20],
        "top_callees_by_in_degree": sorted(
            [{"path": p, "in_total": c, "in_live_only": in_count_live_only[p]}
             for p, c in in_count.items()],
            key=lambda x: x["in_total"], reverse=True,
        )[:20],
    }

    nodes = []
    for p, rec in sorted(file_by_path.items()):
        nodes.append({
            "id": p,
            "name": rec["name"],
            "root": file_root_bucket(p),
            "archive": file_in_archive(p),
            "kind": rec["kind"],
            "in_total": in_count[p],
            "in_live_only": in_count_live_only[p],
            "out": out_count[p],
            "line_count": rec["line_count"],
            "has_dynamic_dispatch": any((rec.get("dynamic_dispatch") or {}).values()),
        })

    edges_agg = [
        {"source": src, "target": tgt, "calls": cnt}
        for (src, tgt), cnt in sorted(edges.items(), key=lambda kv: -kv[1])
    ]

    return {
        "summary": summary,
        "nodes": nodes,
        "edges": edges_agg,
        "unresolved_by_caller_samples": {
            p: dict(sorted(d.items(), key=lambda kv: -kv[1])[:5])
            for p, d in list(unresolved_names.items())[:5]
        },
    }


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    callable_index, file_by_path = load_callable_index()

    all_callsites: list[dict] = []
    for path_str, rec in file_by_path.items():
        path = REPO / path_str
        all_callsites.extend(extract_calls_in_file(path, callable_index, rec))

    with OUT_EDGES.open("w", encoding="utf-8") as f:
        for cs in all_callsites:
            f.write(json.dumps(cs, ensure_ascii=False) + "\n")

    aggs = build_aggregates(all_callsites, callable_index, file_by_path)

    with OUT_SUMMARY.open("w", encoding="utf-8") as f:
        json.dump(aggs["summary"], f, indent=2, ensure_ascii=False)
    with OUT_NODES.open("w", encoding="utf-8") as f:
        json.dump(aggs["nodes"], f, indent=2, ensure_ascii=False)
    with OUT_EDGES_AGG.open("w", encoding="utf-8") as f:
        json.dump(aggs["edges"], f, indent=2, ensure_ascii=False)

    s = aggs["summary"]
    print(f"wrote {s['total_callsites']} callsites to {OUT_EDGES.relative_to(REPO)}")
    print(f"  resolved: {s['resolved_callsites']} ({s['resolved_callsites']*100//max(s['total_callsites'],1)}%)")
    print(f"  unresolved: {s['unresolved_callsites']} (external/builtins)")
    print(f"  intra-file: {s['intra_file_callsites']}")
    print(f"  shadowed callable defs: {s['shadowed_callable_count']}")
    print(f"  files with zero live callers: {s['files_with_zero_live_callers']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
