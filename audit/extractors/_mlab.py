"""
Shared MATLAB-source-parsing helpers used by the audit extractors.

Pragmatic, string-aware, regex-based. Not a real MATLAB parser; covers ~95%+
of the patterns this codebase actually uses, with the residue surfaced as
explicit `unparseable` / `dynamic_field` markers rather than dropped.
"""
from __future__ import annotations

import re
from pathlib import Path


def read_lines(path: Path) -> list[str]:
    """Read a .m file as a list of lines, with utf-8 → latin-1 fallback."""
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return path.read_text(encoding="latin-1").splitlines()


# ---------------------------------------------------------------------------
# String- and comment-aware character walker.
#
# MATLAB's single quote is overloaded: `'string'` vs `x'` (complex transpose).
# Rule used here:
#   - Not in a string: a `'` opens a string only if the previous non-space
#     char is missing or NOT one of {alnum, ')', ']', '}', '.', '_'}.
#   - In a string: the next `'` always closes it, with the doubled `''`
#     escape representing a literal quote.
# ---------------------------------------------------------------------------


def walk_chars(line: str):
    """Yield (i, c, in_string) for each char in `line`."""
    in_string = False
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if c == "'":
            if in_string:
                if i + 1 < n and line[i + 1] == "'":
                    yield i, c, True
                    yield i + 1, "'", True
                    i += 2
                    continue
                in_string = False
                yield i, c, True
                i += 1
                continue
            j = i - 1
            while j >= 0 and line[j] == " ":
                j -= 1
            prev = line[j] if j >= 0 else ""
            if prev and (prev.isalnum() or prev in ")]}._"):
                yield i, c, False
                i += 1
                continue
            in_string = True
            yield i, c, True
            i += 1
            continue
        yield i, c, in_string
        i += 1


def strip_trailing_comment(line: str) -> tuple[str, str]:
    """Split `(code, comment)` at the first `%` outside a string."""
    for i, c, in_str in walk_chars(line):
        if c == "%" and not in_str:
            return line[:i].rstrip(), line[i + 1 :].strip()
    return line.rstrip(), ""


def count_brackets(code: str) -> tuple[int, int]:
    """Return `(square_balance, curly_balance)` for the given code line."""
    sq = 0
    cu = 0
    for _, c, in_str in walk_chars(code):
        if in_str:
            continue
        if c == "[":
            sq += 1
        elif c == "]":
            sq -= 1
        elif c == "{":
            cu += 1
        elif c == "}":
            cu -= 1
    return sq, cu


CONT_RE = re.compile(r"\.\.\.\s*(%.*)?$")


def assemble_logical_line(lines: list[str], start: int) -> tuple[str, str, int, int]:
    """Join one logical line, spanning `...` continuations and unbalanced
    `[`/`{` literals.

    Returns `(joined_code, leading_comment, end_index_exclusive, physical_line_count)`.
    `leading_comment` is the trailing `% ...` of the first physical line (where
    units typically live).
    """
    parts: list[str] = []
    first_comment = ""
    i = start
    n = len(lines)
    sq_open = 0
    cu_open = 0
    physical_lines = 0
    while i < n:
        raw = lines[i]
        cont_match = CONT_RE.search(raw)
        if cont_match:
            code_part = raw[: cont_match.start()]
            saw_continuation = True
        else:
            code_part = raw
            saw_continuation = False
        code, comment = strip_trailing_comment(code_part)
        if i == start:
            first_comment = comment
        parts.append(code)
        physical_lines += 1
        dsq, dcu = count_brackets(code)
        sq_open += dsq
        cu_open += dcu
        if saw_continuation or sq_open > 0 or cu_open > 0:
            i += 1
            continue
        return " ".join(p.strip() for p in parts if p.strip()), first_comment, i + 1, physical_lines
    return " ".join(p.strip() for p in parts if p.strip()), first_comment, n, physical_lines


# ---------------------------------------------------------------------------
# Conditional context stack — tracks the chain of if/switch/for/while/try
# blocks surrounding each line. Used to attribute assignments to operational
# modes (via `if ModeNumber == N`).
# ---------------------------------------------------------------------------

COND_OPEN_RE = re.compile(
    r"^\s*(?P<kind>if|elseif|else|switch|case|otherwise|for|while|try|catch)\b"
    r"(?P<rest>.*?)\s*(%.*)?$"
)
COND_CLOSE_RE = re.compile(r"^\s*end\b\s*(%.*)?$")
MODE_GUARD_RE = re.compile(r"\bModeNumber\s*==\s*(\d+)")


def update_context_stack(line: str, stack: list[dict]) -> list[dict]:
    """Apply one physical line to the conditional-context stack.

    Frames look like `{kind, text, mode_id}`. `elseif`/`else`/`case`/
    `otherwise` are modeled as sibling replacements of the current top
    frame (since one `end` closes the whole if/switch).
    """
    code, _ = strip_trailing_comment(line)
    if COND_CLOSE_RE.match(code):
        return stack[:-1] if stack else stack
    m = COND_OPEN_RE.match(code)
    if not m:
        return stack
    kind = m.group("kind")
    rest = (m.group("rest") or "").strip()
    mode_id = None
    mm = MODE_GUARD_RE.search(rest)
    if mm:
        mode_id = int(mm.group(1))
    if kind in ("elseif", "else", "case", "otherwise"):
        new_stack = stack[:-1] if stack else []
        return new_stack + [{"kind": kind, "text": rest, "mode_id": mode_id}]
    return stack + [{"kind": kind, "text": rest, "mode_id": mode_id}]


# ---------------------------------------------------------------------------
# RHS classification
# ---------------------------------------------------------------------------


def classify_rhs(rhs: str, ao_root_pattern: str = r"\b(AO|AD)\.") -> str:
    """Coarse shape classification for an assignment's right-hand side."""
    s = rhs.strip().rstrip(";").strip()
    if not s:
        return "empty"
    if re.fullmatch(r"'[^']*'", s):
        return "string_literal"
    if s.startswith("{") and s.endswith("}"):
        return "cell_array"
    if s.startswith("[") and s.endswith("]"):
        return "matrix"
    if re.fullmatch(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?", s):
        return "numeric_literal"
    if re.search(ao_root_pattern, s):
        return "ao_reference"
    if re.fullmatch(r"[A-Za-z_]\w*\s*\(.*\)", s):
        return "function_call"
    return "expression"
