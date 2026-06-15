#!/usr/bin/env python3
"""
Per-family schema view (slice 4 of the audit).

Consolidates the AO/AD assignment records from extractors 02 and 03 into a
per-family (and AD) "schema" view — the thing you actually want to look at to
answer questions like:

  - What fields does AO.BPM have, and what are their values?
  - Which modes override AO.BPMx.Monitor.Golden?
  - Where in the source is AD.Energy declared?
  - What channel-name patterns does HCMFOFB use?

Inputs (must exist):
  audit/data/ao_assignments.jsonl
  audit/data/ao_helper_assignments.jsonl

Outputs:
  audit/data/family_schema.json         one record per AO family
  audit/data/ad_schema.json             one record per AD path
  audit/data/mode_overrides.json        which (family, field) is mode-specific
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO = Path("/home/kiliev/Documents/Code/LBL/mmlt")
DATA = REPO / "audit" / "data"
IN_FILES = [DATA / "ao_assignments.jsonl", DATA / "ao_helper_assignments.jsonl"]

OUT_FAMILY = DATA / "family_schema.json"
OUT_AD = DATA / "ad_schema.json"
OUT_MODES = DATA / "mode_overrides.json"


def load_records() -> list[dict]:
    records: list[dict] = []
    for path in IN_FILES:
        if not path.exists():
            print(f"warn: missing {path}", file=sys.stderr)
            continue
        with path.open() as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    return records


def short_source(rec: dict) -> str:
    """Compact source location string for use in schema entries."""
    base = Path(rec["file"]).name
    return f"{base}:{rec['line']}"


def build_field_entry(field_records: list[dict]) -> dict[str, Any]:
    """Collapse all assignments to one family.subpath into a schema entry."""
    # Sort so the "base" (non-mode-gated) assignments come first; mode-gated
    # overrides follow. This makes the entry read top-down: default then
    # exceptions.
    field_records.sort(key=lambda r: (bool(r.get("mode_ids")), r["file"], r["line"]))

    distinct_values: list[str] = []
    seen_values: set[str] = set()
    for r in field_records:
        v = r["rhs_raw"]
        if v not in seen_values:
            seen_values.add(v)
            distinct_values.append(v)

    # Mode-specific assignments: group by mode_id with the value seen there.
    mode_values: dict[str, list[dict]] = defaultdict(list)
    for r in field_records:
        for mid in r.get("mode_ids", []) or []:
            mode_values[str(mid)].append({
                "source": short_source(r),
                "value": r["rhs_raw"],
                "rhs_kind": r["rhs_kind"],
                "comment": r.get("comment", ""),
            })

    # Pick one representative "base" record for the headline kind/comment.
    base_record = next((r for r in field_records if not r.get("mode_ids")), field_records[0])

    return {
        "assignment_count": len(field_records),
        "distinct_value_count": len(distinct_values),
        "values": distinct_values[:8],     # cap to keep the artifact readable
        "rhs_kinds": sorted({r["rhs_kind"] for r in field_records}),
        "comment": base_record.get("comment", ""),
        "is_indexed_update": all(r.get("is_indexed_update") for r in field_records),
        "any_indexed_update": any(r.get("is_indexed_update") for r in field_records),
        "sources": sorted({short_source(r) for r in field_records}),
        "mode_specific": dict(mode_values),
    }


def main() -> int:
    records = load_records()
    if not records:
        print("error: no input records found — run extractors 02 and 03 first.", file=sys.stderr)
        return 1

    # Bucket: AO records keyed by (family, subpath); AD records by subpath.
    ao_buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    ad_buckets: dict[str, list[dict]] = defaultdict(list)

    for r in records:
        if r.get("root") != "AO" and r.get("root") != "AD":
            continue  # skip unparseable / dynamic_field / rebind sentinels
        if r.get("is_root_rebind"):
            continue
        if r["root"] == "AO":
            family = r.get("family")
            subpath = r.get("subpath", "") or ""
            if not family:
                continue
            ao_buckets[(family, subpath)].append(r)
        else:
            subpath = r.get("subpath", "") or ""
            ad_buckets[subpath].append(r)

    # ---- AO family schema ----
    family_index: dict[str, dict] = {}
    for (family, subpath), recs in ao_buckets.items():
        fam_entry = family_index.setdefault(family, {
            "family": family,
            "total_assignments": 0,
            "fields": {},
            "modes_touched": set(),
            "via_helpers": set(),
            "sources": set(),
        })
        fam_entry["total_assignments"] += len(recs)
        fam_entry["fields"][subpath if subpath else "<root>"] = build_field_entry(recs)
        for r in recs:
            for mid in (r.get("mode_ids") or []):
                fam_entry["modes_touched"].add(int(mid))
            if r.get("via_helper"):
                fam_entry["via_helpers"].add(r["via_helper"])
            fam_entry["sources"].add(Path(r["file"]).name)

    # Convert sets to sorted lists and stamp a couple of summary numbers.
    family_schema = {}
    for family, entry in sorted(family_index.items()):
        entry["modes_touched"] = sorted(entry["modes_touched"])
        entry["via_helpers"] = sorted(entry["via_helpers"])
        entry["sources"] = sorted(entry["sources"])
        entry["distinct_fields"] = len(entry["fields"])
        entry["has_unresolved_subpath"] = any("*" in k for k in entry["fields"])
        family_schema[family] = entry

    # ---- AD schema ----
    ad_schema = {}
    for subpath, recs in sorted(ad_buckets.items()):
        ad_schema[subpath] = build_field_entry(recs)

    # ---- Mode overrides view: per-mode, list the (family|AD, field) entries it sets ----
    per_mode: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        if r.get("is_root_rebind") or not r.get("lhs_full"):
            continue
        mode_ids = r.get("mode_ids") or []
        if not mode_ids:
            continue
        for mid in mode_ids:
            per_mode[str(mid)].append({
                "lhs_full": r["lhs_full"],
                "family": r.get("family"),
                "subpath": r.get("subpath"),
                "value": r["rhs_raw"],
                "rhs_kind": r["rhs_kind"],
                "source": short_source(r),
            })

    # Add the operational-mode name discovered from each branch's
    # `AD.OperationalMode = '...'` line, for human-readable mode keys.
    mode_names: dict[str, str] = {}
    for r in records:
        if r.get("lhs_full") == "AD.OperationalMode":
            for mid in (r.get("mode_ids") or []):
                # First non-empty assignment wins (mode 0 is "" default).
                if str(mid) not in mode_names and r["rhs_raw"].strip("'") not in ("", ""):
                    mode_names[str(mid)] = r["rhs_raw"].strip("'")

    mode_overrides = {}
    for mid in sorted(per_mode, key=lambda x: int(x) if x.isdigit() else 0):
        mode_overrides[mid] = {
            "name": mode_names.get(mid, "<unnamed>"),
            "assignment_count": len(per_mode[mid]),
            "assignments": sorted(per_mode[mid], key=lambda a: a["lhs_full"]),
        }

    with OUT_FAMILY.open("w") as f:
        json.dump(family_schema, f, indent=2, ensure_ascii=False)
    with OUT_AD.open("w") as f:
        json.dump(ad_schema, f, indent=2, ensure_ascii=False)
    with OUT_MODES.open("w") as f:
        json.dump(mode_overrides, f, indent=2, ensure_ascii=False)

    print(f"wrote {len(family_schema)} AO families to {OUT_FAMILY.relative_to(REPO)}")
    print(f"wrote {len(ad_schema)} AD paths to {OUT_AD.relative_to(REPO)}")
    print(f"wrote {len(mode_overrides)} mode override sets to {OUT_MODES.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
