"""Unified diff parser, in-memory patch applier, file classifier, and
Python AST change analyzer. Standard library only.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from dataclasses import dataclass, field
from typing import Any

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_DIFF_GIT_RE = re.compile(r"^diff --git (?:\"a/(.+?)\"|a/(\S+)) (?:\"b/(.+?)\"|b/(\S+))$")


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[str]  # raw hunk lines starting with ' ', '-', '+', or '\'
    changed_old_lines: list[int] = field(default_factory=list)
    added_only: bool = False


@dataclass
class FilePatch:
    old_path: str | None
    new_path: str | None
    hunks: list[Hunk] = field(default_factory=list)
    is_binary: bool = False
    is_new: bool = False
    is_deleted: bool = False
    is_rename: bool = False

    @property
    def path(self) -> str:
        """Primary path for classification and lookup (prefer new_path unless deleted)."""
        if self.new_path and self.new_path != "/dev/null":
            return self.new_path
        return self.old_path or ""

    @property
    def base_path(self) -> str:
        """Base-commit path for looking up base source / coverage."""
        if self.old_path and self.old_path != "/dev/null":
            return self.old_path
        return self.new_path or ""

    @property
    def all_changed_old_lines(self) -> list[int]:
        out: list[int] = []
        for h in self.hunks:
            out.extend(h.changed_old_lines)
        return out


@dataclass
class ParsedPatch:
    files: list[FilePatch] = field(default_factory=list)
    parse_error: bool = False
    error_msg: str = ""
    is_empty: bool = False


def _clean_diff_path(raw: str) -> str | None:
    s = raw.strip()
    if "\t" in s:
        s = s.split("\t", 1)[0]
    if s.startswith('"') and s.endswith('"'):
        # Unescape C-style git quoted paths
        try:
            s = bytes(s[1:-1], "utf-8").decode("unicode_escape")
        except Exception:
            s = s[1:-1]
    if s == "/dev/null":
        return None
    if s.startswith("a/") or s.startswith("b/"):
        s = s[2:]
    return s


def parse_unified_diff(text: str) -> ParsedPatch:
    """Parse a git unified diff into FilePatch objects with exact old-file changed lines."""
    if not text or not text.strip():
        return ParsedPatch(files=[], is_empty=True)

    lines = text.splitlines()
    files: list[FilePatch] = []
    cur_file: FilePatch | None = None
    cur_hunk: Hunk | None = None
    saw_any_diff_header = False

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]

        # Start of a new git file section
        if line.startswith("diff --git "):
            saw_any_diff_header = True
            cur_hunk = None
            m = _DIFF_GIT_RE.match(line)
            old_p = (m.group(1) or m.group(2)) if m else None
            new_p = (m.group(3) or m.group(4)) if m else None
            cur_file = FilePatch(old_path=old_p, new_path=new_p)
            files.append(cur_file)
            i += 1
            continue

        # Handle non-git unified diffs starting directly with --- / +++
        if line.startswith("--- ") and (cur_file is None or cur_hunk is not None):
            if i + 1 < n and lines[i + 1].startswith("+++ "):
                saw_any_diff_header = True
                cur_hunk = None
                old_p = _clean_diff_path(line[4:])
                new_p = _clean_diff_path(lines[i + 1][4:])
                cur_file = FilePatch(
                    old_path=old_p,
                    new_path=new_p,
                    is_new=(old_p is None),
                    is_deleted=(new_p is None),
                )
                files.append(cur_file)
                i += 2
                continue

        if cur_file is None:
            # Skip preamble lines (e.g., git format-patch headers) until first diff
            i += 1
            continue

        # File metadata headers (outside hunks)
        if cur_hunk is None:
            if line.startswith("new file mode"):
                cur_file.is_new = True
                i += 1
                continue
            if line.startswith("deleted file mode"):
                cur_file.is_deleted = True
                i += 1
                continue
            if line.startswith("rename from "):
                cur_file.is_rename = True
                cur_file.old_path = _clean_diff_path(line[len("rename from ") :])
                i += 1
                continue
            if line.startswith("rename to "):
                cur_file.is_rename = True
                cur_file.new_path = _clean_diff_path(line[len("rename to ") :])
                i += 1
                continue
            if line.startswith("Binary files ") or line.startswith("GIT binary patch"):
                cur_file.is_binary = True
                i += 1
                continue
            if line.startswith("--- "):
                old_p = _clean_diff_path(line[4:])
                if old_p is None:
                    cur_file.is_new = True
                else:
                    cur_file.old_path = old_p
                i += 1
                continue
            if line.startswith("+++ "):
                new_p = _clean_diff_path(line[4:])
                if new_p is None:
                    cur_file.is_deleted = True
                else:
                    cur_file.new_path = new_p
                i += 1
                continue

        # Hunk header
        if line.startswith("@@ "):
            m = _HUNK_RE.match(line)
            if not m:
                return ParsedPatch(
                    files=files,
                    parse_error=True,
                    error_msg=f"Malformed hunk header: {line!r}",
                )
            old_start = int(m.group(1))
            old_count = int(m.group(2)) if m.group(2) is not None else 1
            new_start = int(m.group(3))
            new_count = int(m.group(4)) if m.group(4) is not None else 1
            cur_hunk = Hunk(
                old_start=old_start,
                old_count=old_count,
                new_start=new_start,
                new_count=new_count,
                lines=[],
            )
            cur_file.hunks.append(cur_hunk)
            i += 1
            continue

        # Inside a hunk
        if cur_hunk is not None:
            if line.startswith((" ", "-", "+", "\\")):
                cur_hunk.lines.append(line)
                i += 1
                continue
            elif line == "":
                # Some diffs drop the leading space on blank context lines
                cur_hunk.lines.append(" ")
                i += 1
                continue
            else:
                # End of hunk (e.g., trailing git signature `-- `)
                cur_hunk = None
                i += 1
                continue

        i += 1

    if not saw_any_diff_header or not files:
        return ParsedPatch(
            files=[],
            parse_error=True,
            error_msg="No valid unified diff file headers found",
        )

    # Compute exact changed_old_lines for every hunk
    for fp in files:
        for h in fp.hunks:
            _compute_hunk_changed_lines(h)

    return ParsedPatch(files=files)


def _compute_hunk_changed_lines(h: Hunk) -> None:
    """Compute exact old-file line numbers touched by deletions/modifications,
    or anchor old-file lines adjacent to pure insertions."""
    old_ln = h.old_start
    deleted_lines: list[int] = []
    insertion_anchors: list[int] = []
    saw_minus = False
    saw_plus = False

    idx = 0
    n = len(h.lines)
    while idx < n:
        raw = h.lines[idx]
        if raw.startswith("\\"):
            idx += 1
            continue
        if raw.startswith(" "):
            old_ln += 1
            idx += 1
        elif raw.startswith("-"):
            saw_minus = True
            deleted_lines.append(old_ln)
            old_ln += 1
            idx += 1
        elif raw.startswith("+"):
            saw_plus = True
            # Check if this + block is pure insertion (not adjacent to -)
            prev_was_minus = idx > 0 and h.lines[idx - 1].startswith("-")
            while idx < n and h.lines[idx].startswith(("+", "\\")):
                idx += 1
            next_is_minus = idx < n and h.lines[idx].startswith("-")
            if not prev_was_minus and not next_is_minus:
                # Pure insertion right before `old_ln` (after `old_ln - 1`)
                if old_ln > 1:
                    insertion_anchors.append(old_ln - 1)
                if old_ln >= 1:
                    insertion_anchors.append(old_ln)
        else:
            idx += 1

    h.added_only = saw_plus and not saw_minus
    combined = sorted(set(deleted_lines) | set(insertion_anchors))
    if not combined and h.old_count > 0:
        combined = [h.old_start]
    h.changed_old_lines = combined


# ---------------------------------------------------------------------------
# In-memory patch application
# ---------------------------------------------------------------------------
def apply_hunks(base_bytes: bytes, hunks: list[Hunk]) -> bytes | None:
    """Apply parsed hunks to base_bytes in memory. Returns None if context fails."""
    if not hunks:
        return base_bytes

    # Preserve line ending style
    crlf = b"\r\n" in base_bytes and b"\r\n" == base_bytes[base_bytes.find(b"\n") - 1 : base_bytes.find(b"\n") + 1]
    eol = "\r\n" if crlf else "\n"
    try:
        base_text = base_bytes.decode("utf-8")
    except UnicodeDecodeError:
        try:
            enc, _ = tokenize.detect_encoding(io.BytesIO(base_bytes).readline)
            base_text = base_bytes.decode(enc)
        except Exception:
            return None

    had_trailing_nl = base_text.endswith("\n")
    old_lines = base_text.splitlines()
    new_lines: list[str] = []
    cursor = 0  # 0-based index into old_lines
    new_trailing_nl = had_trailing_nl

    for h in hunks:
        # 1-based old_start -> 0-based index (note: old_start==0 for empty base file)
        target_idx = max(0, h.old_start - 1)
        if target_idx < cursor or target_idx > len(old_lines):
            return None
        new_lines.extend(old_lines[cursor:target_idx])
        cursor = target_idx

        j = 0
        hn = len(h.lines)
        while j < hn:
            hl = h.lines[j]
            if hl.startswith("\\"):
                j += 1
                continue
            tag = hl[0]
            content = hl[1:]
            if tag == " ":
                if cursor >= len(old_lines) or old_lines[cursor] != content:
                    return None
                new_lines.append(old_lines[cursor])
                cursor += 1
            elif tag == "-":
                if cursor >= len(old_lines) or old_lines[cursor] != content:
                    return None
                cursor += 1
            elif tag == "+":
                new_lines.append(content)
                if j + 1 < hn and h.lines[j + 1].startswith("\\"):
                    new_trailing_nl = False
            j += 1

    new_lines.extend(old_lines[cursor:])
    out_text = eol.join(new_lines)
    if new_trailing_nl and (new_lines or had_trailing_nl):
        out_text += eol
    try:
        return out_text.encode("utf-8")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# File classification
# ---------------------------------------------------------------------------
_DOC_EXTENSIONS = (".md", ".rst", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".pdf")
_DOC_FILENAMES = {
    "LICENSE",
    "CHANGELOG",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "README",
    "README.md",
    "AGENTS.md",
    "CLAUDE.md",
    ".gitignore",
    ".gitpod.yml",
}
_CONFIG_FILENAMES = {
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "MANIFEST.in",
    "Makefile",
    "pytest.ini",
    "tox.ini",
    "conftest.py",
    ".pre-commit-config.yaml",
    "requirements.txt",
    "requirements.lock",
}
_RUST_OR_C_EXTENSIONS = (".rs", ".c", ".h", ".cc", ".cpp", ".pyx", ".pxd")


def classify_file(rel_path: str) -> str:
    """Classify a repo-relative path into a principled category."""
    p = rel_path.lstrip("./")
    if not p:
        return "other"

    parts = p.split("/")
    name = parts[-1]

    # 1. Fixtures
    if p.startswith("tests/fixtures/"):
        return "fixture"

    # 2. Config / build / conftest / sqlglotc / CI
    if name in _CONFIG_FILENAMES or name.startswith("requirements"):
        return "config"
    if p.startswith("sqlglotc/"):
        return "config"
    if p.startswith(".github/"):
        if "ISSUE_TEMPLATE" in p or name.endswith(".md"):
            return "docs"
        return "config"

    # 3. Rust / native tokenizer / C extensions
    if p.startswith("sqlglotrs/") or p == "sqlglot/tokenizer_core.py" or name in ("Cargo.toml", "Cargo.lock"):
        return "rust_or_c"
    if any(name.endswith(ext) for ext in _RUST_OR_C_EXTENSIONS):
        return "rust_or_c"

    # 4. Tests & test helpers
    if p.startswith("tests/"):
        if name.startswith("test_") and name.endswith(".py"):
            return "test"
        if name.endswith(".py"):
            return "test_helper"
        return "fixture"

    # 5. sqlglot Python source & __init__.py
    if p.startswith("sqlglot/"):
        if name == "__init__.py":
            return "root_init" if p == "sqlglot/__init__.py" else "pkg_init"
        if name.endswith(".py"):
            return "source"
        if name == "py.typed":
            return "docs"
        return "other"

    # 6. Docs & standalone non-runtime directories
    if p.startswith(("pdoc/", "posts/", "benchmarks/", "docs/")):
        return "docs"
    if name in _DOC_FILENAMES or any(name.lower().endswith(ext) for ext in _DOC_EXTENSIONS):
        return "docs"

    return "other"


# ---------------------------------------------------------------------------
# Python AST change analysis
# ---------------------------------------------------------------------------
@dataclass
class PythonChangeAnalysis:
    syntax_error: bool = False
    ast_identical: bool = False
    changed_funcs: list[str] = field(default_factory=list)
    changed_classes: list[str] = field(default_factory=list)
    changed_class_headers: list[str] = field(default_factory=list)
    # (class_qualname, attr_name, changed_dict_keys_or_None)
    changed_class_attrs: list[tuple[str, str, list[str] | None]] = field(default_factory=list)
    # (var_name, changed_dict_keys_or_None)
    changed_module_vars: list[tuple[str, list[str] | None]] = field(default_factory=list)
    added_or_removed_public_names: list[str] = field(default_factory=list)
    changed_imports: bool = False
    changed_all: bool = False
    changed_other_module_code: bool = False


def _dump_node(node: ast.AST) -> str:
    return ast.dump(node, include_attributes=False)


def _dict_key_repr(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    if isinstance(node, ast.Constant):
        return repr(node.value) if not isinstance(node.value, str) else node.value
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return _dump_node(node)


def _diff_dict_exprs(old_val: ast.AST | None, new_val: ast.AST | None) -> list[str] | None:
    """If both old and new values are ast.Dict literals with identical spreads,
    return the list of changed/added/removed key names. Otherwise return None."""
    if not isinstance(old_val, ast.Dict) or not isinstance(new_val, ast.Dict):
        return None

    old_spreads = [_dump_node(v) for k, v in zip(old_val.keys, old_val.values) if k is None]
    new_spreads = [_dump_node(v) for k, v in zip(new_val.keys, new_val.values) if k is None]
    if old_spreads != new_spreads:
        return None

    old_items: dict[str, str] = {}
    for k, v in zip(old_val.keys, old_val.values):
        if k is None:
            continue
        kr = _dict_key_repr(k)
        if kr is None:
            return None
        old_items[kr] = _dump_node(v)

    new_items: dict[str, str] = {}
    for k, v in zip(new_val.keys, new_val.values):
        if k is None:
            continue
        kr = _dict_key_repr(k)
        if kr is None:
            return None
        new_items[kr] = _dump_node(v)

    changed_keys: list[str] = []
    for k in set(old_items) | set(new_items):
        if old_items.get(k) != new_items.get(k):
            changed_keys.append(k)
    return sorted(changed_keys)


def _stmt_key(stmt: ast.stmt) -> tuple[str, str] | None:
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return ("func", stmt.name)
    if isinstance(stmt, ast.ClassDef):
        return ("class", stmt.name)
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
        return ("var", stmt.targets[0].id)
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
        return ("var", stmt.target.id)
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        return ("import", _dump_node(stmt))
    return None


def _assign_value(stmt: ast.stmt) -> ast.AST | None:
    if isinstance(stmt, ast.Assign):
        return stmt.value
    if isinstance(stmt, ast.AnnAssign):
        return stmt.value
    return None


def _diff_class(
    old_cls: ast.ClassDef,
    new_cls: ast.ClassDef,
    prefix: str,
    result: PythonChangeAnalysis,
) -> None:
    qname = f"{prefix}.{old_cls.name}" if prefix else old_cls.name
    result.changed_classes.append(qname)

    # Check bases, keywords, decorators
    if (
        [_dump_node(b) for b in old_cls.bases] != [_dump_node(b) for b in new_cls.bases]
        or [_dump_node(k) for k in old_cls.keywords] != [_dump_node(k) for k in new_cls.keywords]
        or [_dump_node(d) for d in old_cls.decorator_list] != [_dump_node(d) for d in new_cls.decorator_list]
    ):
        result.changed_class_headers.append(qname)

    old_map: dict[tuple[str, str], list[ast.stmt]] = {}
    new_map: dict[tuple[str, str], list[ast.stmt]] = {}
    old_other: list[str] = []
    new_other: list[str] = []

    for s in old_cls.body:
        k = _stmt_key(s)
        if k is None:
            old_other.append(_dump_node(s))
        else:
            old_map.setdefault(k, []).append(s)
    for s in new_cls.body:
        k = _stmt_key(s)
        if k is None:
            new_other.append(_dump_node(s))
        else:
            new_map.setdefault(k, []).append(s)

    if old_other != new_other:
        result.changed_class_headers.append(qname)

    for k in set(old_map) | set(new_map):
        o_list = old_map.get(k, [])
        n_list = new_map.get(k, [])
        if [_dump_node(x) for x in o_list] == [_dump_node(x) for x in n_list]:
            continue
        kind, name = k
        if kind == "func":
            fq = f"{qname}.{name}"
            result.changed_funcs.append(fq)
            # If method added/removed or decorator/signature changed, also flag class attr
            if len(o_list) != len(n_list):
                result.changed_class_attrs.append((qname, name, None))
            elif len(o_list) == 1:
                of, nf = o_list[0], n_list[0]
                if isinstance(of, (ast.FunctionDef, ast.AsyncFunctionDef)) and isinstance(
                    nf, (ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    if [_dump_node(d) for d in of.decorator_list] != [_dump_node(d) for d in nf.decorator_list]:
                        result.changed_class_attrs.append((qname, name, None))
        elif kind == "class":
            if len(o_list) == 1 and len(n_list) == 1:
                _diff_class(o_list[0], n_list[0], qname, result)  # type: ignore[arg-type]
            else:
                result.changed_class_headers.append(f"{qname}.{name}")
        elif kind == "var":
            dkeys = None
            if len(o_list) == 1 and len(n_list) == 1:
                dkeys = _diff_dict_exprs(_assign_value(o_list[0]), _assign_value(n_list[0]))
            result.changed_class_attrs.append((qname, name, dkeys))
        else:
            result.changed_class_headers.append(qname)


def analyze_python_change(old_bytes: bytes, new_bytes: bytes) -> PythonChangeAnalysis:
    """Compare old and new Python file bytes using tokenize + AST."""
    res = PythonChangeAnalysis()

    # 1. Verify encoding of both old and new files (catches PEP 263 coding cookie issues)
    try:
        old_enc, _ = tokenize.detect_encoding(io.BytesIO(old_bytes).readline)
    except Exception:
        old_enc = "utf-8"
    try:
        new_enc, _ = tokenize.detect_encoding(io.BytesIO(new_bytes).readline)
        new_bytes.decode(new_enc)
    except Exception:
        res.syntax_error = True
        return res
    if old_enc != new_enc:
        res.syntax_error = True
        return res

    # 2. Parse both ASTs
    try:
        old_tree = ast.parse(old_bytes)
    except Exception:
        res.syntax_error = True
        return res
    try:
        new_tree = ast.parse(new_bytes)
    except Exception:
        res.syntax_error = True
        return res

    # 3. Exact AST equivalence check (ignores pure comments & harmless whitespace,
    #    but catches string literals, docstrings, indentation changes, syntax errors)
    if _dump_node(old_tree) == _dump_node(new_tree):
        res.ast_identical = True
        return res

    # 4. Structural diff of top-level statements
    old_map: dict[tuple[str, str], list[ast.stmt]] = {}
    new_map: dict[tuple[str, str], list[ast.stmt]] = {}
    old_imports: set[str] = set()
    new_imports: set[str] = set()
    old_other: list[str] = []
    new_other: list[str] = []
    old_public: set[str] = set()
    new_public: set[str] = set()

    for s in old_tree.body:
        k = _stmt_key(s)
        if k is None:
            old_other.append(_dump_node(s))
        elif k[0] == "import":
            old_imports.add(k[1])
        else:
            old_map.setdefault(k, []).append(s)
            if not k[1].startswith("_"):
                old_public.add(k[1])

    for s in new_tree.body:
        k = _stmt_key(s)
        if k is None:
            new_other.append(_dump_node(s))
        elif k[0] == "import":
            new_imports.add(k[1])
        else:
            new_map.setdefault(k, []).append(s)
            if not k[1].startswith("_"):
                new_public.add(k[1])

    if old_imports != new_imports:
        res.changed_imports = True
    if old_other != new_other:
        res.changed_other_module_code = True

    diff_public = sorted(old_public ^ new_public)
    if diff_public:
        res.added_or_removed_public_names = diff_public

    for k in set(old_map) | set(new_map):
        o_list = old_map.get(k, [])
        n_list = new_map.get(k, [])
        if [_dump_node(x) for x in o_list] == [_dump_node(x) for x in n_list]:
            continue
        kind, name = k
        if kind == "func":
            res.changed_funcs.append(name)
            if len(o_list) == 1 and len(n_list) == 1:
                of, nf = o_list[0], n_list[0]
                if isinstance(of, (ast.FunctionDef, ast.AsyncFunctionDef)) and isinstance(
                    nf, (ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    if [_dump_node(d) for d in of.decorator_list] != [_dump_node(d) for d in nf.decorator_list]:
                        res.changed_module_vars.append((name, None))
        elif kind == "class":
            if len(o_list) == 1 and len(n_list) == 1:
                _diff_class(o_list[0], n_list[0], "", res)  # type: ignore[arg-type]
            else:
                res.changed_class_headers.append(name)
        elif kind == "var":
            if name == "__all__":
                res.changed_all = True
            dkeys = None
            if len(o_list) == 1 and len(n_list) == 1:
                dkeys = _diff_dict_exprs(_assign_value(o_list[0]), _assign_value(n_list[0]))
            res.changed_module_vars.append((name, dkeys))

    return res
