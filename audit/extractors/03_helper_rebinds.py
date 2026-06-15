#!/usr/bin/env python3
"""
AO-helper-rebind extractor (slice 3 of the audit).

A handful of `alsinit.m` lines rebuild the AO via helper functions:

    AO = buildmmlbpmfamily(AO, 'StorageRing');   # alsinit:340
    AO = buildmmlbpmfamily(AO, 'SRTest');        # alsinit:366
    AO = buildmmlcaenfastps('HCMFOFB', AO);      # alsinit:979
    AO = buildmmlcaenfastps('VCMFOFB', AO);      # alsinit:980

Inside those helpers the struct is named `ao` (lowercase) and the family is a
*local string variable* (e.g. `Family = 'BPM'` or `Family = 'BPMTest'`) used
via MATLAB dynamic-field access: `ao.(Family).MemberOf = {...}`.

The third helper, `buildmml_sextupole_harmonic.m`, uses uppercase `AO` with a
literal family name `SQSHF`, so it's already covered by extractor 02.

This script processes each (helper × call site) pair, resolving the family
name from the caller's arguments, and emits records that look just like the
ones from extractor 02 — same schema — plus two extra provenance fields:

    via_helper       absolute path of the helper file (relative to repo)
    via_call_site    "alsinit.m:340"  (where the rebind originates)

The point: the per-family schema view (slice 4) can union these with the 02
records without special-casing helpers.

Output: audit/data/ao_helper_assignments.jsonl
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _mlab import (
    assemble_logical_line,
    classify_rhs,
    read_lines,
    update_context_stack,
)

REPO = Path("/home/kiliev/Documents/Code/LBL/mmlt")
OUT_DIR = REPO / "audit" / "data"
OUT_JSONL = OUT_DIR / "ao_helper_assignments.jsonl"

# Each call-site spec says: which helper to inline, which local var inside the
# helper points to AO ("ao_var"), and how to resolve the family name. The
# family can be a direct literal (`family_literal`) or determined by the value
# of a helper local variable (`family_from_local`) — the latter is needed for
# buildmmlbpmfamily where `Family` is a derived from the `SubMachine` param.
CALL_SITES = [
    {
        "helper": "machine/ALS/Common/buildmmlbpmfamily.m",
        "ao_var": "ao",
        "family_literal": "BPM",                     # SubMachine = 'StorageRing' branch
        "called_from": "alsinit.m:340",
        "call_args": "(AO, 'StorageRing')",
    },
    {
        "helper": "machine/ALS/Common/buildmmlbpmfamily.m",
        "ao_var": "ao",
        "family_literal": "BPMTest",                 # SubMachine = 'SRTest' branch
        "called_from": "alsinit.m:366",
        "call_args": "(AO, 'SRTest')",
    },
    {
        "helper": "machine/ALS/Common/buildmmlcaenfastps.m",
        "ao_var": "ao",
        "family_literal": "HCMFOFB",
        "called_from": "alsinit.m:979",
        "call_args": "('HCMFOFB', AO)",
    },
    {
        "helper": "machine/ALS/Common/buildmmlcaenfastps.m",
        "ao_var": "ao",
        "family_literal": "VCMFOFB",
        "called_from": "alsinit.m:980",
        "call_args": "('VCMFOFB', AO)",
    },
]


def build_lhs_re(ao_var: str) -> re.Pattern:
    """LHS regex parameterized by the AO variable name in the helper.

    Matches paths of the shape:
        ao.<seg>(.<seg>)*(index)?  =  <rhs>
    where each `<seg>` is either a literal identifier or a dynamic-field
    expression `(VarName)` or `(VarName{i})`. The full match groups every
    segment via `path` so we can substitute resolved literals downstream.
    """
    return re.compile(
        rf"""^\s*
            {re.escape(ao_var)}
            (?P<path>(?:
                \.\([^)]+\)
                |
                \.[A-Za-z_]\w*
            )+)
            (?P<index>(?:\([^=]*?\)|\{{[^=]*?\}}))?
            \s*=(?!=)\s*
            (?P<rhs>.*?)
            \s*$
        """,
        re.VERBOSE,
    )


# A single LHS path segment is either literal `.Name` or dynamic `.(expr)`.
PATH_SEGMENT_RE = re.compile(r"\.(?:\(([^)]+)\)|([A-Za-z_]\w*))")


def split_path_segments(path: str) -> list[tuple[str, str]]:
    """Return a list of `(kind, text)` for each segment of an LHS path.

    `kind` is either `"literal"` (segment is a known identifier) or
    `"dynamic"` (a `(VarName)` expression we can't statically resolve
    in the general case).
    """
    out: list[tuple[str, str]] = []
    for m in PATH_SEGMENT_RE.finditer(path):
        dyn, lit = m.group(1), m.group(2)
        if dyn is not None:
            out.append(("dynamic", dyn.strip()))
        else:
            out.append(("literal", lit))
    return out


def find_family_assignment_lines(lines: list[str]) -> list[tuple[int, str]]:
    """Locate every `Family = '<literal>'` line. We don't currently use this
    for branch-aware family resolution — the call-site specs above carry the
    resolved value directly — but the listing is useful when extending the
    extractor to a new helper.
    """
    pat = re.compile(r"^\s*Family\s*=\s*'([^']*)'\s*;?\s*(?:%.*)?$")
    return [(i + 1, m.group(1)) for i, line in enumerate(lines) if (m := pat.match(line))]


def extract_for_call_site(spec: dict) -> list[dict]:
    helper_path = REPO / spec["helper"]
    if not helper_path.exists():
        print(f"warn: missing helper {helper_path}", file=sys.stderr)
        return []

    ao_var = spec["ao_var"]
    family = spec["family_literal"]
    lhs_re = build_lhs_re(ao_var)
    # Cheap prefix gate to skip non-ao lines without invoking the full regex.
    gate_re = re.compile(rf"^\s*{re.escape(ao_var)}\s*\.")

    lines = read_lines(helper_path)
    rel_helper = str(helper_path.relative_to(REPO))
    records: list[dict] = []
    stack: list[dict] = []

    # Stop at the first subfunction. MATLAB files can hold a top-level
    # function plus several subfunctions; in these helpers the subfunctions
    # use their OWN local `ao` (a fresh return struct) which is not the
    # AccObj — capturing them would invent fake families like `AO.Mode`.
    subfn_re = re.compile(r"^\s*function\b")
    end_of_main = len(lines)
    for j in range(1, len(lines)):  # skip the helper's own `function` on line 0
        if subfn_re.match(lines[j]):
            end_of_main = j
            break

    # Sanity log: what `Family = '...'` literals does the helper itself
    # declare? Useful when validating that our resolved family matches one
    # of the helper's branches.
    family_literals_in_helper = [v for _, v in find_family_assignment_lines(lines)]
    if family_literals_in_helper and family not in family_literals_in_helper:
        # Not an error — the family may come purely from a parameter (the
        # FastPS helper). Just print for review.
        print(
            f"  note: family={family!r} is not among {family_literals_in_helper!r} "
            f"declared inside {rel_helper}; trusting call-site spec.",
            file=sys.stderr,
        )

    i = 0
    n = end_of_main
    while i < n:
        raw = lines[i]
        stack = update_context_stack(raw, stack)
        if not gate_re.match(raw):
            i += 1
            continue
        logical, trailing_comment, end_i, phys_count = assemble_logical_line(lines, i)
        m = lhs_re.match(logical)
        if not m:
            # Helper-level oddity (e.g. `ao = funcall(...)`). Record so we
            # can see what we missed; harmless if zero.
            records.append({
                "file": rel_helper,
                "line": i + 1,
                "physical_lines": phys_count,
                "lhs_full": None,
                "rhs_raw": logical,
                "rhs_kind": "unparseable",
                "comment": trailing_comment,
                "context_stack": list(stack),
                "mode_ids": [],
                "via_helper": rel_helper,
                "via_call_site": spec["called_from"],
                "resolved_family": family,
            })
            i = end_i
            continue

        # Walk segments. The first is the family (resolved to the call-site
        # value if it's `(Family)`); the rest form the subpath. Dynamic
        # segments we can't resolve are rendered as `*` so the audit knows
        # there's a sub-struct without inventing its name.
        segments = split_path_segments(m.group("path"))
        if not segments:
            i = end_i
            continue
        kind0, text0 = segments[0]
        if kind0 == "literal":
            family_used = text0
            unresolved_segments_in_family = 0
        else:
            family_used = family   # the call-site-resolved value
            unresolved_segments_in_family = 0  # we DID resolve it

        subpath_parts: list[str] = []
        unresolved_in_subpath = 0
        for kind_s, text_s in segments[1:]:
            if kind_s == "literal":
                subpath_parts.append(text_s)
            else:
                subpath_parts.append("*")
                unresolved_in_subpath += 1

        full_subpath = ".".join(subpath_parts)
        index_expr = m.group("index")
        rhs = (m.group("rhs") or "").rstrip(";").strip()

        records.append({
            "file": rel_helper,
            "line": i + 1,
            "physical_lines": phys_count,
            "root": "AO",
            "lhs_full": f"AO.{family_used}" + (f".{full_subpath}" if full_subpath else ""),
            "family": family_used,
            "subpath": full_subpath,
            "is_indexed_update": index_expr is not None,
            "is_root_rebind": False,
            "unresolved_segments": unresolved_in_subpath,  # 0 = fully resolved
            "rhs_raw": rhs,
            "rhs_kind": classify_rhs(rhs, ao_root_pattern=rf"\b({re.escape(ao_var)}|AO|AD)\."),
            "comment": trailing_comment,
            "context_stack": [
                {"kind": f["kind"], "text": f["text"], "mode_id": f.get("mode_id")}
                for f in stack
            ],
            "mode_ids": [f["mode_id"] for f in stack if f.get("mode_id") is not None],
            "via_helper": rel_helper,
            "via_call_site": spec["called_from"],
            "resolved_family": family,
        })
        i = end_i

    return records


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    all_records: list[dict] = []
    per_site_counts: list[tuple[str, str, int]] = []
    for spec in CALL_SITES:
        recs = extract_for_call_site(spec)
        all_records.extend(recs)
        per_site_counts.append((spec["called_from"], spec["family_literal"], len(recs)))

    with OUT_JSONL.open("w", encoding="utf-8") as f:
        for rec in all_records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"wrote {len(all_records)} helper-assignment records to "
          f"{OUT_JSONL.relative_to(REPO)}")
    for call_site, fam, count in per_site_counts:
        print(f"  {call_site:24s}  family={fam:12s}  +{count} records")
    return 0


if __name__ == "__main__":
    sys.exit(main())
