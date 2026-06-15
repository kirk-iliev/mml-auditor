#!/usr/bin/env python3
"""
Browser builder (slice 6 of the audit).

Bundles the audit data into a single self-contained HTML page that opens
in any browser straight from the filesystem (no server, no build step).
The page is for poking around the artifacts interactively — useful both
for the auditor and for sharing with colleagues who don't want to run
jq queries.

Inputs:
  audit/data/call_graph_nodes.json
  audit/data/call_graph_edges_agg.json
  audit/data/call_graph_summary.json
  audit/data/family_schema.json
  audit/data/ad_schema.json
  audit/data/mode_overrides.json
  audit/data/file_index.jsonl              (for help text + summaries)

Output:
  audit/browser.html
"""

from __future__ import annotations

import base64
import gzip
import json
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path("/home/kiliev/Documents/Code/LBL/mmlt")
DATA = REPO / "audit" / "data"
OUT = REPO / "audit" / "browser.html"


def load_json(name: str):
    path = DATA / name
    if not path.exists():
        sys.exit(f"error: missing {path} — run earlier extractors first.")
    with path.open() as f:
        return json.load(f)


def load_jsonl(name: str) -> list[dict]:
    path = DATA / name
    if not path.exists():
        sys.exit(f"error: missing {path}")
    return [json.loads(line) for line in path.open() if line.strip()]


def load_jsonl_optional(name: str) -> list[dict]:
    """Same as load_jsonl, but returns [] silently if the file is missing."""
    path = DATA / name
    if not path.exists():
        return []
    return [json.loads(line) for line in path.open() if line.strip()]


def main() -> int:
    nodes = load_json("call_graph_nodes.json")
    edges_agg = load_json("call_graph_edges_agg.json")
    cg_summary = load_json("call_graph_summary.json")
    families = load_json("family_schema.json")
    ad_schema = load_json("ad_schema.json")
    modes = load_json("mode_overrides.json")

    # Cross-reference data (slice 7). Optional — if the extractor hasn't been
    # run yet, the browser still works without the new panels.
    family_refs = load_jsonl_optional("family_refs.jsonl")
    ad_refs = load_jsonl_optional("ad_refs.jsonl")

    # Paren-less call edges (slice 8). Optional too. We merge them into the
    # same callers/callees adjacency so the browser sees a unified graph,
    # but we keep a per-style breakdown on each edge so they can be
    # distinguished visually.
    parenless_edges = load_jsonl_optional("parenless_call_edges.jsonl")

    # Subsystems (slice 9). Optional.
    subsystems_path = DATA / "subsystems.json"
    subsystems = json.load(subsystems_path.open()) if subsystems_path.exists() else None

    # Groups + per-family metadata (slice 10). Optional.
    groups_path = DATA / "groups.json"
    groups_data = json.load(groups_path.open()) if groups_path.exists() else None
    fmeta_path = DATA / "family_meta.json"
    family_meta = json.load(fmeta_path.open()) if fmeta_path.exists() else {}

    # Workflows (slice 11). Optional.
    workflows_path = DATA / "workflows.json"
    workflows_data = json.load(workflows_path.open()) if workflows_path.exists() else None

    # Hand-authored reference annotations (confidence-tiered). Optional.
    annotations_path = DATA / "annotations.json"
    annotations = json.load(annotations_path.open()) if annotations_path.exists() else {}

    # Load the full file index here — used by the API-surface and treemap-data
    # blocks below, and the slim version is derived from it.
    file_index_full = load_jsonl("file_index.jsonl")

    # Pre-compute the "API surface" view: top in-tree callables ranked by total
    # call count (paren + paren-less), one row per callable. This is the single
    # visual the audit needs — it answers "what verbs does any Python port or
    # agentic tool surface need to provide?" The bars are the contract.
    #
    # The callee name on each edge is the filename stem (MATLAB dispatches by
    # filename). Edges within the same file (recursion, self-help) are
    # excluded — they're noise for an API-surface view.
    path_precedence = [
        "mml", "machine/ALS/Common", "machine/ALS/Booster",
        "machine/ALS/BTS", "machine/ALS/GTB", "machine/ALS/StorageRing",
    ]

    def path_root(p: str) -> str:
        for r in path_precedence:
            if p and p.startswith(r + "/"):
                return r
        return "?"

    api_calls: dict[str, dict] = defaultdict(
        lambda: {"paren": 0, "parenless": 0, "callers": set(), "resolved_path": None}
    )
    for e in edges_agg:
        if e["source"] == e["target"]:
            continue
        name = Path(e["target"]).stem
        api_calls[name]["paren"] += e["calls"]
        api_calls[name]["callers"].add(e["source"])
        api_calls[name]["resolved_path"] = e["target"]
    for e in parenless_edges:
        if e["caller"] == e["callee_path"]:
            continue
        name = e["callee_name"]
        api_calls[name]["parenless"] += 1
        api_calls[name]["callers"].add(e["caller"])
        api_calls[name]["resolved_path"] = e["callee_path"]

    api_surface = []
    for name, info in api_calls.items():
        total = info["paren"] + info["parenless"]
        if total < 5:  # ignore trivially-called names so the list stays focused
            continue
        rp = info["resolved_path"]
        api_surface.append({
            "name": name,
            "total": total,
            "paren": info["paren"],
            "parenless": info["parenless"],
            "caller_count": len(info["callers"]),
            "resolved_path": rp,
            "root": path_root(rp),
        })
    api_surface.sort(key=lambda x: -x["total"])
    api_surface = api_surface[:100]   # the top hundred is plenty for a one-screen surface

    # File index — keep a slim subset so the browser stays under ~5MB.
    file_index_slim = {
        r["path"]: {
            "name": r["name"],
            "summary": r["summary"],
            "kind": r["kind"],
            "lines": r["line_count"],
            "mtime": r["mtime"],
            "function": r["function"],
            "in_class_folder": r["in_class_folder"],
            "in_legacy_folder": r["in_legacy_folder"],
            "name_matches_file": r["name_matches_file"],
            "subfunction_count": r["subfunction_count"],
            "dynamic_dispatch": r["dynamic_dispatch"],
            # truncate help to first 600 chars; full text is still in jsonl
            "help": r["help_text"][:600],
        }
        for r in file_index_full
    }

    # Build per-file caller/callee adjacency from the aggregated edges.
    # We collapse paren and paren-less edges into the same (src,tgt) bucket
    # but track the per-style call counts so the UI can break them out.
    edge_buckets: dict[tuple[str, str], dict] = {}
    for e in edges_agg:
        b = edge_buckets.setdefault((e["source"], e["target"]),
                                     {"paren": 0, "parenless": 0})
        b["paren"] += e["calls"]
    for e in parenless_edges:
        key = (e["caller"], e["callee_path"])
        b = edge_buckets.setdefault(key, {"paren": 0, "parenless": 0})
        b["parenless"] += 1

    callers: dict[str, list[dict]] = {}
    callees: dict[str, list[dict]] = {}
    for (src, tgt), counts in edge_buckets.items():
        total = counts["paren"] + counts["parenless"]
        callers.setdefault(tgt, []).append(
            {"path": src, "calls": total, "paren": counts["paren"], "parenless": counts["parenless"]})
        callees.setdefault(src, []).append(
            {"path": tgt, "calls": total, "paren": counts["paren"], "parenless": counts["parenless"]})

    # Update per-node in/out counts to include paren-less edges so the
    # Files-tab "in" column reflects reality and the zero-callers list
    # honors the better-quality edges too.
    out_count: dict[str, int] = defaultdict(int)
    in_count_live: dict[str, int] = defaultdict(int)
    in_count_total: dict[str, int] = defaultdict(int)

    def is_archive(p: str) -> bool:
        return any(part in {"Old", "old", "LegacyFiles", "_Attic", "Attic"}
                   for part in Path(p).parts)

    for (src, tgt), counts in edge_buckets.items():
        n = counts["paren"] + counts["parenless"]
        out_count[src] += n
        in_count_total[tgt] += n
        if not is_archive(src):
            in_count_live[tgt] += n

    for n in nodes:
        n["out"] = out_count.get(n["id"], 0)
        n["in_total"] = in_count_total.get(n["id"], 0)
        n["in_live_only"] = in_count_live.get(n["id"], 0)

    # Aggregate cross-references for both directions: family→files and file→
    # families. Same for AD. Pre-aggregating in Python keeps the browser code
    # simple (no client-side reduce over thousands of records).
    family_to_files: dict[str, list[dict]] = {}
    file_to_families: dict[str, list[dict]] = {}
    for r in family_refs:
        family_to_files.setdefault(r["family"], []).append({
            "file": r["file"],
            "total_refs": r["total_refs"],
            "string_refs": r["string_refs"],
            "struct_refs": r["struct_refs"],
            "sample_lines": r["sample_lines"],
        })
        file_to_families.setdefault(r["file"], []).append({
            "family": r["family"],
            "total_refs": r["total_refs"],
            "string_refs": r["string_refs"],
            "struct_refs": r["struct_refs"],
        })
    for arr in family_to_files.values():
        arr.sort(key=lambda x: -x["total_refs"])
    for arr in file_to_families.values():
        arr.sort(key=lambda x: -x["total_refs"])

    ad_to_files: dict[str, list[dict]] = {}
    file_to_ads: dict[str, list[dict]] = {}
    for r in ad_refs:
        ad_to_files.setdefault(r["ad_path"], []).append({
            "file": r["file"], "refs": r["refs"], "sample_lines": r["sample_lines"],
        })
        file_to_ads.setdefault(r["file"], []).append({
            "ad_path": r["ad_path"], "refs": r["refs"],
        })
    for arr in ad_to_files.values():
        arr.sort(key=lambda x: -x["refs"])
    for arr in file_to_ads.values():
        arr.sort(key=lambda x: -x["refs"])

    payload = {
        "nodes": nodes,
        "edges": edges_agg,
        "callers": callers,
        "callees": callees,
        "file_index": file_index_slim,
        "families": families,
        "ad": ad_schema,
        "modes": modes,
        "cg_summary": cg_summary,
        "family_to_files": family_to_files,
        "file_to_families": file_to_families,
        "ad_to_files": ad_to_files,
        "file_to_ads": file_to_ads,
        "subsystems": subsystems,
        "api_surface": api_surface,
        "groups": groups_data,
        "family_meta": family_meta,
        "workflows": workflows_data,
        "annotations": annotations,
    }
    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    # Sanity log of the embedded payload size.
    size_mb = len(payload_json.encode("utf-8")) / (1024 * 1024)
    print(f"payload size: {size_mb:.1f} MB")

    # Bundle live (non-archive) source code, gzipped + base64. Archive files
    # are deliberately excluded — they're not on the path in production, so
    # embedding ~18 MB of legacy source for occasional forensic curiosity
    # isn't worth the file-size cost. Archive files show a "open from disk"
    # hint in the UI instead.
    source_dict = {}
    archive_paths: list[str] = []
    for rec in file_index_full:
        is_archive = rec["in_legacy_folder"] or any(
            p in {"Old", "old", "_Attic", "Attic"}
            for p in Path(rec["path"]).parts
        )
        if is_archive:
            archive_paths.append(rec["path"])
            continue
        try:
            text = (REPO / rec["path"]).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = (REPO / rec["path"]).read_text(encoding="latin-1")
        source_dict[rec["path"]] = text

    source_raw_json = json.dumps(source_dict, ensure_ascii=False, separators=(",", ":"))
    source_compressed = gzip.compress(source_raw_json.encode("utf-8"), compresslevel=9)
    source_b64 = base64.b64encode(source_compressed).decode("ascii")
    print(f"source bundle: {len(source_dict)} live files, "
          f"{len(source_raw_json)/1024/1024:.1f} MB raw -> "
          f"{len(source_compressed)/1024/1024:.1f} MB gz -> "
          f"{len(source_b64)/1024/1024:.1f} MB base64")
    print(f"  excluded {len(archive_paths)} archive files (open from disk in UI)")

    # Inline D3 from a vendored file so the HTML works fully offline / on
    # network-restricted machines. If the vendored file is missing, fall back
    # to the CDN script tag with a warning.
    d3_path = Path(__file__).resolve().parent / "d3.v7.min.js"
    if d3_path.exists():
        d3_block = (
            '<script>\n/* D3 v7 inlined for offline portability */\n'
            + d3_path.read_text(encoding="utf-8")
            + '\n</script>'
        )
        print(f"inlined D3 from {d3_path.relative_to(REPO)} "
              f"({d3_path.stat().st_size//1024} KB)")
    else:
        d3_block = '<script src="https://d3js.org/d3.v7.min.js"></script>'
        print("warn: vendored d3.v7.min.js missing — using CDN (won't work offline)")

    # Inline pako (gzip in pure JS) for on-demand source decompression.
    pako_path = Path(__file__).resolve().parent / "pako.min.js"
    if pako_path.exists():
        pako_block = (
            '<script>\n/* pako 2.x inlined for source decompression */\n'
            + pako_path.read_text(encoding="utf-8")
            + '\n</script>'
        )
        print(f"inlined pako from {pako_path.relative_to(REPO)} "
              f"({pako_path.stat().st_size//1024} KB)")
    else:
        pako_block = ('<script>console.error("pako not bundled — source view will not work");'
                      'window.pako = null;</script>')
        print("warn: vendored pako.min.js missing — source view will be disabled")

    html = (HTML_TEMPLATE
            .replace("__PAYLOAD__", payload_json)
            .replace("__D3__", d3_block)
            .replace("__PAKO__", pako_block)
            .replace("__SOURCE_B64__", source_b64))
    OUT.write_text(html, encoding="utf-8")
    print(f"wrote {OUT.relative_to(REPO)}")
    return 0


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>MML Audit Browser</title>
<style>
  :root {
    --bg: #0f1115;
    --panel: #161a22;
    --panel-2: #1d2230;
    --border: #2a3140;
    --text: #d8dde7;
    --muted: #7a8295;
    --accent: #6aa9ff;
    --accent-2: #f5b95c;
    --warn: #e07a5f;
    --good: #7cd992;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font: 13px/1.45 -apple-system, "SF Mono", Menlo, Consolas, monospace;
  }
  header {
    padding: 12px 20px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: baseline;
    gap: 20px;
  }
  header h1 { font-size: 14px; margin: 0; letter-spacing: 0.5px; }
  header .meta { color: var(--muted); font-size: 12px; }
  nav {
    display: flex;
    gap: 0;
    border-bottom: 1px solid var(--border);
    padding: 0 20px;
  }
  nav button {
    background: none; border: none; color: var(--muted);
    font: inherit; padding: 10px 16px; cursor: pointer;
    border-bottom: 2px solid transparent;
  }
  nav button.active { color: var(--accent); border-bottom-color: var(--accent); }
  nav button:hover { color: var(--text); }
  main { padding: 18px 20px; }
  .grid { display: grid; gap: 16px; }
  .cols-2 { grid-template-columns: 1fr 1fr; }
  .cols-3 { grid-template-columns: 1fr 1fr 1fr; }
  .card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 14px 16px;
  }
  .card h2 {
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: var(--muted);
    margin: 0 0 10px 0;
  }
  .stat {
    font-size: 22px;
    color: var(--text);
    font-weight: 500;
  }
  .stat-row { display: flex; gap: 18px; flex-wrap: wrap; }
  .stat-row > div { min-width: 120px; }
  .stat-label { color: var(--muted); font-size: 11px; margin-top: 2px; }

  table {
    width: 100%; border-collapse: collapse; font-size: 12px;
  }
  th, td {
    text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--border);
    vertical-align: top;
  }
  th {
    color: var(--muted); font-weight: 600; cursor: pointer;
    user-select: none; position: sticky; top: 0; background: var(--panel);
  }
  th.sort-asc::after { content: " ↑"; color: var(--accent); }
  th.sort-desc::after { content: " ↓"; color: var(--accent); }
  tr:hover td { background: var(--panel-2); }
  td.num, th.num { text-align: left; font-variant-numeric: tabular-nums; }
  td a, .card a {
    color: var(--accent); text-decoration: none; cursor: pointer;
  }
  td a:hover, .card a:hover { text-decoration: underline; }

  /* Confidence-tiered reference annotation block. */
  .ann-card { border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
  .ann-head { padding: 8px 12px; background: var(--panel-2); border-bottom: 1px solid var(--border); }
  .ann-head .ann-title { font-size: 15px; font-weight: 600; }
  .ann-head .ann-sub { color: var(--muted); font-size: 11px; margin-top: 2px; }
  .ann-tier { padding: 10px 12px 12px; border-bottom: 1px solid var(--border); }
  .ann-tier:last-child { border-bottom: none; }
  .ann-tier-label {
    display: inline-flex; align-items: center; gap: 6px;
    font-size: 10px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase;
    margin-bottom: 6px;
  }
  .ann-tier-label::before {
    content: ''; width: 9px; height: 9px; border-radius: 50%; display: inline-block;
  }
  .ann-dot-note { font-weight: 400; text-transform: none; letter-spacing: 0; color: var(--muted); font-size: 10px; }
  .ann-verified  .ann-tier-label { color: var(--good); }
  .ann-verified  .ann-tier-label::before { background: var(--good); }
  .ann-context   .ann-tier-label { color: var(--accent); }
  .ann-context   .ann-tier-label::before { background: var(--accent); }
  .ann-unknown   .ann-tier-label { color: var(--accent-2); }
  .ann-unknown   .ann-tier-label::before { background: var(--accent-2); }
  .ann-unknown   { background: rgba(245,185,92,0.05); }
  .ann-body { font-size: 13px; line-height: 1.5; }
  .ann-body code { background: var(--panel-2); padding: 1px 4px; border-radius: 3px; font-size: 12px; }
  details.ann-tier { padding: 0; }
  .ann-summary { padding: 10px 12px; cursor: pointer; user-select: none; }
  details.ann-tier[open] > .ann-summary { padding-bottom: 4px; }
  .ann-summary::marker { color: var(--accent-2); }
  details.ann-tier > ul { margin: 0; padding: 0 12px 12px 32px; }
  .ann-unknown li { margin: 3px 0; font-size: 12.5px; }
  .ann-verified .ann-body { color: var(--muted); }

  .filter {
    width: 100%; padding: 8px 12px; margin-bottom: 12px;
    background: var(--panel-2); border: 1px solid var(--border);
    color: var(--text); font: inherit; border-radius: 4px;
  }
  .filter:focus { outline: 1px solid var(--accent); }

  .badge {
    display: inline-block; padding: 1px 7px;
    border-radius: 10px; font-size: 10px; margin-right: 4px;
    background: var(--panel-2); color: var(--muted);
    border: 1px solid var(--border);
  }
  .badge.legacy { color: var(--warn); border-color: var(--warn); }
  .badge.mode   { color: var(--accent-2); border-color: var(--accent-2); }
  .badge.helper { color: var(--good); border-color: var(--good); }
  .badge.dyn    { color: var(--warn); border-color: var(--warn); }

  .scroll {
    max-height: 60vh; overflow: auto;
    border: 1px solid var(--border); border-radius: 4px;
    background: var(--panel);
  }
  .helptext {
    color: var(--muted); white-space: pre-wrap;
    background: var(--panel-2); padding: 8px; border-radius: 4px;
    font-size: 11px;
  }
  details { margin: 8px 0; }
  details summary {
    cursor: pointer; color: var(--muted);
    padding: 4px 0;
  }
  .small { color: var(--muted); font-size: 11px; }
  .field-tree {
    font-size: 12px; font-family: "SF Mono", Menlo, monospace;
  }
  .field-tree .field-row {
    padding: 3px 0; border-bottom: 1px dotted var(--border);
  }
  .field-tree .field-value {
    color: var(--accent-2); margin-left: 12px;
  }
  .field-tree .field-mode {
    color: var(--good); margin-left: 12px; font-size: 11px;
  }
  .breadcrumb {
    margin-bottom: 12px; color: var(--muted);
  }
  .breadcrumb a { cursor: pointer; }
  /* D3 graph styling */
  svg.ego { width: 100%; height: 480px; background: var(--panel); border-radius: 4px; }
  svg.ego .node circle { stroke: var(--border); stroke-width: 1.5px; }
  svg.ego .node text { fill: var(--text); font: 10px sans-serif; pointer-events: none; }
  svg.ego .link { stroke: var(--muted); stroke-opacity: 0.5; }
  svg.ego .center-node circle { fill: var(--accent); }
  svg.ego .caller-node circle { fill: var(--accent-2); }
  svg.ego .callee-node circle { fill: var(--good); }

  /* Source viewer */
  .source-wrap {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 4px;
    overflow: hidden;
  }
  .source-header {
    display: flex; justify-content: space-between; align-items: center;
    padding: 8px 14px; border-bottom: 1px solid var(--border);
    background: var(--panel-2);
  }
  .source-header .small { color: var(--muted); }
  .source-header button {
    background: var(--panel); border: 1px solid var(--border);
    color: var(--text); font: inherit; padding: 3px 10px;
    border-radius: 3px; cursor: pointer; margin-left: 6px;
  }
  .source-header button:hover { border-color: var(--accent); color: var(--accent); }
  .source-body {
    max-height: 70vh; overflow: auto;
    display: grid; grid-template-columns: auto 1fr;
    font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 12px;
    background: var(--panel);
  }
  .source-gutter {
    text-align: right; color: var(--muted); padding: 8px 0; padding-right: 8px;
    user-select: none; background: var(--panel-2);
    border-right: 1px solid var(--border);
    white-space: pre;
  }
  .source-gutter .ln {
    display: block; padding: 0 4px 0 12px;
    cursor: pointer; text-decoration: none; color: var(--muted);
  }
  .source-gutter .ln:hover { color: var(--accent); background: var(--panel); }
  .source-gutter .ln.target { color: var(--accent-2); font-weight: 600; }
  .source-code {
    padding: 8px 12px;
    white-space: pre; tab-size: 4;
    color: var(--text);
  }
  .source-code .ln-row { display: block; padding: 0 4px; }
  .source-code .ln-row.target { background: rgba(245, 185, 92, 0.08); }
  /* MATLAB syntax tokens */
  .hl-kw  { color: #c878d8; }
  .hl-str { color: #a5d96a; }
  .hl-cmt { color: #6a7280; font-style: italic; }
  .hl-num { color: #e0a36a; }
  .hl-fn  { color: #6aa9ff; }

  /* Search tab */
  .search-result {
    padding: 6px 12px;
    border-bottom: 1px dotted var(--border);
    cursor: pointer;
  }
  .search-result:hover { background: var(--panel-2); }
  .search-result .meta {
    font-size: 11px; color: var(--muted);
  }
  .search-result .snippet {
    font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 11px;
    color: var(--text); white-space: pre; overflow: hidden;
    text-overflow: ellipsis;
  }
  .search-result mark {
    background: var(--accent-2); color: #000; padding: 0 1px;
  }
  .search-progress {
    color: var(--muted); padding: 8px 12px; font-size: 11px;
  }

  /* API-surface bar chart */
  .api-row {
    display: grid;
    grid-template-columns: 200px 110px 1fr 80px;
    gap: 8px;
    align-items: center;
    padding: 4px 8px;
    border-bottom: 1px dotted var(--border);
    cursor: pointer;
  }
  .api-row:hover { background: var(--panel-2); }
  .api-name { font-weight: 600; color: var(--accent); font-family: "SF Mono", Menlo, monospace; }
  .api-root { font-size: 10px; color: var(--muted); text-align: right; padding-right: 6px;
              white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .api-bar-track { background: var(--panel-2); height: 14px; border-radius: 2px; overflow: hidden;
                   display: flex; }
  .api-bar-paren { background: var(--accent); height: 100%; }
  .api-bar-parenless { background: var(--accent-2); height: 100%; }
  .api-stats { font-size: 11px; color: var(--muted); text-align: right;
               font-variant-numeric: tabular-nums; }
  .api-legend-swatch {
    display: inline-block; width: 12px; height: 10px; vertical-align: middle;
    margin: 0 4px 0 12px;
  }
</style>
</head>
<body>
<header>
  <h1>MML AUDIT BROWSER</h1>
  <span class="meta">Static snapshot — see <code>audit/data/*</code> for raw JSON. Click a row to drill down.</span>
</header>
<nav id="nav">
  <button data-tab="overview" class="active">Overview</button>
  <button data-tab="search">Search source</button>
  <button data-tab="workflows">Workflows</button>
  <button data-tab="files">Files</button>
  <button data-tab="families">AO families</button>
  <button data-tab="ad">AD paths</button>
  <button data-tab="modes">Operational modes</button>
  <button data-tab="subsystems">Subsystems</button>
  <button data-tab="groups">MemberOf groups</button>
  <button data-tab="api">API surface</button>
</nav>
<main id="app"></main>

__D3__
__PAKO__
<script id="payload" type="application/json">__PAYLOAD__</script>
<script id="source-bundle" type="text/plain">__SOURCE_B64__</script>
<script>
'use strict';

// -- Payload --------------------------------------------------------------
const DATA = JSON.parse(document.getElementById('payload').textContent);
const NODES_BY_PATH = Object.fromEntries(DATA.nodes.map(n => [n.id, n]));
// basename → list of paths, for resolving "filename.m:42" references back
// to a full path. When multiple files share a basename we prefer non-archive
// and mml/ over machine-specific ones; same precedence the MML path-config
// would resolve at runtime.
const BY_BASENAME = {};
DATA.nodes.forEach(n => {
  const base = n.id.split('/').pop();
  (BY_BASENAME[base] = BY_BASENAME[base] || []).push(n.id);
});
function resolveBasename(base) {
  const candidates = BY_BASENAME[base] || [];
  if (!candidates.length) return null;
  if (candidates.length === 1) return candidates[0];
  const live = candidates.filter(p => NODES_BY_PATH[p] && !NODES_BY_PATH[p].archive);
  if (live.length === 1) return live[0];
  const pool = live.length ? live : candidates;
  // Prefer mml/ core, then ALS Common.
  const sorted = pool.slice().sort((a, b) => {
    const rank = p => p.startsWith('mml/') ? 0 :
                     p.startsWith('machine/ALS/Common/') ? 1 :
                     p.startsWith('machine/ALS/StorageRing/') ? 2 : 3;
    return rank(a) - rank(b);
  });
  return sorted[0];
}

// -- Source bundle (lazy-decompressed on first access) -------------------
// The bundle is a base64-encoded gzipped JSON dict {path: source-text}.
// Decompression happens once, on the first file-detail view that needs it,
// after which SOURCES is a plain dict and lookups are instant.
let SOURCES = null;
function ensureSources() {
  if (SOURCES) return SOURCES;
  if (!window.pako) {
    SOURCES = {};
    return SOURCES;
  }
  try {
    const b64 = document.getElementById('source-bundle').textContent.trim();
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    const json = pako.ungzip(bytes, { to: 'string' });
    SOURCES = JSON.parse(json);
  } catch (e) {
    console.error('failed to decompress source bundle:', e);
    SOURCES = {};
  }
  return SOURCES;
}
function getSource(path) {
  const dict = ensureSources();
  return dict[path] != null ? dict[path] : null;
}

// -- MATLAB syntax highlighter (small, pragmatic) -------------------------
// Tokenizes line-by-line into spans for keyword / string / comment / number.
// Reuses the same string-vs-transpose heuristic as the audit extractors:
// `'` is a string opener only if the previous non-space char is NOT alnum,
// ')', ']', '}', '.' or '_'. Doubled `''` inside strings is a literal quote.
const MATLAB_KEYWORDS = new Set([
  'if','else','elseif','end','for','while','switch','case','otherwise',
  'break','continue','return','function','global','persistent',
  'try','catch','classdef','properties','methods','events','spmd','parfor',
  'true','false','varargin','varargout','nargin','nargout',
]);
function escapeHtml(s) {
  return s.replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
}
function highlightMatlabLine(line) {
  let html = '';
  let i = 0;
  const n = line.length;
  let inString = false;
  let stringStart = -1;
  let codeStart = 0;

  function flushCode(end) {
    if (end <= codeStart) return;
    const segment = line.slice(codeStart, end);
    // Walk the segment, identify keywords/numbers; leave operators/punct as text.
    let out = '';
    let j = 0;
    while (j < segment.length) {
      const c = segment[j];
      if (/[A-Za-z_]/.test(c)) {
        let k = j;
        while (k < segment.length && /[A-Za-z0-9_]/.test(segment[k])) k++;
        const word = segment.slice(j, k);
        if (MATLAB_KEYWORDS.has(word)) {
          out += '<span class="hl-kw">' + escapeHtml(word) + '</span>';
        } else if (segment[k] === '(' && j === 0) {
          // identifier immediately followed by `(` at start = looks like a call
          out += '<span class="hl-fn">' + escapeHtml(word) + '</span>';
        } else {
          out += escapeHtml(word);
        }
        j = k;
      } else if (/[0-9]/.test(c)) {
        let k = j;
        while (k < segment.length && /[0-9.eE+\-]/.test(segment[k])) {
          // Stop at signs that aren't part of an exponent.
          if ((segment[k] === '+' || segment[k] === '-')
              && k > 0 && segment[k-1] !== 'e' && segment[k-1] !== 'E') break;
          k++;
        }
        const word = segment.slice(j, k);
        if (/^\d+(\.\d+)?([eE][+-]?\d+)?$/.test(word)) {
          out += '<span class="hl-num">' + escapeHtml(word) + '</span>';
        } else {
          out += escapeHtml(word);
        }
        j = k;
      } else {
        out += escapeHtml(c);
        j++;
      }
    }
    html += out;
  }

  while (i < n) {
    const c = line[i];
    if (c === "'") {
      if (inString) {
        // Doubled '' = literal apostrophe inside string.
        if (i + 1 < n && line[i + 1] === "'") { i += 2; continue; }
        html += '<span class="hl-str">' + escapeHtml(line.slice(stringStart, i + 1)) + '</span>';
        inString = false;
        codeStart = i + 1;
        i++;
      } else {
        // Decide: string open or transpose?
        let j = i - 1;
        while (j >= 0 && line[j] === ' ') j--;
        const prev = j >= 0 ? line[j] : '';
        if (prev && /[A-Za-z0-9_)\]}.]/.test(prev)) {
          // transpose; leave in code
          i++;
        } else {
          flushCode(i);
          inString = true;
          stringStart = i;
          i++;
        }
      }
    } else if (c === '%' && !inString) {
      flushCode(i);
      html += '<span class="hl-cmt">' + escapeHtml(line.slice(i)) + '</span>';
      codeStart = n;
      i = n;
    } else {
      i++;
    }
  }
  if (inString) {
    // Unterminated string — render the open-to-end as a string.
    html += '<span class="hl-str">' + escapeHtml(line.slice(stringStart)) + '</span>';
  } else {
    flushCode(n);
  }
  return html;
}

// -- Navigation helpers ---------------------------------------------------
// `pendingScrollLine` is set by the router when a hash like `#/file/.../L42`
// is parsed, and consumed by the source viewer on render. Code navigates
// to a specific line via `navigateTo('file', path, 'L' + line)`.
let pendingScrollLine = null;
// Turn a "filename.m:42" string into a clickable element that gotos there.
function fileLineLink(displayText, fullPath, line) {
  return el('a', {
    style: 'color:var(--accent); cursor:pointer; text-decoration:none;',
    title: fullPath + ':' + line,
    onClick: () => navigateTo('file', fullPath, line ? 'L' + line : null),
  }, displayText);
}
// Parse a "filename.m:42" string and return either a clickable element or
// a plain text node when the file isn't resolvable.
function refToEl(refText) {
  const m = String(refText).match(/^(.+\.m):(\d+)$/);
  if (!m) return document.createTextNode(refText);
  const fullPath = resolveBasename(m[1]);
  if (!fullPath) return document.createTextNode(refText);
  return fileLineLink(refText, fullPath, parseInt(m[2], 10));
}
// Render an array of `file:line` refs joined by commas as DOM nodes.
function refsList(refsArr, max) {
  max = max || refsArr.length;
  const out = [];
  refsArr.slice(0, max).forEach((r, i) => {
    if (i > 0) out.push(', ');
    out.push(refToEl(r));
  });
  if (refsArr.length > max) out.push(' …');
  return out;
}

// -- Hash-based router ----------------------------------------------------
// Each navigation flips window.location.hash, the hashchange listener
// dispatches to the right render function. The browser's native back/
// forward buttons just work (they fire hashchange), and URLs are now
// shareable: copy any hash into another browser session and you land on
// the same detail page. Initial load honors a deep link if present.
const app = document.getElementById('app');
const tabs = document.querySelectorAll('#nav button');
let currentTab = 'overview';

// Which top tab to mark active for each view.
const TAB_FOR_VIEW = {
  overview: 'overview', search: 'search',
  workflows: 'workflows', workflow: 'workflows',
  files: 'files', file: 'files',
  families: 'families', family: 'families',
  ad: 'ad', adpath: 'ad',
  modes: 'modes', mode: 'modes',
  subsystems: 'subsystems', subsystem: 'subsystems',
  groups: 'groups', group: 'groups',
  api: 'api', verb: 'api',
};

function navigateTo(view /* , ...args */) {
  const args = Array.prototype.slice.call(arguments, 1)
    .filter(a => a != null && a !== '');
  const encoded = args.map(a => encodeURIComponent(String(a))).join('/');
  const newHash = '#/' + view + (encoded ? '/' + encoded : '');
  if (window.location.hash === newHash) {
    route();   // same hash, force re-render anyway
  } else {
    window.location.hash = newHash;
    // hashchange listener picks it up
  }
}

function route() {
  const raw = (window.location.hash || '#/overview').replace(/^#\/?/, '');
  const parts = raw.split('/').map(decodeURIComponent);
  const view = parts[0] || 'overview';
  const args = parts.slice(1);
  const tab = TAB_FOR_VIEW[view];
  if (tab) switchActive(tab);
  switch (view) {
    case 'overview':   renderOverview(); break;
    case 'search':     renderSearchTab(); break;
    case 'workflows':  renderWorkflowsList(); break;
    case 'workflow':   showWorkflowDetail(args[0]); break;
    case 'files':      renderFilesList(); break;
    case 'file': {
      const lineSpec = args[1] || '';
      const m = lineSpec.match(/^L(\d+)$/);
      pendingScrollLine = m ? parseInt(m[1], 10) : null;
      showFileDetail(args[0]);
      break;
    }
    case 'families':   renderFamiliesList(); break;
    case 'family':     showFamilyDetail(args[0]); break;
    case 'ad':         renderADList(); break;
    case 'adpath':     showADDetail(args[0]); break;
    case 'modes':      renderModesList(); break;
    case 'mode':       showModeDetail(args[0]); break;
    case 'subsystems': renderSubsystemsList(); break;
    case 'subsystem':  showSubsystemDetail(args[0]); break;
    case 'groups':     renderGroupsList(); break;
    case 'group':      showGroupDetail(args[0]); break;
    case 'api':        renderAPISurface(); break;
    case 'verb':       showVerbDetail(args[0]); break;
    default:           renderOverview();
  }
}

window.addEventListener('hashchange', () => route());
tabs.forEach(b => b.addEventListener('click', () => navigateTo(b.dataset.tab)));

// Keep switchTab as a thin wrapper for legacy callers (some internal helpers
// still call it). Routes through navigateTo so the hash stays in sync.
function switchTab(name) { navigateTo(name); }

// -- Helpers --------------------------------------------------------------
function el(tag, attrs, ...children) {
  const e = document.createElement(tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') e.className = v;
    else if (k === 'onClick') e.addEventListener('click', v);
    else if (k === 'html') e.innerHTML = v;
    else if (k.startsWith('data-')) e.setAttribute(k, v);
    else e[k] = v;
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    e.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return e;
}
function fmt(n) {
  return n == null ? '' : n.toLocaleString();
}
function link(text, fn) {
  return el('a', { onClick: (e) => { e.preventDefault(); fn(); } }, text);
}
// Confidence-tiered reference block. `verified` is live data the caller passes
// in (children); `context` (string) + `unknowns` (string[]) are hand-authored.
function annotationCard({ title, subtitle, verified, context, unknowns }) {
  const tiers = [];
  if (verified && verified.length) {
    tiers.push(el('div', { class: 'ann-tier ann-verified' },
      el('div', { class: 'ann-tier-label' }, 'Verified',
        el('span', { class: 'ann-dot-note' }, 'from static analysis of MML source')),
      el('div', { class: 'ann-body' }, verified)));
  }
  if (context) {
    tiers.push(el('div', { class: 'ann-tier ann-context' },
      el('div', { class: 'ann-tier-label' }, 'Domain context',
        el('span', { class: 'ann-dot-note' }, 'general accelerator knowledge — not ALS-specific config')),
      el('div', { class: 'ann-body' }, context)));
  }
  if (unknowns && unknowns.length) {
    tiers.push(el('details', { class: 'ann-tier ann-unknown' },
      el('summary', { class: 'ann-summary' },
        el('span', { class: 'ann-tier-label' }, 'Needs operator input',
          el('span', { class: 'ann-dot-note' },
            unknowns.length + ' open question(s) — AI conjecture, click to expand'))),
      el('ul', {}, unknowns.map(u => el('li', {}, u)))));
  }
  if (!tiers.length) return null;
  return el('div', { class: 'ann-card' },
    el('div', { class: 'ann-head' },
      el('div', { class: 'ann-title' }, title),
      subtitle ? el('div', { class: 'ann-sub' }, subtitle) : null),
    tiers);
}
function fileBadges(n) {
  const b = [];
  if (n.archive) b.push(el('span', {class: 'badge legacy'}, 'archive'));
  if (n.has_dynamic_dispatch) b.push(el('span', {class: 'badge dyn'}, 'dynamic-dispatch'));
  if (n.kind !== 'function') b.push(el('span', {class: 'badge'}, n.kind));
  return b;
}

// -- Sortable / filterable table -------------------------------------------
function sortableTable(rows, columns, opts = {}) {
  // columns: [{key, label, value(row), render?(row), num?}]
  // opts: {filterPlaceholder, filterFn(row, term), initialSort, onClick(row)}
  let sortKey = opts.initialSort?.key ?? columns[0].key;
  let sortDir = opts.initialSort?.dir ?? 'desc';
  let filterTerm = '';

  const wrapper = el('div');
  const filter = el('input', {
    class: 'filter',
    placeholder: opts.filterPlaceholder ?? 'filter…',
    type: 'search',
    oninput: e => { filterTerm = e.target.value.toLowerCase(); rerender(); }
  });
  wrapper.append(filter);
  const tableWrap = el('div', { class: 'scroll' });
  wrapper.append(tableWrap);

  function rerender() {
    const filtered = filterTerm
      ? rows.filter(r => opts.filterFn ? opts.filterFn(r, filterTerm)
                                       : JSON.stringify(r).toLowerCase().includes(filterTerm))
      : rows;
    const sortedCol = columns.find(c => c.key === sortKey);
    const sorted = filtered.slice().sort((a, b) => {
      const va = sortedCol.value(a), vb = sortedCol.value(b);
      if (va === vb) return 0;
      const cmp = (va < vb) ? -1 : 1;
      return sortDir === 'asc' ? cmp : -cmp;
    });
    const thead = el('thead', {}, el('tr', {}, columns.map(c => {
      const cls = c.key === sortKey ? `sort-${sortDir}` : '';
      return el('th', { class: cls, onClick: () => {
        if (sortKey === c.key) sortDir = (sortDir === 'asc' ? 'desc' : 'asc');
        else { sortKey = c.key; sortDir = c.num ? 'desc' : 'asc'; }
        rerender();
      } }, c.label);
    })));
    const tbody = el('tbody', {}, sorted.slice(0, 1500).map(r => {
      const tr = el('tr', {}, columns.map(c => {
        const v = c.render ? c.render(r) : c.value(r);
        return el('td', { class: c.num ? 'num' : '' },
          typeof v === 'string' || typeof v === 'number' ? v : v);
      }));
      if (opts.onClick) tr.addEventListener('click', () => opts.onClick(r));
      tr.style.cursor = opts.onClick ? 'pointer' : '';
      return tr;
    }));
    const tableEl = el('table', {}, thead, tbody);
    tableWrap.innerHTML = '';
    tableWrap.append(tableEl);
    if (sorted.length > 1500) {
      tableWrap.append(el('div', { class: 'small', style: 'padding: 8px; color: var(--muted);' },
        `(${sorted.length - 1500} more rows filtered out — refine your search to see them)`));
    }
  }
  rerender();
  return wrapper;
}

// -- Overview tab ---------------------------------------------------------
function renderOverview() {
  app.innerHTML = '';
  const s = DATA.cg_summary;
  const stats = el('div', { class: 'grid cols-3' },
    statCard('Files indexed', DATA.nodes.length, 'across mml/ + ALS sub-machines'),
    statCard('AO families', Object.keys(DATA.families).length,
             'declared in alsinit + setoperationalmode + helpers'),
    statCard('AD machine-config paths', Object.keys(DATA.ad).length,
             'Energy, MCF, Circumference, OperationalMode, …'),
    statCard('Operational modes', Object.keys(DATA.modes).length,
             'TopOff, Two-Bunch, Low Emittance, …'),
    statCard('Resolved callsites', fmt(s.resolved_callsites),
             `of ${fmt(s.total_callsites)} total — rest are MATLAB built-ins / toolbox`),
    statCard('Files with zero live callers', fmt(s.files_with_zero_live_callers),
             'paren-less calls and entry-point scripts inflate this — not all dead')
  );
  app.append(stats);

  app.append(el('div', { class: 'grid cols-2', style: 'margin-top:16px' },
    topListCard('Top callees (in-tree)', s.top_callees_by_usage.slice(0, 15),
                r => `${fmt(r.calls)} calls — ${r.name}`,
                r => { const path = candidatePath(r.name); if (path) navigateTo('file', path); }),
    topListCard('Top callers (out-degree)', s.top_callers_by_fanout.slice(0, 15),
                r => `${fmt(r.out)} out → ${shortPath(r.path)}`,
                r => navigateTo('file', r.path)),
  ));

  app.append(el('div', { class: 'grid cols-2', style: 'margin-top:16px' },
    topListCard('Most-called files (incoming edges)', s.top_callees_by_in_degree.slice(0, 15),
                r => `${fmt(r.in_total)} (${fmt(r.in_live_only)} live) — ${shortPath(r.path)}`,
                r => navigateTo('file', r.path)),
    shadowedCard(s.shadowed_callable_samples)
  ));
}

function candidatePath(name) {
  // Pick the highest-precedence path for a callable name from the nodes list.
  const matches = DATA.nodes.filter(n => n.name === name && !n.archive);
  if (!matches.length) return null;
  const orderRank = ['machine/ALS/StorageRing','machine/ALS/Common','machine/ALS/BTS',
                     'machine/ALS/GTB','machine/ALS/Booster','mml'];
  matches.sort((a,b) => orderRank.indexOf(a.root) - orderRank.indexOf(b.root));
  return matches[0].id;
}

function statCard(label, value, sub) {
  return el('div', { class: 'card' },
    el('h2', {}, label),
    el('div', { class: 'stat' }, fmt(value)),
    el('div', { class: 'small stat-label' }, sub)
  );
}
function topListCard(title, items, fmtRow, onClick) {
  return el('div', { class: 'card' },
    el('h2', {}, title),
    el('div', {},
      items.map(r => el('div', { style: 'padding:3px 0; border-bottom:1px dotted var(--border)' },
        link(fmtRow(r), () => onClick(r))))
    )
  );
}
function shadowedCard(samples) {
  return el('div', { class: 'card' },
    el('h2', {}, 'Shadowed callable definitions (sample)'),
    el('div', { class: 'small' }, 'Callable names defined in multiple in-scope files. ALS-path-first wins at runtime; ' +
                                   'click a file to see its detail.'),
    el('div', { style: 'margin-top:8px' },
      Object.entries(samples).slice(0, 8).map(([name, paths]) =>
        el('div', { style: 'margin-bottom:6px' },
          el('div', {}, el('strong', {}, name)),
          paths.map(p => el('div', { style: 'padding-left:16px' },
            link(shortPath(p), () => navigateTo('file', p))))
        ))
    )
  );
}

// -- Files tab ------------------------------------------------------------
function renderFilesList() {
  app.innerHTML = '';
  const rows = DATA.nodes;
  const table = sortableTable(rows, [
    { key: 'name',  label: 'name',  value: r => r.name,
      render: r => link(r.name, () => navigateTo('file', r.id)) },
    { key: 'root',  label: 'root',  value: r => r.root },
    { key: 'kind',  label: 'kind',  value: r => r.kind },
    { key: 'lines', label: 'lines', value: r => r.line_count, num: true },
    { key: 'in_live_only', label: 'in (live)', value: r => r.in_live_only, num: true },
    { key: 'in_total', label: 'in (all)', value: r => r.in_total, num: true },
    { key: 'out', label: 'out', value: r => r.out, num: true },
    { key: 'flags', label: 'flags', value: r => (r.archive?1:0)+(r.has_dynamic_dispatch?1:0),
      render: r => el('span', {}, fileBadges(r)) },
  ], {
    filterPlaceholder: 'filter files by name or path…',
    filterFn: (r, t) => r.name.toLowerCase().includes(t) || r.id.toLowerCase().includes(t),
    initialSort: { key: 'in_live_only', dir: 'desc' },
    onClick: r => navigateTo('file', r.id),
  });
  app.append(table);
}

function showFileDetail(path) {
  const node = NODES_BY_PATH[path];
  const fi = DATA.file_index[path] || {};
  app.innerHTML = '';
  app.append(el('div', { class: 'breadcrumb' },
    link('← back to files', () => navigateTo('files'))));

  // Header card
  app.append(el('div', { class: 'card' },
    el('h2', {}, 'FILE'),
    el('div', { class: 'stat' }, node.name),
    el('div', { class: 'small' }, path),
    el('div', { style: 'margin-top:8px' }, fileBadges(node)),
    el('div', { style: 'margin-top:8px' }, fi.summary || el('em', {}, '(no help text)')),
    el('div', { class: 'stat-row', style: 'margin-top:10px' },
      stat('lines', node.line_count),
      stat('callers (live)', node.in_live_only),
      stat('callers (all)', node.in_total),
      stat('out edges', node.out),
      stat('subfunctions', fi.subfunction_count),
      stat('mtime', (fi.mtime || '').slice(0,10)),
    ),
    fi.help ? el('details', {},
      el('summary', {}, 'Help text'),
      el('div', { class: 'helptext' }, fi.help + (fi.help.length >= 600 ? '…' : ''))
    ) : null,
    fi.function ? el('div', { class: 'small', style: 'margin-top:6px' },
      'Signature: ' + (fi.function.outputs.length
        ? '[' + fi.function.outputs.join(', ') + '] = ' : '')
      + fi.function.name + '(' + fi.function.args.join(', ') + ')') : null,
  ));

  // Callers + callees
  const cIn = DATA.callers[path] || [];
  const cOut = DATA.callees[path] || [];
  app.append(el('div', { class: 'grid cols-2', style: 'margin-top:14px' },
    callListCard('Called by (' + cIn.length + ' files)', cIn, 'source'),
    callListCard('Calls into (' + cOut.length + ' files)', cOut, 'target'),
  ));

  // Cross-refs: which AO families & AD paths this file touches
  const fams = DATA.file_to_families[path] || [];
  const ads  = DATA.file_to_ads[path] || [];
  if (fams.length || ads.length) {
    app.append(el('div', { class: 'grid cols-2', style: 'margin-top:14px' },
      el('div', { class: 'card' },
        el('h2', {}, 'AO families touched (' + fams.length + ')'),
        fams.length ? el('div', { class: 'scroll', style:'max-height:260px' },
          el('table', {}, el('tbody', {},
            fams.map(f => el('tr', {},
              el('td', { class: 'num' }, fmt(f.total_refs)),
              el('td', {}, link(f.family, () => navigateTo('family', f.family))),
              el('td', { class: 'small' },
                f.string_refs ? `${f.string_refs} str` : '',
                f.string_refs && f.struct_refs ? ' / ' : '',
                f.struct_refs ? `${f.struct_refs} struct` : ''),
            ))
          ))
        ) : el('div', { class: 'small' }, '(none detected)')
      ),
      el('div', { class: 'card' },
        el('h2', {}, 'AD paths touched (' + ads.length + ')'),
        ads.length ? el('div', { class: 'scroll', style:'max-height:260px' },
          el('table', {}, el('tbody', {},
            ads.map(a => el('tr', {},
              el('td', { class: 'num' }, fmt(a.refs)),
              el('td', {}, link('AD.' + a.ad_path,
                () => DATA.ad[a.ad_path] ? navigateTo('adpath', a.ad_path) : null)),
            ))
          ))
        ) : el('div', { class: 'small' }, '(none detected)')
      ),
    ));
  }

  // Ego network viz
  app.append(el('div', { class: 'card', style: 'margin-top:14px' },
    el('h2', {}, '1-hop ego network'),
    el('div', { class: 'small' }, 'orange = callers, green = callees, blue = this file. ' +
                                   'Hover for filename; click a node to navigate.'),
    el('div', { id: 'ego' })
  ));
  setTimeout(() => renderEgoNetwork(path), 0);

  // Source viewer
  app.append(el('div', { class: 'card', style: 'margin-top:14px; padding:0' },
    el('h2', { style: 'padding:14px 14px 0 14px; margin:0' }, 'Source'),
    el('div', { id: 'source-host', style: 'padding:10px 14px 14px 14px' })
  ));
  setTimeout(() => renderSourceViewer(path), 0);
}

function renderSourceViewer(path) {
  const host = document.getElementById('source-host');
  if (!host) return;
  host.innerHTML = '';
  const node = NODES_BY_PATH[path] || {};
  const archive = node.archive;
  if (archive) {
    host.append(el('div', { class: 'small' },
      'This file is in an archive folder, so its source is not embedded in the browser ' +
      '(keeps the file size down — archive content is by definition not on the live MML path). ',
      el('br', {}),
      'Open from disk: ', el('code', {}, path),
      ' ',
      el('button', {
        style: 'margin-left:8px; background:var(--panel-2); border:1px solid var(--border); ' +
               'color:var(--text); font:inherit; padding:3px 10px; border-radius:3px; cursor:pointer;',
        onClick: () => {
          navigator.clipboard.writeText(path).then(
            () => {},
            () => alert('clipboard not available — path: ' + path));
        }
      }, 'copy path'),
    ));
    return;
  }
  const src = getSource(path);
  if (src == null) {
    host.append(el('div', { class: 'small' },
      'Source not available for this file. (Was the build run before this file was added? ' +
      'Re-run `python3 audit/extractors/06_build_browser.py` to refresh.)'));
    return;
  }
  const lines = src.split('\n');
  const TRUNC_AT = 1000;
  const truncated = lines.length > TRUNC_AT;
  let displayLines = truncated ? lines.slice(0, TRUNC_AT) : lines;
  let expanded = false;

  function render() {
    host.innerHTML = '';
    const header = el('div', { class: 'source-header' });
    header.append(el('div', { class: 'small' },
      lines.length.toLocaleString() + ' lines · ' +
      (src.length / 1024).toFixed(1) + ' KB' +
      (truncated && !expanded ? ' · showing first ' + TRUNC_AT : '')));
    const buttons = el('div', {});
    if (truncated) {
      buttons.append(el('button', {
        onClick: () => {
          expanded = !expanded;
          displayLines = expanded ? lines : lines.slice(0, TRUNC_AT);
          render();
        }
      }, expanded ? 'collapse' : 'show all ' + lines.length.toLocaleString()));
    }
    buttons.append(el('button', {
      onClick: () => {
        navigator.clipboard.writeText(src).then(
          () => {},
          () => alert('clipboard not available'));
      }
    }, 'copy source'));
    header.append(buttons);

    const body = el('div', { class: 'source-body' });
    const gutter = el('div', { class: 'source-gutter' });
    const code = el('div', { class: 'source-code' });
    body.append(gutter, code);

    // Build gutter and code as line-by-line spans so line numbers can be
    // navigated and the target line can be highlighted/scrolled to.
    const target = pendingScrollLine;
    let targetEl = null;
    const gutterHtml = displayLines.map((_, i) => {
      const ln = i + 1;
      const cls = (ln === target) ? ' target' : '';
      return `<a class="ln${cls}" id="L${ln}" data-ln="${ln}">${ln}</a>`;
    }).join('');
    gutter.innerHTML = gutterHtml;
    // Wire clicks on line numbers: copy `file:line` reference for sharing.
    gutter.querySelectorAll('.ln').forEach(a => {
      a.addEventListener('click', e => {
        e.preventDefault();
        const ln = a.dataset.ln;
        navigator.clipboard.writeText(path + ':' + ln).catch(() => {});
        // Visually highlight the clicked line as the new target.
        code.querySelectorAll('.ln-row.target').forEach(r => r.classList.remove('target'));
        gutter.querySelectorAll('.ln.target').forEach(r => r.classList.remove('target'));
        a.classList.add('target');
        const row = code.querySelector('#code-L' + ln);
        if (row) row.classList.add('target');
      });
    });

    const codeHtml = displayLines.map((rawLine, i) => {
      const ln = i + 1;
      const cls = (ln === target) ? ' target' : '';
      const highlighted = highlightMatlabLine(rawLine) || '&nbsp;';
      return `<span class="ln-row${cls}" id="code-L${ln}">${highlighted}</span>`;
    }).join('\n');
    code.innerHTML = codeHtml;

    const wrap = el('div', { class: 'source-wrap' });
    wrap.append(header, body);
    host.append(wrap);

    // If a jump-to-line was requested, scroll the body to bring it into view.
    if (target && target <= displayLines.length) {
      targetEl = code.querySelector('#code-L' + target);
      if (targetEl) {
        // Run after layout settles.
        requestAnimationFrame(() => {
          const bodyRect = body.getBoundingClientRect();
          const elRect = targetEl.getBoundingClientRect();
          body.scrollTop += (elRect.top - bodyRect.top) - bodyRect.height / 3;
        });
      }
      pendingScrollLine = null;   // consume — only fires once
    } else if (target && truncated && target > TRUNC_AT) {
      // Target is past the truncation window — auto-expand.
      expanded = true;
      displayLines = lines;
      render();
    }
  }
  render();
}

function stat(label, value) {
  return el('div', {},
    el('div', { class: 'stat' }, value == null ? '—' : fmt(value)),
    el('div', { class: 'stat-label' }, label));
}
function callListCard(title, edges, dir) {
  return el('div', { class: 'card' },
    el('h2', {}, title),
    el('div', { class: 'scroll', style: 'max-height: 320px' },
      el('table', {}, el('tbody', {},
        edges.slice().sort((a, b) => b.calls - a.calls).map(e =>
          el('tr', {},
            el('td', { class: 'num' }, fmt(e.calls)),
            el('td', { class: 'small', title: 'paren / paren-less' },
              (e.paren ? `${e.paren}` : '0') + ' / ' + (e.parenless ? `${e.parenless}` : '0')),
            el('td', {}, link(shortPath(e.path), () => navigateTo('file', e.path)))
          )
        )
      ))
    )
  );
}

function renderEgoNetwork(centerPath) {
  const container = d3.select('#ego');
  container.selectAll('*').remove();
  const callers = (DATA.callers[centerPath] || []).slice().sort((a,b)=>b.calls-a.calls).slice(0,15);
  const callees = (DATA.callees[centerPath] || []).slice().sort((a,b)=>b.calls-a.calls).slice(0,15);
  const nodes = [
    { id: centerPath, name: NODES_BY_PATH[centerPath]?.name || centerPath, role: 'center' },
    ...callers.map(c => ({id: c.path, name: NODES_BY_PATH[c.path]?.name || c.path, role: 'caller'})),
    ...callees.map(c => ({id: c.path, name: NODES_BY_PATH[c.path]?.name || c.path, role: 'callee'})),
  ];
  // dedupe in case a file is both caller and callee
  const seen = new Set(); const uniqNodes = nodes.filter(n => !seen.has(n.id) && seen.add(n.id));
  const links = [
    ...callers.map(c => ({source: c.path, target: centerPath, calls: c.calls})),
    ...callees.map(c => ({source: centerPath, target: c.path, calls: c.calls})),
  ];

  if (uniqNodes.length === 1) {
    container.append('div').attr('class','small').text('(no in-tree callers or callees found)');
    return;
  }

  const W = container.node().getBoundingClientRect().width || 800;
  const H = 480;
  const svg = container.append('svg').attr('class','ego').attr('viewBox', [0, 0, W, H]);

  const link = svg.append('g').attr('class','links').selectAll('line').data(links)
    .join('line').attr('class','link')
    .attr('stroke-width', d => Math.max(1, Math.log2(d.calls)));

  const node = svg.append('g').selectAll('g').data(uniqNodes).join('g')
    .attr('class', d => 'node ' + d.role + '-node')
    .style('cursor','pointer')
    .on('click', (_, d) => navigateTo('file', d.id))
    .call(d3.drag()
      .on('start', (e, d) => { if (!e.active) sim.alphaTarget(0.3).restart(); d.fx=d.x; d.fy=d.y; })
      .on('drag',  (e, d) => { d.fx=e.x; d.fy=e.y; })
      .on('end',   (e, d) => { if (!e.active) sim.alphaTarget(0); d.fx=null; d.fy=null; }));
  node.append('circle').attr('r', d => d.role === 'center' ? 9 : 5);
  node.append('title').text(d => d.id);
  node.append('text').attr('x', 8).attr('y', 4).text(d => d.name);

  const sim = d3.forceSimulation(uniqNodes)
    .force('link', d3.forceLink(links).id(d => d.id).distance(80))
    .force('charge', d3.forceManyBody().strength(-200))
    .force('center', d3.forceCenter(W/2, H/2))
    .force('collide', d3.forceCollide(25))
    .on('tick', () => {
      link.attr('x1', d => d.source.x).attr('y1', d => d.source.y)
          .attr('x2', d => d.target.x).attr('y2', d => d.target.y);
      node.attr('transform', d => `translate(${d.x},${d.y})`);
    });
}

// -- Families tab ----------------------------------------------------------
function renderFamiliesList() {
  app.innerHTML = '';
  const rows = Object.values(DATA.families).map(r => {
    const meta = (DATA.family_meta || {})[r.family] || {};
    return { ...r,
             device_count: meta.device_count,
             group_count: (meta.all_groups_touched || []).length };
  });
  const table = sortableTable(rows, [
    { key: 'family', label: 'family', value: r => r.family,
      render: r => link(r.family, () => navigateTo('family', r.family)) },
    { key: 'device_count', label: 'devices',
      value: r => r.device_count == null ? -1 : r.device_count, num: true,
      render: r => r.device_count == null ? '—' : r.device_count },
    { key: 'group_count', label: 'groups', value: r => r.group_count, num: true },
    { key: 'total_assignments', label: 'assignments', value: r => r.total_assignments, num: true },
    { key: 'distinct_fields', label: 'distinct fields', value: r => r.distinct_fields, num: true },
    { key: 'modes_touched', label: 'modes touched',
      value: r => r.modes_touched.length, num: true,
      render: r => r.modes_touched.map(m => el('span',{class:'badge mode'}, String(m))) },
    { key: 'via_helpers', label: 'via helpers',
      value: r => r.via_helpers.length,
      render: r => r.via_helpers.length ? r.via_helpers.map(h => el('span',{class:'badge helper'}, h.split('/').pop())) : '—' },
    { key: 'unresolved', label: 'unresolved sub',
      value: r => r.has_unresolved_subpath ? 1 : 0,
      render: r => r.has_unresolved_subpath ? el('span',{class:'badge dyn'},'yes') : '' },
  ], {
    filterPlaceholder: 'filter families…',
    filterFn: (r, t) => r.family.toLowerCase().includes(t),
    initialSort: { key: 'total_assignments', dir: 'desc' },
    onClick: r => navigateTo('family', r.family),
  });
  app.append(table);
}

function showFamilyDetail(name) {
  const f = DATA.families[name];
  const meta = (DATA.family_meta || {})[name] || {};
  app.innerHTML = '';
  app.append(el('div', { class: 'breadcrumb' },
    link('← back to families', () => navigateTo('families'))));
  app.append(el('div', { class: 'card' },
    el('h2', {}, 'AO FAMILY'),
    el('div', { class: 'stat' }, f.family),
    el('div', { class: 'stat-row', style: 'margin-top:10px' },
      stat('devices', meta.device_count == null ? '—' : meta.device_count),
      stat('total assignments', f.total_assignments),
      stat('distinct fields', f.distinct_fields),
      stat('modes touched', f.modes_touched.length),
      stat('MemberOf groups', (meta.all_groups_touched || []).length),
    ),
    (meta.family_level_groups && meta.family_level_groups.length) ? el('div', { style: 'margin-top:10px' },
      el('span', { class: 'small' }, 'Family-level MemberOf: '),
      meta.family_level_groups.map(g => el('span', {
        class: 'badge helper', style: 'cursor:pointer; margin:2px',
        onClick: () => navigateTo('group', g),
      }, g))
    ) : null,
    (meta.field_level_groups && Object.keys(meta.field_level_groups).length) ? el('details', { style: 'margin-top:6px' },
      el('summary', { class: 'small' },
        'Field-level MemberOf tags (' + Object.keys(meta.field_level_groups).length + ' sub-paths)'),
      el('div', { class: 'small' },
        Object.entries(meta.field_level_groups).map(([sp, gs]) =>
          el('div', { style: 'padding:2px 0' },
            el('code', {}, sp), ': ',
            gs.map(g => el('span', {
              class: 'badge helper', style: 'cursor:pointer; margin:1px',
              onClick: () => navigateTo('group', g),
            }, g))
          ))
      )
    ) : null,
    el('div', { class: 'small', style: 'margin-top:8px' },
      'Sources: ', f.sources.join(', '), '. ',
      f.via_helpers.length ? 'Helpers: ' + f.via_helpers.join(', ') : ''),
    f.has_unresolved_subpath ? el('div', { class: 'small', style:'margin-top:6px;color:var(--warn)' },
      '⚠ Some sub-struct fields use dynamic-field access (ao.(Family).(Field)) — ' +
      'shown as * in field paths. Names not statically resolvable.') : null,
  ));

  const fam_ann = ((DATA.annotations || {}).families || {})[name];
  if (fam_ann) {
    const verified = el('div', {},
      (meta.device_count != null ? meta.device_count + ' devices, ' : '') +
      f.distinct_fields + ' distinct fields, ' +
      (meta.all_groups_touched || []).length + ' MemberOf group(s). ' +
      'Declared in ' + f.sources.join(', ') + '.');
    app.append(el('div', { class: 'card', style: 'margin-top:14px; padding:0; overflow:hidden' },
      annotationCard({
        title: fam_ann.title + ' — ' + name,
        subtitle: 'Reference annotation · three confidence tiers',
        verified: [verified], context: fam_ann.context, unknowns: fam_ann.unknowns,
      })));
  }

  app.append(el('div', { class: 'card', style: 'margin-top:14px' },
    el('h2', {}, 'Fields (' + Object.keys(f.fields).length + ')'),
    el('div', { class: 'field-tree' },
      Object.entries(f.fields).sort((a,b) => a[0].localeCompare(b[0])).map(([fname, entry]) => {
        const modeKeys = Object.keys(entry.mode_specific || {});
        return el('div', { class: 'field-row' },
          el('div', {},
            el('strong', {}, fname),
            ' ',
            el('span', { class: 'small' }, '— ' + entry.rhs_kinds.join('/') +
              (entry.is_indexed_update ? ' (indexed update)' : '') +
              (entry.comment ? '  // ' + entry.comment : '')
            ),
          ),
          entry.values.length ? el('div', { class: 'field-value' },
            entry.values.slice(0, 3).map(v => (v.length > 100 ? v.slice(0, 100)+'…' : v)).join('  |  ')
          ) : null,
          modeKeys.length ? el('div', { class: 'field-mode' },
            'mode-specific: ' + modeKeys.map(k => `${k} (${entry.mode_specific[k].length})`).join(', ')
          ) : null,
          el('div', { class: 'small' }, ...refsList(entry.sources, 4)),
        );
      })
    )
  ));

  // Cross-ref: which files touch this family
  const referrers = DATA.family_to_files[name] || [];
  app.append(el('div', { class: 'card', style: 'margin-top:14px' },
    el('h2', {}, 'Files referencing this family (' + referrers.length + ')'),
    el('div', { class: 'small' }, 'Includes string mentions like ' + "getsp('" + name + "', …)" +
      ' and direct struct access like AO.' + name + '.… Click a file for its detail page.'),
    referrers.length ? el('div', { class: 'scroll', style:'max-height: 400px; margin-top:8px' },
      el('table', {}, el('tbody', {},
        referrers.slice(0, 400).map(r => el('tr', {},
          el('td', { class: 'num' }, fmt(r.total_refs)),
          el('td', { class: 'small' },
            (r.string_refs ? `${r.string_refs}str ` : '') +
            (r.struct_refs ? `${r.struct_refs}struct` : '')),
          el('td', {}, link(shortPath(r.file), () => navigateTo('file', r.file))),
          el('td', { class: 'small', style: 'color:var(--muted)' },
            (r.sample_lines[0] ? `:${r.sample_lines[0][0]} ` + r.sample_lines[0][1].slice(0,80) : '')),
        ))
      ))
    ) : el('div', { class: 'small' }, '(no static references detected — paren-less calls and dynamic dispatch may hide some)'),
    referrers.length > 400 ? el('div', { class: 'small', style:'padding:8px' },
      `(${referrers.length - 400} more — narrow your scope)`) : null,
  ));
}

// -- AD tab ----------------------------------------------------------------
function renderADList() {
  app.innerHTML = '';
  const rows = Object.entries(DATA.ad).map(([path, entry]) => ({path, ...entry}));
  const table = sortableTable(rows, [
    { key: 'path', label: 'AD path', value: r => r.path,
      render: r => link('AD.' + r.path, () => navigateTo('adpath', r.path)) },
    { key: 'assignment_count', label: 'assignments', value: r => r.assignment_count, num: true },
    { key: 'distinct', label: 'distinct vals', value: r => r.distinct_value_count, num: true },
    { key: 'kinds', label: 'kinds', value: r => r.rhs_kinds.join(','),
      render: r => r.rhs_kinds.map(k => el('span',{class:'badge'}, k)) },
    { key: 'modes', label: 'mode-specific?',
      value: r => Object.keys(r.mode_specific).length, num: true,
      render: r => Object.keys(r.mode_specific).map(m => el('span',{class:'badge mode'}, m)) },
  ], {
    filterPlaceholder: 'filter AD paths…',
    filterFn: (r, t) => r.path.toLowerCase().includes(t),
    initialSort: { key: 'assignment_count', dir: 'desc' },
    onClick: r => navigateTo('adpath', r.path),
  });
  app.append(table);
}

function showADDetail(p) {
  const entry = DATA.ad[p];
  app.innerHTML = '';
  app.append(el('div', { class: 'breadcrumb' },
    link('← back to AD paths', () => navigateTo('ad'))));
  app.append(el('div', { class: 'card' },
    el('h2', {}, 'AD PATH'),
    el('div', { class: 'stat' }, 'AD.' + p),
    el('div', { class: 'stat-row', style: 'margin-top:10px' },
      stat('assignments', entry.assignment_count),
      stat('distinct values', entry.distinct_value_count),
    ),
    el('div', { class: 'small', style: 'margin-top:8px' },
      'Sources: ', entry.sources.join(', ')),
    entry.comment ? el('div', { class: 'small', style: 'margin-top:4px' },
      'Comment: ' + entry.comment) : null,
  ));
  app.append(el('div', { class: 'card', style: 'margin-top:14px' },
    el('h2', {}, 'Values seen'),
    el('div', { class: 'field-tree' },
      entry.values.map(v => el('div', { class: 'field-row' }, v))
    )
  ));
  if (Object.keys(entry.mode_specific).length) {
    app.append(el('div', { class: 'card', style: 'margin-top:14px' },
      el('h2', {}, 'Mode-specific overrides'),
      Object.entries(entry.mode_specific).map(([mid, occs]) =>
        el('div', { style: 'margin-bottom:8px' },
          el('div', {},
            el('strong', {},
              link('mode ' + mid + ': ' + (DATA.modes[mid]?.name || ''),
                () => navigateTo('mode', mid)))),
          occs.map(o => el('div', { class: 'field-tree field-row' },
            o.value,
            el('span', { class: 'small', style: 'color:var(--muted); margin-left:8px' },
              '— ', refToEl(o.source)))))
      )
    ));
  }
  // Cross-ref: files that read this AD path directly
  const referrers = DATA.ad_to_files[p] || [];
  app.append(el('div', { class: 'card', style: 'margin-top:14px' },
    el('h2', {}, 'Files referencing AD.' + p + ' (' + referrers.length + ')'),
    referrers.length ? el('div', { class: 'scroll', style:'max-height: 400px' },
      el('table', {}, el('tbody', {},
        referrers.slice(0, 300).map(r => el('tr', {},
          el('td', { class: 'num' }, fmt(r.refs)),
          el('td', {}, link(shortPath(r.file), () => navigateTo('file', r.file))),
          el('td', { class: 'small', style: 'color:var(--muted)' },
            r.sample_lines[0] ? `:${r.sample_lines[0][0]} ` + r.sample_lines[0][1].slice(0,80) : '')
        ))
      ))
    ) : el('div', { class: 'small' }, '(no direct references)'),
  ));
}

// -- Modes tab -------------------------------------------------------------
function renderModesList() {
  app.innerHTML = '';
  const rows = Object.entries(DATA.modes).map(([mid, m]) => ({mid, ...m}));
  const table = sortableTable(rows, [
    { key: 'mid', label: 'mode #', value: r => parseInt(r.mid),
      render: r => link(r.mid, () => navigateTo('mode', r.mid)) },
    { key: 'name', label: 'name', value: r => r.name },
    { key: 'assignment_count', label: 'overrides', value: r => r.assignment_count, num: true },
  ], {
    filterPlaceholder: 'filter modes…',
    filterFn: (r, t) => r.name.toLowerCase().includes(t),
    initialSort: { key: 'mid', dir: 'asc' },
    onClick: r => navigateTo('mode', r.mid),
  });
  app.append(table);
}

function showModeDetail(mid) {
  const m = DATA.modes[mid];
  app.innerHTML = '';
  app.append(el('div', { class: 'breadcrumb' },
    link('← back to modes', () => navigateTo('modes'))));
  app.append(el('div', { class: 'card' },
    el('h2', {}, 'OPERATIONAL MODE'),
    el('div', { class: 'stat' }, 'Mode ' + mid),
    el('div', {}, m.name),
    el('div', { class: 'small', style: 'margin-top:8px' },
      m.assignment_count + ' assignments override this mode'),
  ));
  app.append(el('div', { class: 'card', style: 'margin-top:14px' },
    el('h2', {}, 'Overrides'),
    el('div', { class: 'field-tree' },
      m.assignments.map(a => el('div', { class: 'field-row' },
        el('div', {}, el('strong', {}, a.lhs_full)),
        el('div', { class: 'field-value' }, a.value),
        el('div', { class: 'small' }, a.source + ' • ' + a.rhs_kind),
      ))
    )
  ));
}

// -- Subsystems tab --------------------------------------------------------
function renderSubsystemsList() {
  app.innerHTML = '';
  if (!DATA.subsystems) {
    app.append(el('div', { class: 'small' }, 'subsystems data not available — run extractor 09'));
    return;
  }
  const rows = DATA.subsystems.clusters;
  const table = sortableTable(rows, [
    { key: 'cluster', label: 'subsystem (directory)', value: r => r.cluster,
      render: r => link(r.cluster, () => navigateTo('subsystem', r.cluster)) },
    { key: 'file_count', label: 'files', value: r => r.file_count, num: true },
    { key: 'total_lines', label: 'lines', value: r => r.total_lines, num: true },
    { key: 'cohesion', label: 'cohesion', value: r => r.cohesion, num: true,
      render: r => r.cohesion.toFixed(2) },
    { key: 'internal_edges', label: 'internal', value: r => r.internal_edges, num: true },
    { key: 'external_edges_out', label: 'out-edges', value: r => r.external_edges_out, num: true },
    { key: 'external_edges_in', label: 'in-edges', value: r => r.external_edges_in, num: true },
    { key: 'archive_files', label: 'archive', value: r => r.archive_files, num: true },
  ], {
    filterPlaceholder: 'filter subsystems by directory…',
    filterFn: (r, t) => r.cluster.toLowerCase().includes(t),
    initialSort: { key: 'file_count', dir: 'desc' },
    onClick: r => navigateTo('subsystem', r.cluster),
  });
  app.append(el('div', { class: 'small', style: 'margin-bottom:10px' },
    'Cohesion = internal-edges / (internal + external + incoming). ' +
    '1.0 = self-contained; 0.0 = pure consumer/producer of other subsystems. ' +
    'High-cohesion clusters are natural port-as-a-unit candidates.'));
  app.append(table);
}

function showSubsystemDetail(cluster) {
  const c = DATA.subsystems.clusters.find(x => x.cluster === cluster);
  if (!c) return;
  const files = Object.entries(DATA.subsystems.file_to_cluster)
    .filter(([_, cl]) => cl === cluster).map(([p]) => p);
  app.innerHTML = '';
  app.append(el('div', { class: 'breadcrumb' },
    link('← back to subsystems', () => navigateTo('subsystems'))));
  app.append(el('div', { class: 'card' },
    el('h2', {}, 'SUBSYSTEM'),
    el('div', { class: 'stat' }, c.cluster),
    el('div', { class: 'stat-row', style: 'margin-top:10px' },
      stat('files', c.file_count),
      stat('lines', c.total_lines),
      stat('cohesion', c.cohesion.toFixed(2)),
      stat('archive', c.archive_files),
      stat('internal edges', c.internal_edges),
      stat('out edges', c.external_edges_out),
      stat('in edges', c.external_edges_in),
    ),
  ));
  app.append(el('div', { class: 'grid cols-2', style: 'margin-top:14px' },
    el('div', { class: 'card' },
      el('h2', {}, 'Top out-targets (this subsystem depends on)'),
      c.top_out_targets.length ? el('table', {}, el('tbody', {},
        c.top_out_targets.map(t => el('tr', {},
          el('td', { class: 'num' }, fmt(t.edges)),
          el('td', {}, link(t.cluster, () => navigateTo('subsystem', t.cluster))))))) :
      el('div', { class: 'small' }, '(no external out-edges)'),
    ),
    el('div', { class: 'card' },
      el('h2', {}, 'Top in-sources (subsystems that depend on this)'),
      c.top_in_sources.length ? el('table', {}, el('tbody', {},
        c.top_in_sources.map(t => el('tr', {},
          el('td', { class: 'num' }, fmt(t.edges)),
          el('td', {}, link(t.cluster, () => navigateTo('subsystem', t.cluster))))))) :
      el('div', { class: 'small' }, '(no incoming edges)'),
    ),
  ));
  app.append(el('div', { class: 'card', style: 'margin-top:14px' },
    el('h2', {}, 'Files in this subsystem (' + files.length + ')'),
    el('div', { class: 'scroll', style: 'max-height: 360px' },
      el('table', {}, el('tbody', {},
        files.slice().sort((a, b) => {
          const na = NODES_BY_PATH[a]?.in_live_only || 0;
          const nb = NODES_BY_PATH[b]?.in_live_only || 0;
          return nb - na;
        }).map(p => {
          const n = NODES_BY_PATH[p];
          return el('tr', {},
            el('td', { class: 'num' }, fmt(n?.in_live_only)),
            el('td', { class: 'num' }, fmt(n?.line_count)),
            el('td', {}, link(p.split('/').pop(), () => navigateTo('file', p))),
            el('td', { class: 'small' }, fileBadges(n || {})),
          );
        })
      ))
    )
  ));
}

// -- Search tab ------------------------------------------------------------
// Greps every embedded source file for a substring or /regex/ pattern and
// shows the matches. Decompresses the source bundle on first use (one-time
// cost per page session). For very large queries (matches >2000) we cap to
// keep the DOM small; refine the query to see the rest.
let SEARCH_STATE = { query: '', regex: false, ignoreCase: true };
function renderSearchTab() {
  app.innerHTML = '';
  app.append(el('div', { class: 'card' },
    el('h2', {}, 'Search source'),
    el('div', { class: 'small' },
      'Greps the 1,998 embedded live source files. ',
      'Wrap a query in / / to use a JavaScript regex (e.g. ',
      el('code', {}, '/feval\\([^)]+\\)/'),
      '). Case-insensitive by default; toggle exact case below. ',
      'Click any result to jump to that file and line.'),
  ));

  const card = el('div', { class: 'card', style: 'margin-top:14px; padding:0' });
  const top = el('div', { style: 'padding:10px 14px; border-bottom:1px solid var(--border)' });
  const input = el('input', {
    class: 'filter',
    type: 'search',
    style: 'margin:0; width:60%; display:inline-block',
    placeholder: 'substring or /regex/…',
    value: SEARCH_STATE.query,
  });
  const caseBtn = el('button', {
    style: 'background:var(--panel-2); border:1px solid var(--border); color:var(--text); ' +
           'font:inherit; padding:4px 10px; margin-left:8px; border-radius:3px; cursor:pointer;',
    onClick: () => {
      SEARCH_STATE.ignoreCase = !SEARCH_STATE.ignoreCase;
      caseBtn.textContent = 'Aa: ' + (SEARCH_STATE.ignoreCase ? 'ignore' : 'match');
      runSearch();
    },
  }, 'Aa: ' + (SEARCH_STATE.ignoreCase ? 'ignore' : 'match'));
  const goBtn = el('button', {
    style: 'background:var(--accent); border:none; color:#fff; ' +
           'font:inherit; padding:4px 14px; margin-left:8px; border-radius:3px; cursor:pointer;',
    onClick: () => runSearch(),
  }, 'search');
  input.addEventListener('keydown', e => { if (e.key === 'Enter') runSearch(); });
  top.append(input, caseBtn, goBtn);
  card.append(top);

  const results = el('div', { style: 'padding:4px 0' });
  card.append(results);
  app.append(card);

  function runSearch() {
    SEARCH_STATE.query = input.value;
    const q = input.value.trim();
    results.innerHTML = '';
    if (!q) {
      results.append(el('div', { class: 'search-progress' }, '(empty query)'));
      return;
    }
    // Decompress bundle (cached after first call).
    results.append(el('div', { class: 'search-progress' },
      'searching… (decompressing source bundle on first search may take a second)'));
    // Use requestAnimationFrame so the progress message paints before the
    // synchronous decompression+grep blocks.
    requestAnimationFrame(() => {
      const t0 = performance.now();
      const dict = ensureSources();
      let matcher;
      let flags = SEARCH_STATE.ignoreCase ? 'i' : '';
      const regexMatch = q.match(/^\/(.*)\/([gimsuy]*)$/);
      try {
        if (regexMatch) {
          matcher = new RegExp(regexMatch[1], regexMatch[2] || flags);
        } else {
          matcher = new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), flags);
        }
      } catch (e) {
        results.innerHTML = '';
        results.append(el('div', { class: 'search-progress' },
          'invalid regex: ' + e.message));
        return;
      }
      const hits = [];
      const MAX_HITS = 2000;
      let scanned = 0;
      const paths = Object.keys(dict);
      for (const path of paths) {
        scanned++;
        const text = dict[path];
        // Quick reject before splitting lines.
        if (!matcher.test(text)) { matcher.lastIndex = 0; continue; }
        matcher.lastIndex = 0;
        const lines = text.split('\n');
        for (let i = 0; i < lines.length; i++) {
          if (matcher.test(lines[i])) {
            matcher.lastIndex = 0;
            hits.push({ path, line: i + 1, text: lines[i] });
            if (hits.length >= MAX_HITS) break;
          } else {
            matcher.lastIndex = 0;
          }
        }
        if (hits.length >= MAX_HITS) break;
      }
      const ms = (performance.now() - t0).toFixed(0);
      results.innerHTML = '';
      const summary = el('div', { class: 'search-progress' },
        hits.length + (hits.length >= MAX_HITS ? '+ (capped)' : '') +
        ' hits across ' + scanned + ' files in ' + ms + 'ms');
      results.append(summary);
      if (!hits.length) {
        results.append(el('div', { class: 'small', style: 'padding:14px' }, '(no matches)'));
        return;
      }
      // Group by file for readability.
      const byFile = {};
      hits.forEach(h => { (byFile[h.path] = byFile[h.path] || []).push(h); });
      const filesOrder = Object.keys(byFile).sort((a, b) => byFile[b].length - byFile[a].length);
      filesOrder.forEach(p => {
        const fileHits = byFile[p];
        const node = NODES_BY_PATH[p] || {};
        const fileBlock = el('div', { class: 'search-result',
          onClick: () => navigateTo('file', p) });
        fileBlock.append(el('div', {},
          el('strong', {}, p.split('/').pop()),
          el('span', { class: 'meta', style: 'margin-left:8px' },
            p.replace(/[^/]+$/, ''), ' · ', String(fileHits.length), ' hits')
        ));
        fileHits.slice(0, 6).forEach(h => {
          // Highlight match within snippet (just the first occurrence per line).
          const snippet = h.text.length > 240 ? h.text.slice(0, 240) + '…' : h.text;
          let html = escapeHtml(snippet);
          try {
            const re2 = matcher.flags.includes('g') ? matcher :
              new RegExp(matcher.source, matcher.flags + 'g');
            html = escapeHtml(snippet).replace(
              new RegExp(re2.source, re2.flags),
              m => '<mark>' + escapeHtml(m) + '</mark>'
            );
          } catch (e) { /* leave plain */ }
          const row = el('div', { class: 'snippet',
            onClick: ev => {
              ev.stopPropagation();
              navigateTo('file', p, h.line ? 'L' + h.line : null);
            } });
          row.innerHTML = ':' + h.line + '  ' + html;
          fileBlock.append(row);
        });
        if (fileHits.length > 6) {
          fileBlock.append(el('div', { class: 'meta', style: 'padding-left:12px' },
            '… +' + (fileHits.length - 6) + ' more in this file'));
        }
        results.append(fileBlock);
      });
    });
  }
}

// -- Workflows tab ---------------------------------------------------------
function renderWorkflowsList() {
  app.innerHTML = '';
  if (!DATA.workflows) {
    app.append(el('div', { class: 'small' },
      'workflows data not available — run extractor 11'));
    return;
  }
  const rows = DATA.workflows.workflows;

  app.append(el('div', { class: 'card' },
    el('h2', {}, 'Inferred workflows'),
    el('div', { class: 'small' },
      'Workflows inferred by clustering files via naming conventions ' +
      '(filename prefix and a small set of workflow keywords) and propagating ' +
      'through the call graph. Each row is a candidate procedure operators ' +
      'invoke. ',
      el('strong', {}, 'OWNED'), ' files share the workflow name; ',
      el('strong', {}, 'SPECIFIC'), ' files are reachable in-tree dependencies ' +
      'called by few other workflows; ',
      el('strong', {}, 'SHARED'), ' files are the broad MML infrastructure ' +
      '(getpv, setpv, getfamilydata, …) — port once as the API surface.'),
    el('div', { class: 'small', style: 'margin-top:6px;color:var(--warn)' },
      'These are STATIC inferences — they catch what file naming and the call graph ' +
      'say. They miss runtime/GUI dispatch and may misclassify utility prefixes ' +
      'as workflows. Treat as a starting list, not ground truth.')
  ));

  const table = sortableTable(rows, [
    { key: 'name', label: 'workflow', value: r => r.name,
      render: r => link(r.name, () => navigateTo('workflow', r.name)) },
    { key: 'scale', label: 'scale', value: r => r.scale,
      render: r => el('span', { class: 'badge' + (r.scale === 'subsystem' ? ' mode' : '') },
        r.scale) },
    { key: 'owned_count', label: 'owned', value: r => r.owned_count, num: true },
    { key: 'specific_count', label: 'specific', value: r => r.specific_count, num: true },
    { key: 'owned_lines', label: 'owned lines', value: r => r.owned_lines, num: true },
    { key: 'unique_procedures', label: 'unique', value: r => r.unique_procedures, num: true },
    { key: 'historical_revisions', label: 'hist rev',
      value: r => r.historical_revisions, num: true,
      render: r => r.historical_revisions
        ? el('span', { class: 'badge dyn' }, String(r.historical_revisions))
        : '' },
    { key: 'description', label: 'description', value: r => r.description || '',
      render: r => el('span', { class: 'small' },
        (r.description || '').slice(0, 110)) },
  ], {
    filterPlaceholder: 'filter workflows by name or description…',
    filterFn: (r, t) => r.name.toLowerCase().includes(t)
                     || (r.description || '').toLowerCase().includes(t),
    initialSort: { key: 'owned_count', dir: 'desc' },
    onClick: r => navigateTo('workflow', r.name),
  });
  app.append(table);
}

function showWorkflowDetail(name) {
  const w = DATA.workflows.workflows.find(x => x.name === name);
  if (!w) return;
  app.innerHTML = '';
  app.append(el('div', { class: 'breadcrumb' },
    link('← back to workflows', () => navigateTo('workflows'))));
  app.append(el('div', { class: 'card' },
    el('h2', {}, 'WORKFLOW (inferred via ' + w.kind + ')'),
    el('div', { class: 'stat' }, w.name),
    el('div', { class: 'small', style: 'margin-top:4px' }, w.description || '(no description)'),
    el('div', { class: 'stat-row', style: 'margin-top:10px' },
      stat('owned files', w.owned_count),
      stat('unique procedures', w.unique_procedures),
      stat('historical revs', w.historical_revisions),
      stat('owned lines', w.owned_lines),
      stat('specific deps', w.specific_count),
      stat('shared deps', w.shared_count),
      stat('scale', w.scale),
    ),
    w.lead_file ? el('div', { class: 'small', style: 'margin-top:6px' },
      'Lead entry point: ', link(w.lead_file, () => navigateTo('file', w.lead_file))) : null,
  ));

  // Top AO families + verbs
  app.append(el('div', { class: 'grid cols-2', style: 'margin-top:14px' },
    el('div', { class: 'card' },
      el('h2', {}, 'AO families touched (by owned + specific)'),
      w.top_families.length
        ? el('table', {},
            el('thead', {}, el('tr', {},
              el('th', { class: 'num' }, 'refs'),
              el('th', {}, 'family'))),
            el('tbody', {},
              w.top_families.map(([fam, n]) => el('tr', {},
                el('td', { class: 'num' }, fmt(n)),
                el('td', {}, link(fam, () => navigateTo('family', fam)))))))
        : el('div', { class: 'small' }, '(no static family references)'),
    ),
    el('div', { class: 'card' },
      el('h2', {}, 'Top verbs called'),
      w.top_verbs.length
        ? el('table', {},
            el('thead', {}, el('tr', {},
              el('th', { class: 'num' }, 'calls'),
              el('th', {}, 'verb'))),
            el('tbody', {},
              w.top_verbs.map(([verb, n]) => el('tr', {},
                el('td', { class: 'num' }, fmt(n)),
                el('td', {}, verb)))))
        : el('div', { class: 'small' }, '(no resolved calls)'),
    ),
  ));

  // Historical revisions (when present)
  if (w.historical_clusters && w.historical_clusters.length) {
    app.append(el('div', { class: 'card', style: 'margin-top:14px' },
      el('h2', {}, 'Historical revisions in this workflow'),
      el('div', { class: 'small' },
        'Files with date-stamp suffixes (e.g. _20231114) that share a base ' +
        'name with siblings — informal version-control by folder copy.'),
      w.historical_clusters.map(c => el('div', { style: 'margin-top:8px' },
        el('div', {}, el('strong', {}, c.canonical), ' — ', String(c.count), ' copies'),
        c.files.map(f => el('div', { class: 'small', style: 'padding-left:14px' },
          link(f, () => navigateTo('file', f))))))
    ));
  }

  // Explanatory card: the three-tier model + how to read the "called by" column.
  app.append(el('div', { class: 'card', style: 'margin-top:14px' },
    el('h2', {}, 'How to read the file lists below'),
    el('div', { class: 'small' },
      el('strong', {}, 'Owned'),
      ' — files whose name matches the workflow tag (the workflow\'s own files). ',
      el('br', {}),
      el('strong', {}, 'Workflow-specific dependencies'),
      ' — files NOT in the owned set, but reachable through the call graph ' +
      'from an owned file, and called by few other workflows. These are this ' +
      'workflow\'s private dependencies — port them with the workflow. ',
      el('br', {}),
      el('strong', {}, 'Shared infrastructure'),
      ' — also reachable, but used by many other workflows too (getpv, setpv, ' +
      'getfamilydata, etc.). Port once as part of the MML API surface, not ' +
      'per workflow. ',
      el('br', {}),
      'The ', el('strong', {}, '"called by"'),
      ' line under each file lists the OTHER files in this workflow\'s scope ' +
      '(owned + specific) that directly call it — so you can see why each ' +
      'dependency is part of the workflow.'),
  ));

  // File lists — three tiers.
  const callerInfo = w.callers_in_scope || {};
  const callerTruncated = w.callers_truncated || {};

  function fileList(title, paths, kindLabel) {
    return el('div', { class: 'card', style: 'margin-top:14px' },
      el('h2', {}, title + ' (' + paths.length + ')'),
      paths.length ? el('div', { class: 'scroll', style: 'max-height: 380px' },
        el('table', {},
          el('thead', {}, el('tr', {},
            el('th', { class: 'num' }, 'lines'),
            el('th', {}, 'file'),
            el('th', {}, 'called by (within this workflow)'))),
          el('tbody', {},
            paths.slice(0, 600).map(p => {
              const n = NODES_BY_PATH[p] || {};
              const callers = callerInfo[p] || [];
              const more = callerTruncated[p] || 0;
              // Build the inline caller list as a flat array (the `el` helper
              // only flattens children one level, so nested arrays would
              // serialize as [object Text]).
              const callerLinks = [];
              callers.forEach((c, i) => {
                if (i > 0) callerLinks.push(', ');
                callerLinks.push(el('a', {
                  class: 'small',
                  style: 'color:var(--accent); cursor:pointer; text-decoration:none',
                  title: c,
                  onClick: () => navigateTo('file', c),
                }, c.split('/').pop()));
              });
              if (more) callerLinks.push(
                el('span', { class: 'small',
                  style: 'color:var(--muted); margin-left:4px' },
                  `+${more} more`));

              let callerCell;
              if (callers.length) {
                callerCell = el('div', {},
                  el('div', { class: 'small', style: 'color:var(--muted)' },
                    p.replace(/[^/]+$/, '')),
                  el('div', { style: 'margin-top:2px' }, ...callerLinks),
                );
              } else if (kindLabel === 'specific') {
                // Specific dep with no direct in-scope caller = reached transitively.
                // Worth explaining since it's a real "why is this here?" answer.
                callerCell = el('div', {},
                  el('div', { class: 'small', style: 'color:var(--muted)' },
                    p.replace(/[^/]+$/, '')),
                  el('div', { class: 'small',
                    style: 'color:var(--muted); font-style:italic; margin-top:2px' },
                    '(reached transitively; no direct caller in workflow)'),
                );
              } else {
                // For owned & shared: blank caller cell when none. Owned files
                // with no in-scope callers are typically the entry points;
                // shared files are called by many other workflows by definition.
                callerCell = el('div', { class: 'small',
                  style: 'color:var(--muted)' },
                  p.replace(/[^/]+$/, ''));
              }
              return el('tr', {},
                el('td', { class: 'num' }, fmt(n.line_count)),
                el('td', {}, link(p.split('/').pop(), () => navigateTo('file', p))),
                el('td', {}, callerCell));
            })
          ))
      ) : el('div', { class: 'small' }, '(none)'),
      paths.length > 600 ? el('div', { class: 'small', style: 'padding:8px' },
        `(${paths.length - 600} more)`) : null,
    );
  }
  app.append(fileList('Owned files', w.owned, 'owned'));
  app.append(fileList('Workflow-specific dependencies', w.specific, 'specific'));
  app.append(fileList('Shared infrastructure (used by this + many workflows)',
                      w.shared, 'shared'));
}

// -- Groups tab ------------------------------------------------------------
function renderGroupsList() {
  app.innerHTML = '';
  if (!DATA.groups) {
    app.append(el('div',{class:'small'},'groups data not available — run extractor 10'));
    return;
  }
  app.append(el('div', { class: 'card' },
    el('h2', {}, 'MemberOf groups'),
    el('div', { class: 'small' },
      'Every AO field declares a MemberOf cell array tagging it with one or more groups. ' +
      'These groups are the implicit operational taxonomy: getmachineconfig iterates over ' +
      'MemberOf == \'MachineConfig\', the archiver walks \'Archive\', orbit correction ' +
      'walks \'BPM\', etc. The bigger groups are the broad organizational tags; the smaller ' +
      'ones tend to encode operationally-meaningful subsets.'),
  ));
  const rows = Object.entries(DATA.groups).map(([name, info]) => ({
    name, ...info,
  }));
  app.append(sortableTable(rows, [
    { key: 'name', label: 'group', value: r => r.name,
      render: r => link(r.name, () => navigateTo('group', r.name)) },
    { key: 'family_count', label: 'families', value: r => r.family_count, num: true },
    { key: 'field_count', label: 'field-level tags', value: r => r.field_count, num: true },
    { key: 'sample', label: 'sample families', value: r => 0,
      render: r => el('span', { class: 'small' },
        r.families.slice(0, 6).join(', ') + (r.families.length > 6 ? '…' : '')) },
  ], {
    filterPlaceholder: 'filter groups…',
    filterFn: (r, t) => r.name.toLowerCase().includes(t)
                      || r.families.some(f => f.toLowerCase().includes(t)),
    initialSort: { key: 'family_count', dir: 'desc' },
    onClick: r => navigateTo('group', r.name),
  }));
}

function showGroupDetail(name) {
  const g = DATA.groups[name];
  if (!g) return;
  app.innerHTML = '';
  app.append(el('div', { class: 'breadcrumb' },
    link('← back to groups', () => navigateTo('groups'))));
  app.append(el('div', { class: 'card' },
    el('h2', {}, 'MEMBEROF GROUP'),
    el('div', { class: 'stat' }, "'" + name + "'"),
    el('div', { class: 'stat-row', style: 'margin-top:10px' },
      stat('families in this group', g.family_count),
      stat('field-level tags', g.field_count),
    ),
  ));
  app.append(el('div', { class: 'card', style: 'margin-top:14px' },
    el('h2', {}, 'Families in this group (' + g.families.length + ')'),
    el('div', { class: 'small' },
      g.families.map(f => el('span', {
        class: 'badge', style: 'cursor:pointer; margin:2px',
        onClick: () => navigateTo('family', f),
      }, f)))
  ));
  app.append(el('div', { class: 'card', style: 'margin-top:14px' },
    el('h2', {}, 'Field-level tags (first ' + g.fields.length +
      (g.fields_total > g.fields.length ? ' of ' + g.fields_total : '') + ')'),
    el('div', { class: 'small' }, 'Each row is one place this group tag is applied. ' +
      'Field path is the sub-struct under the family (blank = the family itself).'),
    el('div', { class: 'scroll', style: 'max-height: 360px; margin-top:8px' },
      el('table', {}, el('tbody', {},
        g.fields.map(f => el('tr', {},
          el('td', {}, link(f.family, () => navigateTo('family', f.family))),
          el('td', { class: 'small' }, f.field_path || '(family-level)'),
          el('td', { class: 'small', style: 'color:var(--muted)' },
            f.file
              ? fileLineLink((f.file.split('/').pop()) + ':' + f.line, f.file, f.line)
              : '')
        ))
      ))
    )
  ));
}

// -- API surface tab -------------------------------------------------------
function renderAPISurface() {
  app.innerHTML = '';
  const data = DATA.api_surface || [];
  if (!data.length) {
    app.append(el('div', { class: 'small' }, 'API surface data not available — rebuild with extractor 06.'));
    return;
  }

  app.append(el('div', { class: 'card' },
    el('h2', {}, 'MML API surface (discovered by usage)'),
    el('div', { class: 'small' },
      'Top ', String(data.length), ' in-tree callables ranked by total call count across ',
      'the codebase. Each row is a function the MML actually uses heavily — the same ',
      'verbs any Python port would need to provide on day one, and the same surface ',
      'an agentic interface would expose as tools.'),
    el('div', { class: 'small', style: 'margin-top:6px' },
      el('span', { class: 'api-legend-swatch', style: 'background:var(--accent)' }),
      'paren calls — foo(args)',
      el('span', { class: 'api-legend-swatch', style: 'background:var(--accent-2)' }),
      'paren-less calls — foo;'),
    el('div', { class: 'small', style: 'margin-top:6px' },
      'Verb name shown on the left, resolved source path next to it ' +
      '(mml/ = generic core, machine/ALS/* = facility-specific). ' +
      'Click any row to open the implementing file.'),
  ));

  // Top-3 numbers for the summary card
  app.append(el('div', { class: 'grid cols-3', style: 'margin-top:14px' },
    statCard('verbs in surface', data.length, '(top of the long tail; full list in call_graph_edges.jsonl)'),
    statCard('total resolved calls', fmt(data.reduce((s, r) => s + r.total, 0)),
             'across these top callables'),
    statCard('verbs from mml/ core', data.filter(r => r.root === 'mml').length,
             'vs ' + data.filter(r => r.root !== 'mml').length + ' from facility-specific layers'),
  ));

  const wrap = el('div', { class: 'card', style: 'margin-top:14px; padding:0' });
  const controls = el('div', { style: 'padding:10px 14px; border-bottom:1px solid var(--border)' });
  let filterTerm = '';
  let rootFilter = 'all';

  const filter = el('input', {
    class: 'filter',
    placeholder: 'filter verbs by name…',
    type: 'search',
    style: 'margin:0; width: 280px; display:inline-block',
    oninput: e => { filterTerm = e.target.value.toLowerCase(); render(); },
  });
  controls.append(filter);
  controls.append(el('span', { class: 'small', style: 'margin-left:14px' }, 'show: '));
  ['all', 'mml', 'machine/ALS/Common', 'machine/ALS/StorageRing'].forEach(r => {
    const btn = el('button', {
      style: 'background:var(--panel-2);border:1px solid var(--border);color:var(--text);' +
             'font:inherit;padding:4px 10px;margin-left:4px;border-radius:3px;cursor:pointer;',
      onClick: () => { rootFilter = r; render(); }
    }, r === 'all' ? 'all' : r.split('/').pop());
    controls.append(btn);
  });
  wrap.append(controls);

  const list = el('div', { style: 'padding:6px 0' });
  wrap.append(list);
  app.append(wrap);

  const maxTotal = data[0].total;

  function render() {
    const filtered = data.filter(r =>
      (rootFilter === 'all' || r.root === rootFilter) &&
      (!filterTerm || r.name.toLowerCase().includes(filterTerm))
    );
    list.innerHTML = '';
    if (!filtered.length) {
      list.append(el('div', { class: 'small', style: 'padding:14px' }, '(no matches)'));
      return;
    }
    const verbAnns = (DATA.annotations || {}).verbs || {};
    filtered.forEach(r => {
      const hasAnn = !!verbAnns[r.name];
      const row = el('div', { class: 'api-row',
        onClick: () => hasAnn ? navigateTo('verb', r.name)
                              : (r.resolved_path && navigateTo('file', r.resolved_path)) });
      row.append(el('div', { class: 'api-name', title: hasAnn ? 'has reference annotation' : r.name },
        r.name, hasAnn ? el('span', { class: 'badge', style: 'margin-left:6px' }, 'doc') : null));
      row.append(el('div', { class: 'api-root', title: r.resolved_path },
        r.resolved_path ? r.resolved_path.replace(/^machine\/ALS\//, 'a/') : ''));

      const track = el('div', { class: 'api-bar-track' });
      const pct = (r.total / maxTotal) * 100;
      const parenPct = (r.paren / r.total) * pct;
      const parenlessPct = (r.parenless / r.total) * pct;
      if (parenPct > 0) {
        const seg = el('div', { class: 'api-bar-paren', title: r.paren + ' paren calls' });
        seg.style.width = parenPct + '%';
        track.append(seg);
      }
      if (parenlessPct > 0) {
        const seg = el('div', { class: 'api-bar-parenless',
          title: r.parenless + ' paren-less calls' });
        seg.style.width = parenlessPct + '%';
        track.append(seg);
      }
      row.append(track);

      row.append(el('div', { class: 'api-stats',
        title: 'total / distinct caller files' },
        fmt(r.total) + ' · ' + fmt(r.caller_count) + 'f'));
      list.append(row);
    });
  }
  render();
}

function showVerbDetail(name) {
  const v = (DATA.api_surface || []).find(r => r.name === name);
  const ann = ((DATA.annotations || {}).verbs || {})[name];
  app.innerHTML = '';
  app.append(el('div', { class: 'breadcrumb' },
    link('← back to API surface', () => navigateTo('api'))));
  app.append(el('div', { class: 'card' },
    el('h2', {}, 'VERB'),
    el('div', { class: 'stat' }, name),
    v ? el('div', { class: 'stat-row', style: 'margin-top:10px' },
      stat('total calls', fmt(v.total)),
      stat('paren', fmt(v.paren)),
      stat('paren-less', fmt(v.parenless)),
      stat('caller files', fmt(v.caller_count)),
    ) : el('div', { class: 'small', style: 'margin-top:8px' },
      '(not in the top-100 API surface — aggregate call stats unavailable)'),
    (ann && ann.signature_inferred) ? el('div', { style: 'margin-top:10px' },
      el('span', { class: 'small', style: 'color:var(--accent-2)' }, 'Inferred signature (verify against source): '),
      el('div', { style: 'margin-top:3px' }, el('code', {}, ann.signature_inferred))) : null,
    (v && v.resolved_path) ? el('div', { class: 'small', style: 'margin-top:10px' },
      link('open source → ' + v.resolved_path, () => navigateTo('file', v.resolved_path))) : null,
  ));
  if (ann) {
    const verified = v ? [el('div', {},
      fmt(v.total) + ' calls from ' + v.caller_count + ' files (' +
      v.paren + ' paren, ' + v.parenless + ' paren-less).' +
      (v.resolved_path ? ' Resolves to ' + v.resolved_path + '.' : ''))] : [];
    app.append(el('div', { class: 'card', style: 'margin-top:14px; padding:0; overflow:hidden' },
      annotationCard({
        title: ann.title + ' — ' + name,
        subtitle: 'Reference annotation · three confidence tiers',
        verified, context: ann.context, unknowns: ann.unknowns,
      })));
  } else {
    app.append(el('div', { class: 'card', style: 'margin-top:14px' },
      el('div', { class: 'small' }, 'No reference annotation authored for this verb yet.')));
  }
}

function switchActive(tabName) {
  tabs.forEach(b => b.classList.toggle('active', b.dataset.tab === tabName));
  currentTab = tabName;
}


// -- Helpers ---------------------------------------------------------------
function shortPath(p) {
  return p.replace('machine/ALS/', 'a/').replace(/^mml\//, 'mml/');
}

// -- Bootstrap -------------------------------------------------------------
route();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
