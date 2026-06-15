#!/usr/bin/env python3
"""
AO/AD assignment extractor (slice 2 of the audit).

Processes files that declare the AO (per-family) and AD (machine-wide) structs
directly — by uppercase `AO.<family>.<field> = ...` or `AD.<path> = ...` —
emitting one record per assignment site.

Sources:
  - machine/ALS/StorageRing/alsinit.m
  - machine/ALS/StorageRing/setoperationalmode.m
  - machine/ALS/StorageRing/buildml_sextupole_harmonic.m
        (called from alsinit; uses uppercase `AO` with a literal family name)

Helpers that mutate the AO via a *lowercase* `ao` parameter and a dynamic
`(Family)` field — `buildmmlbpmfamily`, `buildmmlcaenfastps` — are handled by
the sibling `03_helper_rebinds.py` extractor.

Output:
  audit/data/ao_assignments.jsonl
  audit/data/ao_assignments_summary.json
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make sibling `_mlab.py` importable regardless of where this script is run.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _mlab import (
    assemble_logical_line,
    classify_rhs,
    read_lines,
    update_context_stack,
)

REPO = Path("/home/kiliev/Documents/Code/LBL/mmlt")
OUT_DIR = REPO / "audit" / "data"
OUT_JSONL = OUT_DIR / "ao_assignments.jsonl"
OUT_SUMMARY = OUT_DIR / "ao_assignments_summary.json"

SOURCES = [
    REPO / "machine" / "ALS" / "StorageRing" / "alsinit.m",
    REPO / "machine" / "ALS" / "StorageRing" / "setoperationalmode.m",
    REPO / "machine" / "ALS" / "StorageRing" / "buildmml_sextupole_harmonic.m",
]

ROOT_RE = re.compile(r"^\s*(?P<root>AO|AD)\b")
LHS_RE = re.compile(
    r"""^\s*
        (?P<root>AO|AD)
        (?P<subpath>(?:\.[A-Za-z_]\w*)+)
        (?P<index>(?:\([^=]*?\)|\{[^=]*?\}))?
        \s*=(?!=)\s*
        (?P<rhs>.*?)
        \s*$
    """,
    re.VERBOSE,
)
ROOT_REBIND_RE = re.compile(r"^\s*(?P<root>AO|AD)\s*=(?!=)\s*(?P<rhs>.*?)\s*$")
DYNAMIC_FIELD_RE = re.compile(r"\.\([^)]*\)")


def extract_from_file(path: Path) -> list[dict]:
    lines = read_lines(path)
    records: list[dict] = []
    stack: list[dict] = []
    rel = path.relative_to(REPO)

    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        stack = update_context_stack(raw, stack)

        if not ROOT_RE.match(raw):
            i += 1
            continue

        logical, trailing_comment, end_i, phys_count = assemble_logical_line(lines, i)
        mode_ids = [f["mode_id"] for f in stack if f.get("mode_id") is not None]

        rebind_match = ROOT_REBIND_RE.match(logical)
        if rebind_match:
            root = rebind_match.group("root")
            rhs = (rebind_match.group("rhs") or "").rstrip(";").strip()
            records.append({
                "file": str(rel),
                "line": i + 1,
                "physical_lines": phys_count,
                "root": root,
                "lhs_full": root,
                "family": None,
                "subpath": "",
                "is_indexed_update": False,
                "is_root_rebind": True,
                "rhs_raw": rhs,
                "rhs_kind": classify_rhs(rhs),
                "comment": trailing_comment,
                "context_stack": list(stack),
                "mode_ids": mode_ids,
            })
            i = end_i
            continue

        if DYNAMIC_FIELD_RE.search(logical.split("=", 1)[0]):
            records.append({
                "file": str(rel),
                "line": i + 1,
                "physical_lines": phys_count,
                "lhs_full": None,
                "rhs_raw": logical,
                "rhs_kind": "dynamic_field",
                "comment": trailing_comment,
                "context_stack": list(stack),
                "mode_ids": mode_ids,
            })
            i = end_i
            continue

        m = LHS_RE.match(logical)
        if not m:
            records.append({
                "file": str(rel),
                "line": i + 1,
                "physical_lines": phys_count,
                "lhs_full": None,
                "rhs_raw": logical,
                "rhs_kind": "unparseable",
                "comment": trailing_comment,
                "context_stack": list(stack),
                "mode_ids": mode_ids,
            })
            i = end_i
            continue

        root = m.group("root")
        subpath = m.group("subpath").lstrip(".")
        path_parts = subpath.split(".")
        family = path_parts[0] if root == "AO" else None
        field_subpath = ".".join(path_parts[1:]) if root == "AO" else subpath
        index_expr = m.group("index")
        rhs = (m.group("rhs") or "").rstrip(";").strip()

        records.append({
            "file": str(rel),
            "line": i + 1,
            "physical_lines": phys_count,
            "root": root,
            "lhs_full": f"{root}.{subpath}",
            "family": family,
            "subpath": field_subpath,
            "is_indexed_update": index_expr is not None,
            "is_root_rebind": False,
            "rhs_raw": rhs,
            "rhs_kind": classify_rhs(rhs),
            "comment": trailing_comment,
            "context_stack": [
                {"kind": f["kind"], "text": f["text"], "mode_id": f.get("mode_id")}
                for f in stack
            ],
            "mode_ids": mode_ids,
        })
        i = end_i

    return records


def build_summary(records: list[dict]) -> dict:
    by_file: dict[str, int] = {}
    by_root: dict[str, int] = {}
    by_rhs_kind: dict[str, int] = {}
    families: dict[str, dict] = {}
    ad_paths: set[str] = set()
    by_mode: dict[str, int] = {}
    dynamic_field_examples: list[dict] = []
    parse_failures: list[dict] = []
    indexed_updates = 0
    root_rebinds = 0

    for r in records:
        by_file[r["file"]] = by_file.get(r["file"], 0) + 1
        by_rhs_kind[r["rhs_kind"]] = by_rhs_kind.get(r["rhs_kind"], 0) + 1

        if r.get("lhs_full") is None:
            entry = {"file": r["file"], "line": r["line"], "snippet": r["rhs_raw"][:120]}
            if r["rhs_kind"] == "dynamic_field":
                dynamic_field_examples.append(entry)
            else:
                parse_failures.append(entry)
            continue

        if r.get("is_root_rebind"):
            root_rebinds += 1
            continue

        root = r["root"]
        by_root[root] = by_root.get(root, 0) + 1
        if r["is_indexed_update"]:
            indexed_updates += 1
        if root == "AO" and r["family"]:
            fam = families.setdefault(r["family"], {"assignments": 0, "fields": set()})
            fam["assignments"] += 1
            if r["subpath"]:
                fam["fields"].add(r["subpath"])
        elif root == "AD":
            ad_paths.add(r["subpath"])
        for mid in r["mode_ids"]:
            by_mode[str(mid)] = by_mode.get(str(mid), 0) + 1

    return {
        "total_assignments": len(records),
        "by_file": by_file,
        "by_root": by_root,
        "by_rhs_kind": by_rhs_kind,
        "indexed_updates": indexed_updates,
        "root_rebinds": root_rebinds,
        "parse_failures_count": len(parse_failures),
        "parse_failures_examples": parse_failures[:10],
        "dynamic_field_count": len(dynamic_field_examples),
        "dynamic_field_examples": dynamic_field_examples[:10],
        "ao_families": {
            name: {
                "assignments": info["assignments"],
                "distinct_fields": len(info["fields"]),
                "sample_fields": sorted(info["fields"])[:8],
            }
            for name, info in sorted(families.items())
        },
        "ad_distinct_paths": sorted(ad_paths),
        "by_mode": by_mode,
    }


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    all_records: list[dict] = []
    for src in SOURCES:
        if not src.exists():
            print(f"warn: missing source {src}", file=sys.stderr)
            continue
        all_records.extend(extract_from_file(src))

    with OUT_JSONL.open("w", encoding="utf-8") as f:
        for rec in all_records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    summary = build_summary(all_records)
    summary["extracted_at"] = datetime.now(timezone.utc).isoformat()

    with OUT_SUMMARY.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    print(f"wrote {len(all_records)} assignment records to {OUT_JSONL.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
