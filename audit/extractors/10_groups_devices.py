#!/usr/bin/env python3
"""
MemberOf groups + per-family device counts (slice 10 of the audit).

Two pieces of latent AO structure that nothing else in the audit currently
exposes:

  1. MemberOf groups — the implicit logical/operational taxonomy. Every AO
     field declares a `MemberOf` cell array tagging it with one or more
     groups: 'BPM', 'HBPM', 'Magnet', 'Diagnostics', 'PlotFamily',
     'MachineConfig', 'Save', 'Archive', 'Setpoint', 'Monitor', etc.
     These tags are how MML aggregates families for operations:
     `getmachineconfig` walks `MemberOf == 'MachineConfig'`, the archiver
     walks `MemberOf == 'Archive'`, orbit-correction walks `'BPM'`, etc.

  2. Device counts — each family's DeviceList is a matrix of [sector,
     device-index] rows. The row count tells you how many physical devices
     the family controls. Concrete physical scale (HCM has 96 correctors,
     RF has 1, etc.) for the audit's "what is each thing?" question.

Both come straight from `ao_assignments.jsonl` — no new source-tree scan
needed.

Outputs:
  audit/data/groups.json        group_name -> {families: [...], fields: [...]}
  audit/data/family_meta.json   family -> {groups: [...], device_count: N, ...}
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path("/home/kiliev/Documents/Code/LBL/mmlt")
DATA = REPO / "audit" / "data"
IN_FILES = [DATA / "ao_assignments.jsonl", DATA / "ao_helper_assignments.jsonl"]
OUT_GROUPS = DATA / "groups.json"
OUT_FAMILY_META = DATA / "family_meta.json"


def load_records() -> list[dict]:
    records: list[dict] = []
    for p in IN_FILES:
        if not p.exists():
            print(f"warn: missing {p}", file=sys.stderr)
            continue
        with p.open() as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    return records


# Extract single-quoted strings from a cell-array literal.
# `{'BPM'; 'HBPM'; 'Horizontal'; 'Save'}` -> ['BPM', 'HBPM', 'Horizontal', 'Save']
# Works whether the separator is `;` or `,` and whether the literal is one-line
# or had spanned multiple physical lines (the assignment extractor already
# joined those).
QUOTED_RE = re.compile(r"'([^']*)'")


def parse_cell_strings(rhs_raw: str) -> list[str]:
    return QUOTED_RE.findall(rhs_raw)


# Count matrix rows from a `[ ... ]` literal whose rhs_raw is one joined line.
# Strategy: strip the outer brackets, then split on `;`. MATLAB uses `;` to
# end each row of a matrix literal. Trailing `;` before `]` is common and
# should not count as an extra row.
def count_matrix_rows(rhs_raw: str) -> int | None:
    s = rhs_raw.strip().rstrip(";").strip()
    if not (s.startswith("[") and s.endswith("]")):
        return None
    inner = s[1:-1].strip()
    if not inner:
        return 0
    # If the matrix has explicit `;` row separators, count them; otherwise
    # assume one row only (single-line matrix like `[1 2 3]`).
    if ";" not in inner:
        return 1
    rows = [r.strip() for r in inner.split(";")]
    rows = [r for r in rows if r]   # drop trailing empty after final `;`
    return len(rows)


# Scan alsinit.m for `<var> = zeros(N, 2);` declarations so we can resolve
# `AO.HCM.DeviceList = HCMlist`-style references to a concrete row count.
# Limited to `zeros(N, ...)` and `ones(N, ...)` — these are the two patterns
# alsinit uses to pre-allocate device lists.
ALSINIT = REPO / "machine" / "ALS" / "StorageRing" / "alsinit.m"
ZEROS_RE = re.compile(
    r"^\s*([A-Za-z_]\w*)\s*=\s*(?:zeros|ones)\s*\(\s*(\d+)\s*,"
)


def load_var_to_rowcount() -> dict[str, int]:
    """`HCMlist = zeros(96, 2);` -> {'HCMlist': 96}. Returns {} if alsinit
    isn't where it's expected."""
    if not ALSINIT.exists():
        return {}
    out: dict[str, int] = {}
    for line in ALSINIT.read_text(encoding="utf-8", errors="replace").splitlines():
        m = ZEROS_RE.match(line)
        if m:
            out[m.group(1)] = int(m.group(2))
    return out


def main() -> int:
    records = load_records()
    if not records:
        sys.exit("error: no AO assignment records found.")

    var_to_rows = load_var_to_rowcount()

    # MemberOf extraction. We track:
    #   - which groups each family-level MemberOf declares (the canonical
    #     "this family is a member of these groups" relationship)
    #   - field-level memberships (more granular tagging, useful for
    #     understanding fine-grained iteration but secondary to the
    #     family-level view).
    group_to_families: dict[str, set[str]] = defaultdict(set)
    group_to_fields: dict[str, list[dict]] = defaultdict(list)
    family_groups: dict[str, dict] = defaultdict(
        lambda: {"family_level": set(), "field_level": defaultdict(set), "sources": []}
    )

    # Device counts: each family may declare DeviceList at the top level.
    # Some helpers also assign DeviceList — we keep the first non-empty
    # value seen per family, plus all sources for transparency.
    family_devices: dict[str, dict] = {}

    for r in records:
        if r.get("root") != "AO" or not r.get("family"):
            continue
        family = r["family"]
        subpath = r.get("subpath", "") or ""

        if subpath.endswith("MemberOf") or subpath == "MemberOf":
            groups = parse_cell_strings(r.get("rhs_raw", ""))
            sub_prefix = subpath[:-len("MemberOf")].rstrip(".") if subpath else ""
            for g in groups:
                group_to_families[g].add(family)
                group_to_fields[g].append({
                    "family": family,
                    "field_path": sub_prefix,
                    "file": r["file"],
                    "line": r["line"],
                })
            if not sub_prefix:
                # Family-level MemberOf declaration
                family_groups[family]["family_level"].update(groups)
            else:
                family_groups[family]["field_level"][sub_prefix].update(groups)
            family_groups[family]["sources"].append({
                "subpath": subpath,
                "groups": groups,
                "source": f"{Path(r['file']).name}:{r['line']}",
            })

        if subpath == "DeviceList":
            rhs = r.get("rhs_raw", "")
            count = count_matrix_rows(rhs)
            via = "literal"
            if count is None:
                # Try resolving a bare-variable RHS via the var_to_rows map.
                m = re.fullmatch(r"\s*([A-Za-z_]\w*)\s*", rhs)
                if m and m.group(1) in var_to_rows:
                    count = var_to_rows[m.group(1)]
                    via = f"resolved via {m.group(1)} = zeros(...)"
            if count is not None:
                family_devices.setdefault(family, {
                    "count": count,
                    "via": via,
                    "sources": [],
                })
                family_devices[family]["sources"].append({
                    "count": count,
                    "source": f"{Path(r['file']).name}:{r['line']}",
                    "via": via,
                })

    # Group output: per-group lists, sorted, with counts.
    groups_out = {}
    for g in sorted(group_to_families):
        fams = sorted(group_to_families[g])
        groups_out[g] = {
            "family_count": len(fams),
            "field_count": len(group_to_fields[g]),
            "families": fams,
            "fields": group_to_fields[g][:60],  # cap for browser size
            "fields_total": len(group_to_fields[g]),
        }

    # Family metadata output.
    family_meta = {}
    for family in sorted(set(list(family_groups) + list(family_devices))):
        fg = family_groups.get(family, {})
        fd = family_devices.get(family, {})
        family_meta[family] = {
            "family_level_groups": sorted(fg.get("family_level", set())),
            "field_level_groups": {
                fp: sorted(g) for fp, g in fg.get("field_level", {}).items()
            } if fg else {},
            "all_groups_touched": sorted(
                fg.get("family_level", set())
                | set(g for s in fg.get("field_level", {}).values() for g in s)
            ) if fg else [],
            "device_count": fd.get("count"),
            "device_count_via": fd.get("via", ""),
            "device_sources": fd.get("sources", []),
        }

    OUT_GROUPS.write_text(json.dumps(groups_out, indent=2, ensure_ascii=False))
    OUT_FAMILY_META.write_text(json.dumps(family_meta, indent=2, ensure_ascii=False))

    print(f"wrote {len(groups_out)} MemberOf groups to {OUT_GROUPS.relative_to(REPO)}")
    print(f"wrote {len(family_meta)} family meta records to {OUT_FAMILY_META.relative_to(REPO)}")
    print()
    print("top groups by family count:")
    top_groups = sorted(groups_out.items(), key=lambda kv: -kv[1]["family_count"])[:15]
    for g, info in top_groups:
        sample = ", ".join(info["families"][:6]) + ("…" if len(info["families"]) > 6 else "")
        print(f"  {info['family_count']:3d}  {g:20s}  {sample}")
    print()
    print("device counts (top 15):")
    by_devices = sorted(
        [(f, m["device_count"]) for f, m in family_meta.items() if m["device_count"]],
        key=lambda kv: -kv[1],
    )[:15]
    for f, n in by_devices:
        print(f"  {n:4d}  {f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
