#!/usr/bin/env python3
"""
Cross-reference extractor (slice 7 of the audit, layer B).

Joins the file index with the AO/AD schema by scanning every in-scope `.m`
file for references to known families and AD paths. Two reference kinds:

  1. STRING MENTION  — the family name appears as a string literal:
        getsp('BPMx', ...)
        getfamilydata('BPM', 'Status')
        if strcmpi(Family, 'HCM')
     This is how most MML verbs take a family argument.

  2. STRUCT ACCESS   — the family name appears as a struct field:
        AO.BPMx.Monitor
        ao.(Family).BaseName       (we already handled the dynamic case)
        getao.BPMx.Status
     This indicates code that pokes into the AO directly.

For AD paths we look for `AD.<Path>` (struct access) and `'<Path>'`
mentions inside common AD-reading verbs (`getfamilydata`, etc.). String
mentions of AD path names are too ambiguous to record blindly (e.g.
'Energy' as a string could be many things), so we conservatively limit
AD mentions to direct `AD.<Path>` access — fewer false positives.

Outputs:
  audit/data/family_refs.jsonl   one record per (file, family) pair
  audit/data/ad_refs.jsonl       one record per (file, AD path) pair
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
FAMILY_SCHEMA = DATA / "family_schema.json"
AD_SCHEMA = DATA / "ad_schema.json"
OUT_FAMILY = DATA / "family_refs.jsonl"
OUT_AD = DATA / "ad_refs.jsonl"


def load_families() -> list[str]:
    with FAMILY_SCHEMA.open() as f:
        return list(json.load(f).keys())


def load_ad_paths() -> list[str]:
    with AD_SCHEMA.open() as f:
        return list(json.load(f).keys())


def iter_file_paths() -> list[str]:
    with FILE_INDEX.open() as f:
        return [json.loads(line)["path"] for line in f if line.strip()]


def split_strings_and_code(line: str) -> tuple[list[tuple[int, str]], str]:
    """Return (list of (col, string_contents), code-only line with strings blanked).

    Comments are stripped from the code-only output. Strings are returned
    separately so we can scan them for family-name mentions without also
    matching the same name in struct-access syntax.
    """
    strings: list[tuple[int, str]] = []
    code_chars = list(line)
    in_string_chars: list[int] = []
    in_string = False
    string_start = -1
    for i, c, in_str in walk_chars(line):
        if c == "%" and not in_str:
            for j in range(i, len(code_chars)):
                code_chars[j] = " "
            break
        if in_str:
            code_chars[i] = " "
            if not in_string:
                in_string = True
                string_start = i + 1   # skip opening quote
            in_string_chars.append(i)
        else:
            if in_string:
                # We just exited a string — collect it.
                content = line[string_start : in_string_chars[-1]]  # excludes closing quote
                strings.append((string_start, content))
                in_string = False
                in_string_chars = []
    return strings, "".join(code_chars)


def find_references_in_file(
    path: Path,
    families: list[str],
    ad_paths: list[str],
) -> tuple[dict[str, dict], dict[str, dict]]:
    """Scan one file. Return:
      family_hits: {family: {string: int, struct: int, sample_lines: [(lineno, text)]}}
      ad_hits:     {ad_path: {count: int, sample_lines: [...]}}
    """
    # Precompile regexes for struct access of each family/AD path. Using a
    # single alternation per kind keeps the per-line cost bounded.
    fam_alt = "|".join(re.escape(f) for f in families)
    struct_re = re.compile(rf"\.({fam_alt})(?=[.\s({{\[;,)])")
    ad_alt = "|".join(re.escape(p.split(".")[0]) for p in ad_paths)
    # AD path access. We match `AD.<Root>` and the schema entries that nest
    # deeper (e.g. `AD.Directory.BPMData`) are recorded as a hit on the full
    # path when seen in full. For now we record the FULL declared paths only.
    full_ad_re = re.compile(
        r"\bAD\.(?P<path>(?:[A-Za-z_]\w*)(?:\.[A-Za-z_]\w*)*)"
    )

    fam_hits: dict[str, dict] = {}
    ad_hits: dict[str, dict] = {}
    fam_set = set(families)
    ad_path_set = set(ad_paths)

    raw_lines = read_lines(path)
    for lineno, line in enumerate(raw_lines, start=1):
        strings, code = split_strings_and_code(line)

        # 1) String mentions of family names. We require the entire string
        #    to be the family name (so 'BPMxStatus' doesn't match BPMx); the
        #    MML convention passes families as bare strings.
        for _, s in strings:
            if s in fam_set:
                entry = fam_hits.setdefault(s, {"string": 0, "struct": 0, "sample_lines": []})
                entry["string"] += 1
                if len(entry["sample_lines"]) < 3:
                    entry["sample_lines"].append([lineno, line.strip()[:200]])

        # 2) Struct-access mentions of family names: `.BPMx` followed by a
        #    delimiter that's not a word char. The code-only line has strings
        #    blanked so `'BPMx'` won't match here even with a stray `.`.
        for m in struct_re.finditer(code):
            fam = m.group(1)
            entry = fam_hits.setdefault(fam, {"string": 0, "struct": 0, "sample_lines": []})
            entry["struct"] += 1
            if len(entry["sample_lines"]) < 3:
                entry["sample_lines"].append([lineno, line.strip()[:200]])

        # 3) AD direct access — record any AD.<path> that matches a declared
        #    schema entry (full match) OR its prefix root.
        for m in full_ad_re.finditer(code):
            seen = m.group("path")
            # Prefer the longest declared path that prefixes the seen path.
            matched = None
            for ad in ad_paths:
                if seen == ad or seen.startswith(ad + "."):
                    if matched is None or len(ad) > len(matched):
                        matched = ad
            if matched is None:
                # Unknown AD path — still useful audit signal, store as is.
                matched = seen
            entry = ad_hits.setdefault(matched, {"count": 0, "sample_lines": []})
            entry["count"] += 1
            if len(entry["sample_lines"]) < 3:
                entry["sample_lines"].append([lineno, line.strip()[:200]])

    return fam_hits, ad_hits


def main() -> int:
    families = load_families()
    ad_paths = load_ad_paths()
    file_paths = iter_file_paths()

    family_records: list[dict] = []
    ad_records: list[dict] = []

    for fp in file_paths:
        abs_path = REPO / fp
        fam_hits, ad_hits = find_references_in_file(abs_path, families, ad_paths)
        for fam, info in fam_hits.items():
            family_records.append({
                "file": fp,
                "family": fam,
                "string_refs": info["string"],
                "struct_refs": info["struct"],
                "total_refs": info["string"] + info["struct"],
                "sample_lines": info["sample_lines"],
            })
        for ad_path, info in ad_hits.items():
            ad_records.append({
                "file": fp,
                "ad_path": ad_path,
                "refs": info["count"],
                "sample_lines": info["sample_lines"],
            })

    with OUT_FAMILY.open("w", encoding="utf-8") as f:
        for r in family_records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with OUT_AD.open("w", encoding="utf-8") as f:
        for r in ad_records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # Brief stdout summary so the run is self-describing.
    per_family = defaultdict(int)
    for r in family_records:
        per_family[r["family"]] += 1
    print(f"wrote {len(family_records)} (file, family) reference records to "
          f"{OUT_FAMILY.relative_to(REPO)}")
    print(f"wrote {len(ad_records)} (file, AD path) reference records to "
          f"{OUT_AD.relative_to(REPO)}")
    top = sorted(per_family.items(), key=lambda kv: -kv[1])[:8]
    print("  top families by file count:")
    for fam, n in top:
        print(f"    {n:4d}  {fam}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
