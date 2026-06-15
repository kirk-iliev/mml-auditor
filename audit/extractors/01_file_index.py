#!/usr/bin/env python3
"""
File index extractor for the MML audit.

Walks a fixed set of roots under the MML checkout, and for every .m file emits:
  - relative path
  - file kind (function | script | class_method | contents | unknown)
  - main function signature (outputs, name, inputs)
  - help text (the contiguous % comment block immediately after the function/file head)
  - one-line summary (first non-empty help line, stripped of leading %FUNCNAME prefix)
  - subfunction count (additional `function` keywords beyond the first)
  - line count and file size in bytes
  - mtime ISO timestamp
  - flags for dynamic-dispatch hints in the file body (feval, eval, str2func)

Output: JSON Lines (one record per line) at audit/data/file_index.jsonl
plus a small summary JSON at audit/data/file_index_summary.json.

Static-only. Pragmatic regex parsing, ~90% coverage. Files that don't fit are
emitted with kind=unknown and what we could extract; nothing is dropped silently.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path("/home/kiliev/Documents/Code/LBL/mmlt")
OUT_DIR = REPO / "audit" / "data"
OUT_JSONL = OUT_DIR / "file_index.jsonl"
OUT_SUMMARY = OUT_DIR / "file_index_summary.json"

ROOTS = [
    REPO / "mml",
    REPO / "machine" / "ALS" / "StorageRing",
    REPO / "machine" / "ALS" / "Booster",
    REPO / "machine" / "ALS" / "BTS",
    REPO / "machine" / "ALS" / "GTB",
    REPO / "machine" / "ALS" / "Common",
]

# Match a function declaration line. MATLAB grammar (simplified):
#   function [out1, out2] = name(in1, in2)
#   function out = name(in1)
#   function name(in1)
#   function name
# We capture: outputs (optional), name, inputs (optional).
#
# The output spec is only present when followed by `=`. Without that
# constraint the regex backtracks and steals characters off the function
# name as a phantom output (e.g. `function aoinit(x)` -> outs=`aoini`,
# name=`t`).
FUNC_RE = re.compile(
    r"""^\s*function\s+
        (?:                                     # optional output spec ...
            (?:
                (?:\[\s*(?P<outs_multi>[^\]]*)\s*\])
                | (?P<outs_one>[A-Za-z_]\w*)
            )
            \s*=\s*                             # ... only if followed by `=`
        )?
        (?P<name>[A-Za-z_]\w*)                  # function name
        \s*
        (?:\(\s*(?P<args>[^)]*)\s*\))?          # optional arg list
        \s*(?:%.*)?$                            # optional trailing comment
    """,
    re.VERBOSE,
)

# A line continuation in MATLAB ends with `...` (possibly followed by comment).
CONT_RE = re.compile(r"\.\.\.\s*(%.*)?$")

# Dynamic-dispatch hints worth flagging in the file body.
DYNAMIC_PATTERNS = {
    "feval": re.compile(r"\bfeval\s*\("),
    "eval":  re.compile(r"\beval\s*\("),
    "str2func": re.compile(r"\bstr2func\s*\("),
}


def read_text(path: Path) -> list[str]:
    """Read a .m file, returning a list of lines (no trailing newline).

    MATLAB files are usually plain ASCII / UTF-8, but a handful of legacy files
    are latin-1. Fall back rather than crashing.
    """
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return path.read_text(encoding="latin-1").splitlines()


def join_continued(lines: list[str], start: int) -> tuple[str, int]:
    """Join a logical line that may span multiple physical lines via `...`.

    Returns (joined_line_without_continuations, next_line_index).
    """
    buf = []
    i = start
    while i < len(lines):
        line = lines[i]
        m = CONT_RE.search(line)
        if m:
            # Strip the `...` and any trailing comment; keep what's before.
            buf.append(line[: m.start()])
            i += 1
            continue
        buf.append(line)
        return " ".join(s.strip() for s in buf), i + 1
    return " ".join(s.strip() for s in buf), i


def parse_signature(joined: str) -> dict | None:
    """Parse a joined function declaration line into outputs/name/args."""
    m = FUNC_RE.match(joined)
    if not m:
        return None
    outs_multi = m.group("outs_multi")
    outs_one = m.group("outs_one")
    if outs_multi is not None:
        outputs = [s.strip() for s in outs_multi.split(",") if s.strip()]
    elif outs_one is not None:
        outputs = [outs_one]
    else:
        outputs = []
    args_raw = m.group("args") or ""
    args = [s.strip() for s in args_raw.split(",") if s.strip()]
    return {
        "name": m.group("name"),
        "outputs": outputs,
        "args": args,
    }


def extract_help_block(lines: list[str], start: int) -> tuple[list[str], int]:
    """Extract the contiguous `%`-comment block starting at `start`.

    A help block may include blank lines that are *inside* the block — MATLAB
    convention is to stop at the first non-blank, non-comment line.
    """
    block = []
    i = start
    saw_comment = False
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("%"):
            block.append(stripped.lstrip("%").rstrip())
            saw_comment = True
            i += 1
        elif stripped == "" and saw_comment:
            # Blank line inside an in-progress help block — keep it, allow continuation.
            block.append("")
            i += 1
        elif stripped == "" and not saw_comment:
            i += 1  # skip leading blanks before any comment
        else:
            break
    # Trim trailing blank lines.
    while block and block[-1] == "":
        block.pop()
    return block, i


def one_line_summary(help_block: list[str], func_name: str | None) -> str:
    """First non-empty help line, with leading `FUNCNAME - ` stripped if present."""
    for line in help_block:
        s = line.strip()
        if not s:
            continue
        # Strip "FUNCNAME -" or "FUNCNAME:" prefix (case-insensitive on name).
        if func_name:
            prefix_re = re.compile(rf"^{re.escape(func_name)}\s*[-:]\s*", re.IGNORECASE)
            s = prefix_re.sub("", s)
        return s
    return ""


def classify(path: Path, sig: dict | None, first_function_line: int | None,
             total_lines: int) -> str:
    name = path.stem
    if name.lower() == "contents":
        return "contents"
    if "@" in path.parts[-2] if len(path.parts) >= 2 else False:
        # Defensive — path.parts always has >= 2 here, but be safe.
        if path.parts[-2].startswith("@"):
            return "class_method"
    if sig is None:
        # No function line at all → script (or unparseable)
        return "script" if total_lines > 0 else "unknown"
    return "function"


def file_record(path: Path) -> dict:
    rel = path.relative_to(REPO)
    try:
        stat = path.stat()
    except OSError as e:
        return {
            "path": str(rel),
            "kind": "unknown",
            "error": f"stat failed: {e}",
        }

    lines = read_text(path)
    total_lines = len(lines)

    # Find the first `function` line (allowing for line continuations).
    sig = None
    help_block: list[str] = []
    first_function_line = None
    subfunction_count = 0
    after_main = 0

    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith("function"):
            first_function_line = i
            joined, next_i = join_continued(lines, i)
            sig = parse_signature(joined)
            help_block, after_main = extract_help_block(lines, next_i)
            i = after_main
            break
        else:
            i += 1

    # Count subfunctions (additional `function` keywords after the main one).
    if first_function_line is not None:
        for j in range(after_main, len(lines)):
            ls = lines[j].lstrip()
            if ls.startswith("function") and not ls.startswith("function_"):
                subfunction_count += 1

    # If no `function` line, treat the leading comment block as the help text.
    if first_function_line is None:
        help_block, _ = extract_help_block(lines, 0)

    # Dynamic-dispatch flags (scan the whole file body, ignoring lines that are
    # purely comments — a false-positive `feval` inside a comment is not a real
    # dispatch site).
    dynamic_hits = {k: 0 for k in DYNAMIC_PATTERNS}
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("%"):
            continue
        # Strip inline comments before matching to avoid catching `% calls feval(...)`.
        code = re.sub(r"(?<!')%.*$", "", line)
        for key, pat in DYNAMIC_PATTERNS.items():
            if pat.search(code):
                dynamic_hits[key] += 1

    kind = classify(path, sig, first_function_line, total_lines)
    name_mismatch = (
        sig is not None
        and kind == "function"
        and sig["name"].lower() != path.stem.lower()
    )

    return {
        "path": str(rel),
        "name": path.stem,
        "kind": kind,
        "size_bytes": stat.st_size,
        "line_count": total_lines,
        "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        "function": sig,                           # None for scripts/unknown
        "summary": one_line_summary(help_block, sig["name"] if sig else None),
        "help_text": "\n".join(help_block),
        "help_line_count": len(help_block),
        "subfunction_count": subfunction_count,
        "dynamic_dispatch": dynamic_hits,          # {feval: N, eval: N, str2func: N}
        "name_matches_file": (not name_mismatch),  # False if function name != filename
        "in_class_folder": (
            len(path.parts) >= 2 and path.parts[-2].startswith("@")
        ),
        "in_legacy_folder": "LegacyFiles" in path.parts,
        # Stable bucket key for summary aggregation: the configured ROOT this
        # file lives under (e.g. "machine/ALS/StorageRing", "mml").
        "root": root_bucket(path),
    }


def root_bucket(path: Path) -> str:
    """Return the configured ROOT (as a posix string) that contains `path`."""
    for root in ROOTS:
        try:
            path.relative_to(root)
            return str(root.relative_to(REPO))
        except ValueError:
            continue
    return "unknown"


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    for root in ROOTS:
        if not root.exists():
            print(f"warn: root missing: {root}", file=sys.stderr)
            continue
        for path in sorted(root.rglob("*.m")):
            if not path.is_file():
                continue
            records.append(file_record(path))

    with OUT_JSONL.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # Summary stats — useful for spot-checking the extractor.
    summary = {
        "total_files": len(records),
        "by_root": {},
        "by_kind": {},
        "with_help": 0,
        "with_dynamic_dispatch": 0,
        "name_mismatches": 0,
        "in_legacy_folder": 0,
        "in_class_folder": 0,
        "no_function_line": 0,
        "parser_failures": 0,  # had `function` line but couldn't parse signature
    }
    for r in records:
        root_key = r["root"]
        summary["by_root"][root_key] = summary["by_root"].get(root_key, 0) + 1
        summary["by_kind"][r["kind"]] = summary["by_kind"].get(r["kind"], 0) + 1
        if r.get("help_line_count", 0) > 0:
            summary["with_help"] += 1
        dh = r.get("dynamic_dispatch") or {}
        if any(dh.values()):
            summary["with_dynamic_dispatch"] += 1
        if not r.get("name_matches_file", True):
            summary["name_mismatches"] += 1
        if r.get("in_legacy_folder"):
            summary["in_legacy_folder"] += 1
        if r.get("in_class_folder"):
            summary["in_class_folder"] += 1
        if r["kind"] in ("script", "unknown") and r.get("function") is None:
            # Distinguish "no function line at all" from "had one but unparseable".
            summary["no_function_line"] += 1
        if r["kind"] == "function" and r.get("function") is None:
            summary["parser_failures"] += 1

    with OUT_SUMMARY.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    print(f"wrote {len(records)} records to {OUT_JSONL.relative_to(REPO)}")
    print(f"summary at {OUT_SUMMARY.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
