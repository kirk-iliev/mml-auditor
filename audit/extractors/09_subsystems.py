#!/usr/bin/env python3
"""
Subsystem clustering (slice 9 of the audit).

Groups files by their containing directory and computes per-cluster
metrics over the call graph. Directory-based clustering is the right
unit for this codebase — subdirs like `StorageRing/LFB/` (Longitudinal
Feedback), `StorageRing/FAD/` (Fast Acquisition Digitizer),
`StorageRing/Lattices/`, `mml/online/labca/` are already authored as
coherent subsystems by the team. A formal community-detection algorithm
would mostly re-derive these boundaries.

Per-cluster outputs:
  - file_count, total_lines
  - internal_edges:  caller and callee both in this cluster
  - external_edges:  caller in this cluster, callee elsewhere
  - incoming_edges:  callee in this cluster, caller elsewhere
  - cohesion = internal / (internal + external + incoming)
                (1.0 = fully self-contained, 0.0 = pure consumer/producer)
  - external_top_targets: top 5 other clusters this one depends on

Output:
  audit/data/subsystems.json
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path("/home/kiliev/Documents/Code/LBL/mmlt")
DATA = REPO / "audit" / "data"

OUT = DATA / "subsystems.json"


def load_jsonl(name: str) -> list[dict]:
    return [json.loads(line) for line in (DATA / name).open() if line.strip()]


ARCHIVE_PARTS = {"Old", "old", "LegacyFiles", "_Attic", "Attic"}


def is_archive(parts: tuple[str, ...]) -> bool:
    return any(p in ARCHIVE_PARTS for p in parts)


def cluster_for(path_str: str) -> str:
    """Cluster = the containing directory.

    Archive subfolders are normalized so that all files under any
    `LegacyFiles/`/`Old/` etc. inside a subsystem still cluster with the
    parent subsystem — but flagged so the consumer can filter.
    """
    p = Path(path_str)
    parts = p.parts
    if is_archive(parts):
        # Drop the archive folder name from the cluster key.
        kept = [x for x in parts[:-1] if x not in ARCHIVE_PARTS]
        return "/".join(kept) or "/".join(parts[:-1])
    return "/".join(parts[:-1])


def main() -> int:
    file_index = load_jsonl("file_index.jsonl")
    nodes_by_path = {r["path"]: r for r in file_index}

    # Cluster membership.
    file_to_cluster: dict[str, str] = {p: cluster_for(p) for p in nodes_by_path}
    cluster_files: dict[str, list[str]] = defaultdict(list)
    for p, c in file_to_cluster.items():
        cluster_files[c].append(p)

    # Merge paren + paren-less edges into a single weighted-edge stream.
    edges_paren = load_jsonl("call_graph_edges_agg.json") if False else json.load(
        (DATA / "call_graph_edges_agg.json").open()
    )
    try:
        edges_pl = load_jsonl("parenless_call_edges.jsonl")
    except FileNotFoundError:
        edges_pl = []

    edge_bucket: dict[tuple[str, str], int] = defaultdict(int)
    for e in edges_paren:
        edge_bucket[(e["source"], e["target"])] += e["calls"]
    for e in edges_pl:
        edge_bucket[(e["caller"], e["callee_path"])] += 1

    # Aggregate per cluster.
    internal: dict[str, int] = defaultdict(int)
    external_out: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    external_in:  dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for (src, tgt), w in edge_bucket.items():
        c_src = file_to_cluster.get(src)
        c_tgt = file_to_cluster.get(tgt)
        if c_src is None or c_tgt is None:
            continue
        if c_src == c_tgt:
            internal[c_src] += w
        else:
            external_out[c_src][c_tgt] += w
            external_in[c_tgt][c_src] += w

    # Build per-cluster records.
    clusters = []
    for c, files in sorted(cluster_files.items()):
        total_lines = sum(nodes_by_path[f]["line_count"] for f in files)
        out_edges = sum(external_out[c].values())
        in_edges = sum(external_in[c].values())
        int_edges = internal[c]
        denom = int_edges + out_edges + in_edges
        cohesion = (int_edges / denom) if denom else 0.0
        archive_count = sum(1 for f in files
                            if any(p in ARCHIVE_PARTS for p in Path(f).parts))
        top_out = sorted(external_out[c].items(), key=lambda kv: -kv[1])[:5]
        top_in = sorted(external_in[c].items(), key=lambda kv: -kv[1])[:5]
        clusters.append({
            "cluster": c,
            "file_count": len(files),
            "archive_files": archive_count,
            "total_lines": total_lines,
            "internal_edges": int_edges,
            "external_edges_out": out_edges,
            "external_edges_in": in_edges,
            "cohesion": round(cohesion, 3),
            "top_out_targets": [{"cluster": k, "edges": v} for k, v in top_out],
            "top_in_sources":  [{"cluster": k, "edges": v} for k, v in top_in],
            "sample_files": sorted(files)[:8],
        })

    clusters.sort(key=lambda c: -c["file_count"])

    out = {
        "cluster_count": len(clusters),
        "clusters": clusters,
        "file_to_cluster": file_to_cluster,
    }
    with OUT.open("w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    print(f"wrote {len(clusters)} subsystem clusters to {OUT.relative_to(REPO)}")
    print()
    print(f"{'files':>6} {'lines':>8} {'cohes':>6}  cluster")
    for c in clusters[:25]:
        print(f"{c['file_count']:>6} {c['total_lines']:>8} {c['cohesion']:>6.2f}  {c['cluster']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
