#!/usr/bin/env python3
"""
Build audit/data/source_bundle.json.gz — the standalone source corpus the MCP
server reads instead of walking the 136 MB on-disk .m tree.

This is the SAME selection 06_build_browser.py embeds into browser.html: every
file in file_index (the ALS-relevant ~2,424 records) EXCEPT archival ones
(_Attic/Old/legacy). The result is a gzipped JSON dict {repo_relative_path: text}
of ~1,998 live ALS source files, ~3-4 MB compressed.

Why a separate file (vs. reusing the browser's base64 blob): the server is
stdlib-only and reads a plain gzip file directly (no base64, no HTML parsing).
gzip + json are both standard library, so the server stays zero-dependency.

Re-run whenever the source tree changes:
    python3 audit/extractors/12_source_bundle.py
"""

import gzip
import json
from pathlib import Path

REPO = Path("/home/kiliev/Documents/Code/LBL/mmlt")
DATA = REPO / "audit" / "data"
ARCHIVE_DIR_NAMES = {"Old", "old", "_Attic", "Attic"}


def load_jsonl(name):
    out = []
    with open(DATA / name, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def main():
    file_index = load_jsonl("file_index.jsonl")
    source = {}
    archived = 0
    missing = 0
    for rec in file_index:
        path = rec["path"]
        is_archive = rec["in_legacy_folder"] or any(
            p in ARCHIVE_DIR_NAMES for p in Path(path).parts
        )
        if is_archive:
            archived += 1
            continue
        fp = REPO / path
        if not fp.is_file():
            missing += 1
            continue
        try:
            source[path] = fp.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            source[path] = fp.read_text(encoding="latin-1")

    out = DATA / "source_bundle.json.gz"
    raw = json.dumps(source, ensure_ascii=False, separators=(",", ":"))
    with gzip.open(out, "wt", encoding="utf-8") as fh:
        fh.write(raw)

    raw_mb = len(raw.encode("utf-8")) / 1024 / 1024
    gz_mb = out.stat().st_size / 1024 / 1024
    print(f"source bundle: {len(source)} files, "
          f"{raw_mb:.1f} MB raw -> {gz_mb:.1f} MB gz")
    print(f"  skipped {archived} archival, {missing} missing-on-disk")
    print(f"  wrote {out.relative_to(REPO)}")


if __name__ == "__main__":
    main()
