#!/usr/bin/env python3
"""
Paren-less call detector (slice 8 — augments slice 5).

MATLAB allows calling a no-arg function without parens:
    FamilyName = getfamilylist;
    setlabcadefaults;
    showao
Extractor 05 only catches `name(` so it misses these. They're frequent in
MML — many utility functions are no-arg, and the convention is to drop the
parens. Without them, our zero-caller list is inflated and our dead-code
signal is weaker.

This extractor adds a complementary edge stream by finding bare identifier
tokens that:
  (1) are in the callable index (so we know it's defined in-tree),
  (2) are NOT assigned anywhere in the same file as a local variable,
  (3) appear in a position consistent with a statement / RHS reference.

The local-variable filter is the key false-positive guard. A name that
also appears as the LHS of an `=` somewhere in the same file is treated as
a local — even one assignment is enough to disambiguate.

Outputs:
  audit/data/parenless_call_edges.jsonl
  audit/data/parenless_call_summary.json
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

OUT_EDGES = DATA / "parenless_call_edges.jsonl"
OUT_SUMMARY = DATA / "parenless_call_summary.json"

PATH_PRECEDENCE = [
    "mml", "machine/ALS/Common", "machine/ALS/Booster",
    "machine/ALS/BTS", "machine/ALS/GTB", "machine/ALS/StorageRing",
]


def file_root(path_str: str) -> str:
    for root in PATH_PRECEDENCE:
        if path_str.startswith(root + "/"):
            return root
    return "<unknown>"


def precedence(root: str) -> int:
    try:
        return PATH_PRECEDENCE.index(root)
    except ValueError:
        return -1


def load_callable_index() -> tuple[dict, dict]:
    """Same construction as extractor 05: filename stem → list of file recs,
    sorted by path precedence. Class-method files in `@Foo/` excluded —
    OO dispatch is runtime/type-based, not name-based."""
    by_name: dict[str, list[dict]] = defaultdict(list)
    by_path: dict[str, dict] = {}
    with FILE_INDEX.open() as f:
        for line in f:
            r = json.loads(line)
            by_path[r["path"]] = r
            if r.get("in_class_folder"):
                continue
            by_name[r["name"]].append(r)
    for recs in by_name.values():
        recs.sort(key=lambda r: (-precedence(file_root(r["path"])), r["path"]))
    return by_name, by_path


# Built-in MATLAB control keywords + types that show up as bare tokens and
# must never be treated as calls. The callable-index lookup catches most
# false positives on its own, but these are worth excluding explicitly.
MATLAB_KEYWORDS = frozenset({
    "if", "elseif", "else", "end", "for", "while", "switch", "case", "otherwise",
    "break", "continue", "return", "function", "global", "persistent",
    "try", "catch", "classdef", "properties", "methods", "events",
    "spmd", "parfor", "true", "false", "varargin", "varargout",
    "nargin", "nargout", "this", "obj",
})


# Strip comments + strings from a line, preserving columns.
def clean_line(line: str) -> str:
    out = list(line)
    for i, c, in_str in walk_chars(line):
        if in_str:
            out[i] = " "
        elif c == "%":
            for j in range(i, len(out)):
                out[j] = " "
            break
    return "".join(out)


# Identifier — must not be preceded by `.` (struct access) and must not be
# followed by `(` (already caught by extractor 05).
IDENT_RE = re.compile(r"(?<![.\w])([A-Za-z_]\w*)(?!\s*\()")

# Detect LHS-of-assignment patterns to populate the local-variable set.
LHS_PATTERNS = [
    re.compile(r"^\s*([A-Za-z_]\w*)\s*=(?!=)"),                       # `x = ...`
    re.compile(r"^\s*([A-Za-z_]\w*)\s*\("),                            # `x(idx) = ...` — caught as IDENT_RE skips, but x is local
    re.compile(r"^\s*([A-Za-z_]\w*)\s*\{"),                            # `x{i} = ...`
    re.compile(r"^\s*\[([^\]]+)\]\s*=(?!=)"),                          # `[a, b] = ...`
    re.compile(r"^\s*for\s+([A-Za-z_]\w*)\s*="),                       # `for i = ...`
    re.compile(r"^\s*(?:global|persistent)\s+(.+)"),                   # `global x y z`
]
# A `<name>(idx) = rhs` line introduces `<name>` as a local even though our
# bare-identifier regex would also see `<name>` in the body. We catch it via
# the second pattern above, but only when the assignment shape is clear.
INDEXED_LHS_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*[\(\{].+?[\)\}]\s*=(?!=)")


def collect_local_vars(clean_lines: list[str], file_rec: dict) -> set[str]:
    """Identify variables defined in this file's scope.

    Conservative: any identifier ever appearing on the LHS of an assignment
    OR in the function signature's args/outputs OR in a `for VAR = …` head
    OR a `global`/`persistent` declaration is treated as local.
    """
    locals_: set[str] = set()
    # From function signature(s).
    sig = file_rec.get("function") or {}
    locals_.update(sig.get("args", []))
    locals_.update(sig.get("outputs", []))

    for line in clean_lines:
        # Indexed LHS: `x(i) = ...` or `x{i} = ...`
        m = INDEXED_LHS_RE.match(line)
        if m:
            locals_.add(m.group(1))

        m = LHS_PATTERNS[0].match(line)
        if m:
            locals_.add(m.group(1))

        m = LHS_PATTERNS[3].match(line)
        if m:
            for v in m.group(1).split(","):
                v = v.strip()
                if re.fullmatch(r"[A-Za-z_]\w*", v):
                    locals_.add(v)

        m = LHS_PATTERNS[4].match(line)
        if m:
            locals_.add(m.group(1))

        m = LHS_PATTERNS[5].match(line)
        if m:
            for v in m.group(1).split():
                v = v.strip().rstrip(";")
                if re.fullmatch(r"[A-Za-z_]\w*", v):
                    locals_.add(v)

        # Also: subfunctions declared in the same file create additional
        # function args/outputs that are local to those subfunctions. For
        # a paren-less call check we conservatively union them.
        m = re.match(r"^\s*function\b\s*(?:\[([^\]]+)\]|([A-Za-z_]\w*))?\s*=?\s*([A-Za-z_]\w*)\s*\(([^)]*)\)", line)
        if m:
            multi, single, _name, args = m.groups()
            if multi:
                for v in multi.split(","):
                    locals_.add(v.strip())
            if single:
                locals_.add(single)
            if args:
                for v in args.split(","):
                    locals_.add(v.strip())

    return locals_


def extract_parenless(path: Path, callable_index: dict, file_rec: dict) -> list[dict]:
    raw_lines = read_lines(path)
    cleaned = [clean_line(l) for l in raw_lines]
    locals_ = collect_local_vars(cleaned, file_rec)
    self_name = file_rec["name"]
    # Same-file subfunction names — calling those isn't an inter-file edge.
    subfn_names: set[str] = set()
    for line in cleaned:
        m = re.match(r"^\s*function\b.*?\b([A-Za-z_]\w*)\s*[\(\s]", line)
        if m:
            subfn_names.add(m.group(1))

    edges: list[dict] = []
    for lineno, line in enumerate(cleaned, start=1):
        # Don't scan the function-declaration head itself.
        if re.match(r"^\s*function\b", line):
            continue
        for m in IDENT_RE.finditer(line):
            name = m.group(1)
            if name in MATLAB_KEYWORDS:
                continue
            if name in locals_:
                continue
            if name == self_name or name in subfn_names:
                continue
            candidates = callable_index.get(name)
            if not candidates:
                continue
            primary = candidates[0]
            edges.append({
                "caller": file_rec["path"],
                "caller_line": lineno,
                "callee_name": name,
                "callee_path": primary["path"],
                "resolved": True,
                "intra_file": False,
                "candidates_count": len(candidates),
                "shadowed": len(candidates) > 1,
                "style": "parenless",
            })
    return edges


def main() -> int:
    callable_index, file_by_path = load_callable_index()

    edges: list[dict] = []
    for path_str, rec in file_by_path.items():
        edges.extend(extract_parenless(REPO / path_str, callable_index, rec))

    with OUT_EDGES.open("w", encoding="utf-8") as f:
        for e in edges:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    # Summary
    by_caller_out: dict[str, int] = defaultdict(int)
    by_callee_in: dict[str, int] = defaultdict(int)
    by_callee_name: dict[str, int] = defaultdict(int)
    for e in edges:
        by_caller_out[e["caller"]] += 1
        by_callee_in[e["callee_path"]] += 1
        by_callee_name[e["callee_name"]] += 1

    summary = {
        "total_parenless_edges": len(edges),
        "distinct_callers": len(by_caller_out),
        "distinct_callees": len(by_callee_in),
        "top_parenless_callees": [
            {"name": n, "calls": c}
            for n, c in sorted(by_callee_name.items(), key=lambda kv: -kv[1])[:25]
        ],
        "top_parenless_callers_by_fanout": [
            {"path": p, "out": c}
            for p, c in sorted(by_caller_out.items(), key=lambda kv: -kv[1])[:15]
        ],
    }
    with OUT_SUMMARY.open("w") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    print(f"wrote {len(edges)} paren-less edges to {OUT_EDGES.relative_to(REPO)}")
    print(f"  distinct callers: {len(by_caller_out)}")
    print(f"  distinct callees: {len(by_callee_in)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
