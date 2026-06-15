#!/usr/bin/env python3
"""
MML Audit — MCP knowledge server (stdlib-only, zero dependencies).

Exposes the static-analysis artifacts in audit/data/ + the live .m source tree
as read-only tools an agent (Claude Code / Osprey) can query over MCP/stdio.

Two ways to run it:

  * As an MCP server (what an agent uses):
        python3 audit/mcp/server.py
    Speaks newline-delimited JSON-RPC 2.0 on stdin/stdout. Register it in
    .mcp.json and Claude Code spawns it automatically.

  * As a CLI (for dry-runs / the fallback demo):
        python3 audit/mcp/server.py list
        python3 audit/mcp/server.py call get_family BPM
        python3 audit/mcp/server.py call callers_of getmachineconfig.m
        python3 audit/mcp/server.py call read_source mml/getmachineconfig.m 1 40

Design notes:
  * Read-only. No write tools — writes belong to Osprey's approval chain.
  * read_source is path-confined to the repo root (rejects ../ escapes).
  * get_family keeps the three annotation tiers as DISTINCT labeled fields
    (verified / domain_context / needs_operator_input) so an agent never
    presents an open question as fact.
"""

import json
import gzip
import os
import sys

# ---------------------------------------------------------------------------
# Paths — resolved relative to this file so cwd doesn't matter.
# ---------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.normpath(os.path.join(HERE, "..", "data"))
ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))   # repo root = source tree
SOURCE_BUNDLE = os.path.join(DATA, "source_bundle.json.gz")


def _load_json(name):
    with open(os.path.join(DATA, name), encoding="utf-8") as fh:
        return json.load(fh)


def _load_jsonl(name):
    out = []
    with open(os.path.join(DATA, name), encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


# ---------------------------------------------------------------------------
# Load artifacts once at startup. (The 69 MB raw call graph is intentionally
# NOT loaded — we use the 1.1 MB aggregated edges instead.)
# ---------------------------------------------------------------------------
FAMILY_SCHEMA = _load_json("family_schema.json")
FAMILY_META = _load_json("family_meta.json")
ANNOTATIONS = _load_json("annotations.json")
EDGES_AGG = _load_json("call_graph_edges_agg.json")          # [{source,target,calls}]
FAMILY_REFS = _load_jsonl("family_refs.jsonl")               # [{file,family,total_refs,...}]

# Second wave (tools 6-20): the rest of the data model + inventory. All small
# JSON except file_index.jsonl (2.5 MB). The 69 MB raw call graph is STILL not
# loaded — callees_of works off the same 1.1 MB aggregate as callers_of.
AD_SCHEMA = _load_json("ad_schema.json")                     # 51 machine-config paths
MODE_OVERRIDES = _load_json("mode_overrides.json")           # 16 operational modes
GROUPS = _load_json("groups.json")                           # 82 MemberOf groups
SUBSYSTEMS = _load_json("subsystems.json")                   # 48 directory clusters
WORKFLOWS = _load_json("workflows.json")                     # 67 inferred workflows
FILE_INDEX = _load_jsonl("file_index.jsonl")                 # 2,424 file records
_SUMMARIES = {
    "file_index": _load_json("file_index_summary.json"),
    "ao_assignments": _load_json("ao_assignments_summary.json"),
    "call_graph": _load_json("call_graph_summary.json"),
    "parenless_call": _load_json("parenless_call_summary.json"),
}

# Source corpus. Preferred path: the self-contained gzipped bundle
# ({repo_relative_path: text}, ~2k ALS files, built by extractors/12_source_bundle.py).
# This is what makes the server shareable without the 136 MB on-disk .m tree.
# If the bundle is absent (e.g. a full local checkout that never built it), fall
# back to reading/walking the live tree under ROOT so local dev still works.
if os.path.isfile(SOURCE_BUNDLE):
    with gzip.open(SOURCE_BUNDLE, "rt", encoding="utf-8") as _fh:
        SOURCES = json.load(_fh)
else:
    SOURCES = None   # signals live-tree fallback

# Index family_refs by family for O(1) lookup.
_REFS_BY_FAMILY = {}
for _r in FAMILY_REFS:
    _REFS_BY_FAMILY.setdefault(_r["family"], []).append(_r)

# Indexes for the second-wave tools.
_FILE_BY_PATH = {r["path"]: r for r in FILE_INDEX}
_WF_LIST = WORKFLOWS.get("workflows", [])
_WF_BY_ID = {w["id"]: w for w in _WF_LIST}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _resolve_family(name):
    """Case-insensitive family-name resolution."""
    if name in FAMILY_SCHEMA:
        return name
    low = name.lower()
    for k in FAMILY_SCHEMA:
        if k.lower() == low:
            return k
    return None


def _matches_file(target, query):
    """Match a call-graph node path against a user query (full path or basename)."""
    if "/" in query:
        return target == query
    return target == query or target.endswith("/" + query)


# ---------------------------------------------------------------------------
# Tools — each returns a JSON-serializable Python object.
# ---------------------------------------------------------------------------
def tool_list_families(_args):
    """Overview of all 37 AO families: field count, assignment count, device count, groups."""
    rows = []
    for name, sch in sorted(FAMILY_SCHEMA.items()):
        meta = FAMILY_META.get(name, {})
        rows.append({
            "family": name,
            "n_fields": len(sch.get("fields", {})),
            "total_assignments": sch.get("total_assignments"),
            "device_count": meta.get("device_count"),
            "family_groups": meta.get("family_level_groups", []),
            "modes_touched": sch.get("modes_touched", []),
            "annotated": name in ANNOTATIONS.get("families", {}),
        })
    return {"family_count": len(rows), "families": rows}


def tool_get_family(args):
    """Full picture of one AO family, with verified facts separated from conjecture."""
    name = _resolve_family(args["name"])
    if not name:
        raise ValueError(f"Unknown family '{args['name']}'. Try list_families.")
    sch = FAMILY_SCHEMA[name]
    meta = FAMILY_META.get(name, {})
    ann = ANNOTATIONS.get("families", {}).get(name)

    # Verified tier — derived live from the extraction artifacts.
    fields = {}
    for fname, f in sch.get("fields", {}).items():
        fields[fname] = {
            "assignment_count": f.get("assignment_count"),
            "distinct_value_count": f.get("distinct_value_count"),
            "rhs_kinds": f.get("rhs_kinds", []),
        }
    refs = _REFS_BY_FAMILY.get(name, [])
    verified = {
        "family": name,
        "total_assignments": sch.get("total_assignments"),
        "field_count": len(sch.get("fields", {})),
        "fields": fields,
        "modes_touched": sch.get("modes_touched", []),
        "via_helpers": sch.get("via_helpers", []),
        "device_count": meta.get("device_count"),
        "device_count_via": meta.get("device_count_via", ""),
        "groups_touched": meta.get("all_groups_touched", []),
        "defined_at": sch.get("sources", [])[:10],
        "referencing_file_count": len(refs),
    }

    out = {"verified": verified}
    # Domain + operator-input tiers — hand-authored, kept explicitly separate.
    if ann:
        out["domain_context"] = {
            "title": ann.get("title"),
            "context": ann.get("context"),
            "_note": "General accelerator-physics knowledge. NOT ALS-specific config.",
        }
        out["needs_operator_input"] = {
            "open_questions": ann.get("unknowns", []),
            "_note": "Unverified. Conjecture / interview agenda — do not state as fact.",
        }
    else:
        out["domain_context"] = None
        out["needs_operator_input"] = {
            "open_questions": [],
            "_note": f"No hand-authored annotation for '{name}'.",
        }
    return out


def tool_files_for_family(args):
    """Which .m files reference a given family, ranked by reference count."""
    name = _resolve_family(args["name"])
    if not name:
        raise ValueError(f"Unknown family '{args['name']}'. Try list_families.")
    refs = sorted(_REFS_BY_FAMILY.get(name, []),
                  key=lambda r: r.get("total_refs", 0), reverse=True)
    limit = int(args.get("limit", 40))
    rows = [{
        "file": r["file"],
        "total_refs": r.get("total_refs"),
        "string_refs": r.get("string_refs"),
        "struct_refs": r.get("struct_refs"),
        "sample_lines": r.get("sample_lines", [])[:2],
    } for r in refs[:limit]]
    return {"family": name, "total_referencing_files": len(refs),
            "showing": len(rows), "files": rows}


def tool_callers_of(args):
    """Which files call a given file (aggregated, weighted by call count)."""
    q = args["file"]
    hits = [e for e in EDGES_AGG if _matches_file(e["target"], q)]
    hits.sort(key=lambda e: e.get("calls", 0), reverse=True)
    limit = int(args.get("limit", 50))
    rows = [{"caller": e["source"], "target": e["target"], "calls": e["calls"]}
            for e in hits[:limit]]
    return {"query": q, "total_callers": len(hits),
            "showing": len(rows), "callers": rows}


def tool_read_source(args):
    """Read a .m source file. Served from the embedded bundle when present
    (~2k ALS files), else from the live tree (path-confined to the repo root)."""
    rel = args["path"]
    if SOURCES is not None:
        # Bundle mode: the key set IS the allow-list, so traversal is impossible
        # by construction — no path normalization / escape check needed.
        if rel not in SOURCES:
            raise ValueError(f"No such file in source bundle: {rel}")
        lines = SOURCES[rel].splitlines(keepends=True)
    else:
        abs_path = os.path.normpath(os.path.join(ROOT, rel))
        # Confinement: resolved path must stay inside ROOT.
        if os.path.commonpath([os.path.realpath(abs_path), os.path.realpath(ROOT)]) != os.path.realpath(ROOT):
            raise ValueError(f"Path '{rel}' escapes the repo root — refused.")
        if not os.path.isfile(abs_path):
            raise ValueError(f"No such file: {rel}")
        with open(abs_path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()

    start = int(args["start_line"]) if args.get("start_line") else 1
    end = int(args["end_line"]) if args.get("end_line") else len(lines)
    start = max(1, start)
    end = min(len(lines), end)
    # Cap an unbounded read so we never dump a 1,500-line file into context.
    truncated = False
    if not args.get("end_line") and (end - start) > 400:
        end = start + 400 - 1
        truncated = True
    body = "".join(f"{i:>5}  {lines[i-1]}" for i in range(start, end + 1))
    return {"path": rel, "total_lines": len(lines),
            "shown": [start, end], "truncated": truncated, "source": body}


# ---------------------------------------------------------------------------
# Second-wave helpers + tools (6-20)
# ---------------------------------------------------------------------------
def _compact(value, max_items=20):
    """Bound a possibly-large summary value so stats() never floods context."""
    if isinstance(value, dict):
        items = list(value.items())
        out = {k: v for k, v in items[:max_items]}
        if len(items) > max_items:
            out["_truncated"] = f"{len(items) - max_items} more keys omitted"
        return out
    if isinstance(value, list):
        if len(value) > max_items:
            return value[:max_items] + [f"_truncated: {len(value) - max_items} more omitted"]
        return value
    return value


def _resolve_in(mapping, name):
    """Case-insensitive key resolution against a dict-keyed artifact."""
    if name in mapping:
        return name
    low = name.lower()
    for k in mapping:
        if k.lower() == low:
            return k
    return None


def tool_stats(_args):
    """Headline counts from the four precomputed summaries (Overview tab)."""
    return {label: {k: _compact(v) for k, v in summ.items()}
            for label, summ in _SUMMARIES.items()}


def tool_list_ad_paths(_args):
    """All 51 AD machine-config paths (Energy, MCF, OperationalMode, …)."""
    rows = []
    for name, e in sorted(AD_SCHEMA.items()):
        rows.append({
            "path": name,
            "assignment_count": e.get("assignment_count"),
            "distinct_value_count": e.get("distinct_value_count"),
            "rhs_kinds": e.get("rhs_kinds", []),
            "mode_specific": e.get("mode_specific"),
            "comment": e.get("comment"),
        })
    return {"ad_path_count": len(rows), "ad_paths": rows}


def tool_get_ad_path(args):
    """Full schema for one AD machine-config path. All facts are verified."""
    name = _resolve_in(AD_SCHEMA, args["name"])
    if not name:
        raise ValueError(f"Unknown AD path '{args['name']}'. Try list_ad_paths.")
    e = AD_SCHEMA[name]
    verified = {
        "ad_path": name,
        "assignment_count": e.get("assignment_count"),
        "distinct_value_count": e.get("distinct_value_count"),
        "values": _compact(e.get("values"), 40),
        "rhs_kinds": e.get("rhs_kinds", []),
        "comment": e.get("comment"),
        "is_indexed_update": e.get("is_indexed_update"),
        "any_indexed_update": e.get("any_indexed_update"),
        "mode_specific": e.get("mode_specific"),
        "defined_at": e.get("sources", [])[:10],
    }
    return {"verified": verified, "domain_context": None,
            "needs_operator_input": {"open_questions": [],
                "_note": f"No hand-authored annotation for AD path '{name}'."}}


def tool_list_modes(_args):
    """The 16 operational modes (TopOff, the person-named modes, the dup 99, …)."""
    def _key(kv):
        return int(kv[0]) if kv[0].lstrip("-").isdigit() else 1 << 30
    rows = [{"id": mid, "name": m.get("name"), "assignment_count": m.get("assignment_count")}
            for mid, m in sorted(MODE_OVERRIDES.items(), key=_key)]
    return {"mode_count": len(rows), "modes": rows}


def tool_get_mode(args):
    """The exact per-mode field deltas applied by setoperationalmode."""
    mid = str(args["id"])
    m = MODE_OVERRIDES.get(mid)
    if not m:
        raise ValueError(f"Unknown mode '{mid}'. Try list_modes.")
    assigns = m.get("assignments", [])
    return {"id": mid, "name": m.get("name"),
            "assignment_count": m.get("assignment_count"),
            "assignments": assigns[:200],
            "assignments_truncated": len(assigns) > 200}


def tool_list_groups(_args):
    """The 82 MemberOf groups — the implicit operational taxonomy."""
    rows = [{"group": name, "family_count": g.get("family_count"),
             "field_count": g.get("field_count"), "fields_total": g.get("fields_total")}
            for name, g in sorted(GROUPS.items(),
                                  key=lambda kv: kv[1].get("family_count", 0), reverse=True)]
    return {"group_count": len(rows), "groups": rows}


def tool_get_group(args):
    """Which families/fields belong to one MemberOf group (reverse index)."""
    name = _resolve_in(GROUPS, args["name"])
    if not name:
        raise ValueError(f"Unknown group '{args['name']}'. Try list_groups.")
    g = GROUPS[name]
    fields = g.get("fields", [])
    return {"group": name, "family_count": g.get("family_count"),
            "field_count": g.get("field_count"), "fields_total": g.get("fields_total"),
            "families": g.get("families", []),
            "fields": fields[:100], "fields_truncated": len(fields) > 100}


def tool_list_subsystems(_args):
    """The 48 directory clusters with internal-cohesion scores."""
    rows = [{"cluster": c.get("cluster"), "file_count": c.get("file_count"),
             "archive_files": c.get("archive_files"), "total_lines": c.get("total_lines"),
             "cohesion": c.get("cohesion")}
            for c in SUBSYSTEMS.get("clusters", [])]
    rows.sort(key=lambda r: r.get("file_count") or 0, reverse=True)
    return {"cluster_count": SUBSYSTEMS.get("cluster_count", len(rows)), "clusters": rows}


def tool_list_workflows(_args):
    """The 67 inferred workflows (TopOff, LOCO, Orbit, Inject, …)."""
    rows = [{"id": w.get("id"), "name": w.get("name"), "kind": w.get("kind"),
             "scale": w.get("scale"), "description": w.get("description"),
             "owned_count": w.get("owned_count"), "lead_file": w.get("lead_file")}
            for w in _WF_LIST]
    rows.sort(key=lambda r: r.get("owned_count") or 0, reverse=True)
    return {"workflow_count": len(rows), "workflows": rows}


def tool_get_workflow(args):
    """One inferred workflow's procedures, families, verbs and lead file."""
    wid = args["id"]
    w = _WF_BY_ID.get(wid) or next((c for c in _WF_LIST if c.get("name") == wid), None)
    if not w:
        raise ValueError(f"Unknown workflow '{wid}'. Try list_workflows.")
    spec = w.get("specific", [])
    return {
        "id": w.get("id"), "name": w.get("name"), "kind": w.get("kind"),
        "scale": w.get("scale"), "description": w.get("description"),
        "lead_file": w.get("lead_file"),
        "owned_count": w.get("owned_count"), "specific_count": w.get("specific_count"),
        "shared_count": w.get("shared_count"),
        "owned_lines": w.get("owned_lines"), "specific_lines": w.get("specific_lines"),
        "unique_procedures": w.get("unique_procedures"),
        "historical_revisions": w.get("historical_revisions"),
        "top_families": w.get("top_families", []), "top_verbs": w.get("top_verbs", []),
        "specific": spec[:40], "specific_truncated": len(spec) > 40,
        "sample_owned": w.get("sample_owned", []),
        "_note": "Inferred (prefix/keyword/reachability), not operator-verified. "
                 "Ground-truth review pending.",
    }


def tool_get_file(args):
    """The file-index record for one .m file (signature, help, line count, flags)."""
    path = args["path"]
    rec = _FILE_BY_PATH.get(path)
    if not rec:
        hits = [r for r in FILE_INDEX
                if r["path"] == path or r["path"].endswith("/" + path) or r.get("name") == path]
        if len(hits) == 1:
            rec = hits[0]
        elif len(hits) > 1:
            return {"query": path, "ambiguous": True,
                    "matches": [h["path"] for h in hits[:25]]}
    if not rec:
        raise ValueError(f"No file-index record for '{path}'. Try search_files.")
    return rec


def tool_search_files(args):
    """Find files by path/name substring (the Files tab's filter)."""
    q = args["q"].lower()
    limit = int(args.get("limit", 40))
    hits = [r for r in FILE_INDEX
            if q in r["path"].lower() or q in (r.get("name") or "").lower()]
    rows = [{"path": r["path"], "name": r.get("name"), "kind": r.get("kind"),
             "line_count": r.get("line_count"), "summary": r.get("summary"),
             "in_legacy_folder": r.get("in_legacy_folder")} for r in hits[:limit]]
    return {"query": args["q"], "total_matches": len(hits),
            "showing": len(rows), "files": rows}


def tool_callees_of(args):
    """Which files a given file calls (forward edges — the transpose of callers_of)."""
    q = args["file"]
    hits = [e for e in EDGES_AGG if _matches_file(e["source"], q)]
    hits.sort(key=lambda e: e.get("calls", 0), reverse=True)
    limit = int(args.get("limit", 50))
    rows = [{"caller": e["source"], "callee": e["target"], "calls": e["calls"]}
            for e in hits[:limit]]
    return {"query": q, "total_callees": len(hits), "showing": len(rows), "callees": rows}


def tool_get_verb(args):
    """Usage stats for one verb (computed live from the call graph) + annotation tiers."""
    raw = args["name"]
    verb = raw[:-2] if raw.endswith(".m") else raw
    hits = [e for e in EDGES_AGG if _matches_file(e["target"], verb + ".m")]
    hits.sort(key=lambda e: e.get("calls", 0), reverse=True)
    verified = {
        "verb": verb,
        "total_calls": sum(e.get("calls", 0) for e in hits),
        "distinct_callers": len(hits),
        "top_callers": [{"caller": e["source"], "calls": e["calls"]} for e in hits[:25]],
    }
    out = {"verified": verified}
    ann = ANNOTATIONS.get("verbs", {}).get(verb)
    if ann:
        out["signature_inferred"] = {
            "value": ann.get("signature_inferred"),
            "_note": "Inferred signature — NOT verified from a declaration.",
        }
        out["domain_context"] = {
            "title": ann.get("title"), "context": ann.get("context"),
            "_note": "General accelerator-physics / MML knowledge. NOT ALS-specific config.",
        }
        out["needs_operator_input"] = {
            "open_questions": ann.get("unknowns", []),
            "_note": "Unverified. Conjecture / interview agenda — do not state as fact.",
        }
    else:
        out["domain_context"] = None
        out["needs_operator_input"] = {
            "open_questions": [],
            "_note": f"No hand-authored annotation for verb '{verb}'.",
        }
    return out


def _iter_sources():
    """Yield (repo_relative_path, file_text) for every searchable .m file.

    Bundle mode (SOURCES loaded): the ~2k ALS files in the embedded bundle.
    Fallback: walk the live tree under ROOT, skipping audit/ and .git/."""
    if SOURCES is not None:
        yield from SOURCES.items()
        return
    skip = {"audit", ".git"}
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for fn in filenames:
            if not fn.endswith(".m"):
                continue
            ap = os.path.join(dirpath, fn)
            try:
                with open(ap, encoding="utf-8", errors="replace") as fh:
                    yield os.path.relpath(ap, ROOT), fh.read()
            except OSError:
                continue


def tool_search_source(args):
    """Full-text grep over the source corpus (case-insensitive substring), capped.

    With the bundle present this searches the ~2k ALS files it contains; without
    it, the full live .m tree. Either way the matching logic is identical."""
    raw = args["q"]
    q = raw.lower()
    limit = int(args.get("limit", 50))
    matches = []
    files_with_hits = 0
    truncated = False
    for rel, text in _iter_sources():
        if truncated:
            break
        file_hit = False
        for i, line in enumerate(text.splitlines(), 1):
            if q in line.lower():
                snippet = line if len(line) <= 240 else line[:240] + " …"
                matches.append({"file": rel, "line": i, "text": snippet.strip()})
                file_hit = True
                if len(matches) >= limit:
                    truncated = True
                    break
        if file_hit:
            files_with_hits += 1
    return {"query": raw, "showing": len(matches),
            "files_with_hits": files_with_hits, "truncated": truncated,
            "matches": matches}


# ---------------------------------------------------------------------------
# Tool registry: name -> (handler, description, inputSchema)
# ---------------------------------------------------------------------------
TOOLS = {
    "list_families": (
        tool_list_families,
        "List all 37 AO (Accelerator Object) families with field counts, "
        "assignment counts, device counts and group memberships. Start here.",
        {"type": "object", "properties": {}},
    ),
    "get_family": (
        tool_get_family,
        "Full schema + context for one AO family. Returns three SEPARATE tiers: "
        "'verified' (live facts from the code), 'domain_context' (general physics, "
        "not ALS config), and 'needs_operator_input' (open questions — NOT fact).",
        {"type": "object",
         "properties": {"name": {"type": "string", "description": "Family name, e.g. BPM, HCM, QF"}},
         "required": ["name"]},
    ),
    "files_for_family": (
        tool_files_for_family,
        "List the .m files that reference a given family, ranked by reference count, "
        "with sample lines.",
        {"type": "object",
         "properties": {"name": {"type": "string"},
                        "limit": {"type": "integer", "description": "max files (default 40)"}},
         "required": ["name"]},
    ),
    "callers_of": (
        tool_callers_of,
        "Which files call a given file. Accepts a basename (getmachineconfig.m) or a "
        "full path (mml/getmachineconfig.m). Weighted by call count.",
        {"type": "object",
         "properties": {"file": {"type": "string"},
                        "limit": {"type": "integer", "description": "max callers (default 50)"}},
         "required": ["file"]},
    ),
    "read_source": (
        tool_read_source,
        "Read a .m source file from the live MML tree, optionally a line range. "
        "Path-confined to the repo root.",
        {"type": "object",
         "properties": {"path": {"type": "string", "description": "repo-relative path, e.g. mml/getmachineconfig.m"},
                        "start_line": {"type": "integer"},
                        "end_line": {"type": "integer"}},
         "required": ["path"]},
    ),
    "stats": (
        tool_stats,
        "Headline counts for the whole audit (files, assignments, callsites, "
        "shadowing, paren-less calls) from the precomputed summaries.",
        {"type": "object", "properties": {}},
    ),
    "list_ad_paths": (
        tool_list_ad_paths,
        "List all 51 AD (machine-config) paths — Energy, MCF, OperationalMode, "
        "ATModel, Circumference, … — with assignment/value counts. The AD half "
        "of the data model (AO families are the other half).",
        {"type": "object", "properties": {}},
    ),
    "get_ad_path": (
        tool_get_ad_path,
        "Full schema for one AD machine-config path: values, RHS kinds, comment, "
        "mode-specificity, source sites. All verified from the code.",
        {"type": "object",
         "properties": {"name": {"type": "string", "description": "AD path, e.g. Energy, MCF, OperationalMode"}},
         "required": ["name"]},
    ),
    "list_modes": (
        tool_list_modes,
        "List the 16 operational modes from setoperationalmode (TopOff, the "
        "person-named Greg/Tom/Christoph modes, the duplicate 99, …) with their "
        "per-mode assignment counts.",
        {"type": "object", "properties": {}},
    ),
    "get_mode": (
        tool_get_mode,
        "The exact field deltas one operational mode applies (e.g. mode 1 = "
        "TopOff/production). Pass the numeric mode id.",
        {"type": "object",
         "properties": {"id": {"type": "string", "description": "mode id, e.g. 1, 4, 99"}},
         "required": ["id"]},
    ),
    "list_groups": (
        tool_list_groups,
        "List the 82 MemberOf groups — the implicit operational taxonomy the MML "
        "iterates over (MachineConfig, Magnet, …) — with family/field counts.",
        {"type": "object", "properties": {}},
    ),
    "get_group": (
        tool_get_group,
        "Which families and fields belong to one MemberOf group. This is the "
        "reverse index get_family doesn't give you (group -> members).",
        {"type": "object",
         "properties": {"name": {"type": "string", "description": "group name, e.g. MachineConfig"}},
         "required": ["name"]},
    ),
    "list_subsystems": (
        tool_list_subsystems,
        "List the 48 directory clusters with internal-cohesion scores and line "
        "counts (e.g. the StorageRing/Topoff anomaly: 19 files, ~30k lines, zero cohesion).",
        {"type": "object", "properties": {}},
    ),
    "list_workflows": (
        tool_list_workflows,
        "List the 67 inferred workflows (TopOff, LOCO, Orbit, Inject, …). "
        "Inferred via prefix/keyword/reachability — a starting list, not operator-verified.",
        {"type": "object", "properties": {}},
    ),
    "get_workflow": (
        tool_get_workflow,
        "One inferred workflow's owned/specific procedures, top families & verbs, "
        "and lead file. Accepts the workflow id (e.g. prefix:bpm) or its name.",
        {"type": "object",
         "properties": {"id": {"type": "string", "description": "workflow id or name, e.g. prefix:bpm"}},
         "required": ["id"]},
    ),
    "get_verb": (
        tool_get_verb,
        "Usage stats for one verb (setpv, getpv, getfamilydata, …) computed live "
        "from the call graph — total calls, distinct callers, top callers — plus "
        "the separated annotation tiers (inferred-signature / domain / open-questions).",
        {"type": "object",
         "properties": {"name": {"type": "string", "description": "verb name, e.g. setpv"}},
         "required": ["name"]},
    ),
    "get_file": (
        tool_get_file,
        "The file-index record for one .m file: signature, help text, line count, "
        "kind, dynamic-dispatch and legacy-folder flags. Accepts a full path or basename.",
        {"type": "object",
         "properties": {"path": {"type": "string", "description": "repo-relative path or basename"}},
         "required": ["path"]},
    ),
    "search_files": (
        tool_search_files,
        "Find .m files by path/name substring (the Files-tab filter). Returns "
        "path, kind, line count and one-line summary per hit.",
        {"type": "object",
         "properties": {"q": {"type": "string"},
                        "limit": {"type": "integer", "description": "max files (default 40)"}},
         "required": ["q"]},
    ),
    "callees_of": (
        tool_callees_of,
        "Which files a given file CALLS (forward call edges — the transpose of "
        "callers_of). Accepts a basename or full path. Weighted by call count.",
        {"type": "object",
         "properties": {"file": {"type": "string"},
                        "limit": {"type": "integer", "description": "max callees (default 50)"}},
         "required": ["file"]},
    ),
    "search_source": (
        tool_search_source,
        "Full-text grep over the live .m source tree (case-insensitive substring). "
        "Returns file:line:text hits, capped at `limit`. Finds files by CONTENT "
        "(comments, PV-name patterns, feval sites) that search_files can't.",
        {"type": "object",
         "properties": {"q": {"type": "string"},
                        "limit": {"type": "integer", "description": "max matches (default 50)"}},
         "required": ["q"]},
    ),
}


# ---------------------------------------------------------------------------
# MCP stdio server (newline-delimited JSON-RPC 2.0)
# ---------------------------------------------------------------------------
def _result(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _handle(msg):
    """Return a response dict, or None for notifications (no reply)."""
    method = msg.get("method")
    req_id = msg.get("id")

    if method == "initialize":
        proto = (msg.get("params") or {}).get("protocolVersion", "2024-11-05")
        return _result(req_id, {
            "protocolVersion": proto,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "mml-audit", "version": "0.1.0"},
        })

    if method in ("notifications/initialized", "initialized"):
        return None  # notification, no response

    if method == "ping":
        return _result(req_id, {})

    if method == "tools/list":
        return _result(req_id, {"tools": [
            {"name": n, "description": d, "inputSchema": s}
            for n, (_fn, d, s) in TOOLS.items()
        ]})

    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        entry = TOOLS.get(name)
        if not entry:
            return _result(req_id, {
                "content": [{"type": "text", "text": f"Unknown tool: {name}"}],
                "isError": True})
        try:
            payload = entry[0](args)
            text = json.dumps(payload, indent=2, ensure_ascii=False)
            return _result(req_id, {"content": [{"type": "text", "text": text}],
                                    "isError": False})
        except Exception as exc:  # surface tool errors to the agent, don't crash
            return _result(req_id, {
                "content": [{"type": "text", "text": f"Error in {name}: {exc}"}],
                "isError": True})

    if req_id is not None:
        return _error(req_id, -32601, f"Method not found: {method}")
    return None


def serve_stdio():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = _handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


# ---------------------------------------------------------------------------
# CLI mode — for dry-runs and the Option-C fallback demo.
# ---------------------------------------------------------------------------
def cli(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print("Usage:\n  server.py                       # run as MCP server (stdio)\n"
              "  server.py list                  # list tools\n"
              "  server.py call <tool> [args...] # invoke a tool directly")
        return
    if argv[0] == "list":
        for n, (_fn, d, _s) in TOOLS.items():
            print(f"{n:18} {d}")
        return
    if argv[0] == "call":
        name = argv[1]
        rest = argv[2:]
        fn = TOOLS[name][0]
        # Map positional CLI args to the tool's schema properties, in order.
        props = list(TOOLS[name][2].get("properties", {}).keys())
        args = {}
        for key, val in zip(props, rest):
            args[key] = int(val) if val.lstrip("-").isdigit() else val
        try:
            print(json.dumps(fn(args), indent=2, ensure_ascii=False))
        except Exception as exc:
            print(f"Error in {name}: {exc}")
        return
    print(f"Unknown CLI command: {argv[0]}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cli(sys.argv[1:])
    else:
        serve_stdio()
