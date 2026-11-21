#!/usr/bin/env python3
"""Differential test: synthetic edit generator comparing Hybrid Verifier drop
decisions against real pytest (SKIP_INTEGRATION=0, pytest 9 with subtests).

Generates synthetic patches across 9+ edit categories:
  1. constant (module-level constant)
  2. dict_value (class dict literal value)
  3. condition_common (boolean/comparison condition in covered function)
  4. condition_rare_threshold (rarely-triggered boundary condition — shadow stress test!)
  5. condition_equiv (semantically equivalent comparison — shadow positive test)
  6. return_value (function return expression)
  7. decorator (method decorator change)
  8. signature (default argument value change)
  9. import (broken/altered import statement)
 10. class_body (class attribute scalar change)
 11. fixture (SQL fixture edit)
 12. verifier_timeout (infinite loop — verifier timeout must KEEP tests)
 13. verifier_error (syntax error — verifier error must KEEP tests)

For each edit, computes:
  - static_set (BlastRadiusSelector without verifier)
  - verified_set (BlastRadiusSelector with verifier)
  - dropped = static_set - verified_set
Then applies the patch to an isolated copy of /sqlglot and runs real `pytest`
on every test in `dropped` (catching failures, errors, timeouts, AND pytest 9
SUBFAILED subtests!). Reports any dropped test that pytest says fails.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
if "/app" not in sys.path and Path("/app/selector.py").is_file():
    sys.path.insert(0, "/app")

import selector as sel  # noqa: E402
import verifier as ver  # noqa: E402


def _make_diff(rel: str, old_text: str, new_text: str) -> str:
    assert old_text != new_text, f"No-op synthetic edit on {rel}"
    diff_lines = list(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
            n=3,
        )
    )
    return f"diff --git a/{rel} b/{rel}\n" + "".join(diff_lines)


def _replace_once(sqlglot_root: Path, rel: str, old_sub: str, new_sub: str) -> tuple[str, dict[str, str]]:
    text = (sqlglot_root / rel).read_text("utf-8")
    assert old_sub in text, f"Pattern {old_sub!r} not found in {rel}"
    new_text = text.replace(old_sub, new_sub, 1)
    return _make_diff(rel, text, new_text), {rel: new_text}


def generate_synthetic_edits(sqlglot_root: Path) -> list[dict[str, Any]]:
    """Build a diverse suite of synthetic edits purely from pinned commit files."""
    edits: list[dict[str, Any]] = []

    def add(name: str, kind: str, rel: str, old_sub: str, new_sub: str) -> None:
        patch, files = _replace_once(sqlglot_root, rel, old_sub, new_sub)
        edits.append({"name": name, "kind": kind, "rel": rel, "patch": patch, "files": files})

    # 1. Constant (module-level)
    add(
        "const_serde_class_key",
        "constant",
        "sqlglot/serde.py",
        'CLASS = "c"',
        'CLASS = "k"',
    )

    # 2. Dict value (class dict literal value with unchanged keys)
    add(
        "dict_val_postgres_time",
        "dict_value",
        "sqlglot/dialects/postgres.py",
        '"YYYY": "%Y"',
        '"YYYY": "%y"',
    )

    # 3. Condition (common boolean operator flip in covered optimizer function)
    add(
        "cond_simplify_and_to_or",
        "condition",
        "sqlglot/optimizer/simplify.py",
        "if isinstance(this, exp.Not) and self.dialect.SAFE_TO_ELIMINATE_DOUBLE_NEGATION:",
        "if isinstance(this, exp.Not) or self.dialect.SAFE_TO_ELIMINATE_DOUBLE_NEGATION:",
    )

    # 4a. Condition (ADVERSARIAL rare threshold — 3 tests incl. 28.8s test_max_nodes!)
    add(
        "cond_rare_max_nodes",
        "condition_rare",
        "sqlglot/parser.py",
        "if self._node_count > self.max_nodes:",
        "if self._node_count > self.max_nodes + 1000000:",
    )

    # 4b. Condition (ADVERSARIAL rare threshold — 59 tests, right inside shadow pruning zone!)
    add(
        "cond_rare_max_text_width",
        "condition_rare",
        "sqlglot/generator.py",
        "return sum(len(arg) for arg in args) > self.max_text_width",
        "return sum(len(arg) for arg in args) > self.max_text_width + 200",
    )

    # 4c. Condition (ADVERSARIAL rare guard in simplify.py — 39 tests)
    add(
        "cond_rare_where_filter",
        "condition_rare",
        "sqlglot/optimizer/simplify.py",
        "if always_true(where.this) and not isinstance(where.parent, exp.Filter):",
        "if always_true(where.this) and isinstance(where.parent, exp.Filter):",
    )

    # 4d. Condition (ADVERSARIAL short-circuited rare branch — 101 tests, only 2 enter body!)
    add(
        "cond_rare_pivots_star",
        "condition_rare",
        "sqlglot/optimizer/qualify_columns.py",
        'if pivots and source_columns and "*" not in source_columns:',
        'if pivots and source_columns and "*" in source_columns:',
    )

    # 4e. Condition (ADVERSARIAL short-circuited rare branch — 51 tests, only 1 enters body!)
    add(
        "cond_rare_gen_comments",
        "condition_rare",
        "sqlglot/optimizer/simplify.py",
        "if comments and node.comments:",
        "if comments and not node.comments:",
    )

    # 4f. Condition (ADVERSARIAL evaluated-every-time rare-True branch — 76 tests, 2 enter body!)
    add(
        "cond_rare_source_none_trap",
        "condition_rare",
        "sqlglot/optimizer/qualify_columns.py",
        "if source is None:",
        "if source is None and False:",
    )

    # 4g. Condition (ADVERSARIAL evaluated-every-time rare-True branch — 59 tests, 2 enter body!)
    add(
        "cond_rare_mysql_unsigned_trap",
        "condition_rare",
        "sqlglot/generators/mysql.py",
        "if expression.this in self.UNSIGNED_TYPE_MAPPING:",
        "if expression.this in self.UNSIGNED_TYPE_MAPPING and False:",
    )

    # 5. Condition (semantically equivalent `is None` -> `== None` on Context._table)
    add(
        "cond_equiv_context_is_none",
        "condition_equiv",
        "sqlglot/executor/context.py",
        "if self._table is None:",
        "if self._table == None:",
    )

    # 6. Return value (helper function)
    add(
        "return_val_helper_ensure_list",
        "return_value",
        "sqlglot/helper.py",
        "    if value is None:\n        return []",
        "    if value is None:\n        return [None]",
    )

    # 7. Decorator change
    add(
        "decorator_context_property",
        "decorator",
        "sqlglot/executor/context.py",
        "@property\n    def table(self) -> Table:",
        "@staticmethod\n    def table(self) -> Table:",
    )

    # 8. Signature default change
    add(
        "signature_generator_pretty",
        "signature",
        "sqlglot/generator.py",
        "pretty: bool | int | None = None,",
        "pretty: bool | int | None = True,",
    )

    # 9. Import change (broken import)
    add(
        "import_lineage_broken",
        "import",
        "sqlglot/lineage.py",
        "from sqlglot import Schema, exp, maybe_parse",
        "from sqlglot import Schema, exp, maybe_parse, NONEXISTENT_SYM_XYZ",
    )

    # 10. Class body scalar attribute change
    add(
        "class_body_postgres_index_offset",
        "class_body",
        "sqlglot/dialects/postgres.py",
        "INDEX_OFFSET = 1",
        "INDEX_OFFSET = 0",
    )

    # 11. Fixture edit
    norm_txt = (sqlglot_root / "tests/fixtures/optimizer/normalize.sql").read_text("utf-8")
    first_semi = norm_txt.find(";")
    assert first_semi != -1
    norm_new = norm_txt[:first_semi] + " AND 1 = 2" + norm_txt[first_semi:]
    edits.append(
        {
            "name": "fixture_normalize_sql",
            "kind": "fixture",
            "rel": "tests/fixtures/optimizer/normalize.sql",
            "patch": _make_diff("tests/fixtures/optimizer/normalize.sql", norm_txt, norm_new),
            "files": {"tests/fixtures/optimizer/normalize.sql": norm_new},
        }
    )

    # 12. Verifier timeout safety (infinite loop inside covered function)
    add(
        "safety_timeout_infinite_loop",
        "verifier_timeout",
        "sqlglot/serde.py",
        "def dump(expression: exp.Expr) -> list[dict[str, t.Any]]:",
        "def dump(expression: exp.Expr) -> list[dict[str, t.Any]]:\n    while True:\n        pass",
    )

    # 13. Verifier syntax error safety
    add(
        "safety_syntax_error",
        "verifier_error",
        "sqlglot/generators/sqlite.py",
        "class SQLiteGenerator(generator.Generator):",
        "class SQLiteGenerator(((",
    )

    return edits


def run_pytest_on_tests(
    sqlglot_root: Path,
    file_overrides: dict[str, str],
    test_nodeids: list[str],
) -> set[str]:
    """Apply `file_overrides` to an isolated /tmp copy of sqlglot_root and run real
    pytest (SKIP_INTEGRATION=0, catching FAILED, ERROR, and pytest 9 SUBFAILED) on
    `test_nodeids`. Returns the set of failing nodeids."""
    if not test_nodeids:
        return set()

    # Copy only patched files into a tiny overlay dir? No — pytest needs real files;
    # symlink everything in /sqlglot except patched files into a temp dir in /tmp!
    with tempfile.TemporaryDirectory(prefix="diff_pytest_") as tmpdir:
        tmp = Path(tmpdir)
        # Create shallow directory mirror using symlinks for untouched subtrees/files
        override_paths = set(file_overrides.keys())

        def mirror(src_dir: Path, dst_dir: Path, rel_prefix: str = "") -> None:
            dst_dir.mkdir(parents=True, exist_ok=True)
            for entry in src_dir.iterdir():
                if entry.name == "__pycache__":
                    continue
                rel = f"{rel_prefix}{entry.name}" if not rel_prefix else f"{rel_prefix}/{entry.name}"
                # Check if any override is inside this subtree
                if entry.is_dir():
                    prefix_slash = rel + "/"
                    if any(op.startswith(prefix_slash) for op in override_paths):
                        mirror(entry, dst_dir / entry.name, rel)
                    else:
                        os.symlink(entry, dst_dir / entry.name)
                else:
                    if rel in override_paths:
                        (dst_dir / entry.name).write_text(file_overrides[rel], encoding="utf-8")
                    else:
                        os.symlink(entry, dst_dir / entry.name)

        mirror(sqlglot_root, tmp)

        env = os.environ.copy()
        env["PYTHONPATH"] = str(tmp)
        env["SKIP_INTEGRATION"] = "0"
        env["PYTHONDONTWRITEBYTECODE"] = "1"

        # Run pytest on the target nodeids with -p no:cacheprovider and per-test timeout
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-o",
            "addopts=",
            "--timeout=45",
            "-r",
            "fE",
            "-v",
            *test_nodeids,
        ]
        if len(test_nodeids) >= 8:
            cmd.insert(3, "-n")
            cmd.insert(4, "2")

        proc = subprocess.run(
            cmd,
            cwd=str(tmp),
            env=env,
            capture_output=True,
            text=True,
        )
        output = proc.stdout + "\n" + proc.stderr

        # Parse failures, errors, AND pytest 9 SUBFAILED lines!
        failing: set[str] = set()
        nodeid_set = set(test_nodeids)
        for line in output.splitlines():
            s = line.strip()
            if s.startswith(("FAILED ", "ERROR ", "SUBFAILED")):
                for nid in nodeid_set:
                    if nid in s:
                        failing.add(nid)
            elif " FAILED" in s or " ERROR" in s or " SUBFAILED" in s:
                for nid in nodeid_set:
                    if nid in s:
                        failing.add(nid)

        # If pytest exited non-zero and we couldn't parse a specific nodeid (e.g. collection error),
        # conservatively mark all tests in files that errored (or all tested nodeids) as failing!
        if proc.returncode != 0 and not failing:
            failing.update(nodeid_set)

        return failing


def main() -> None:
    sqlglot_root = Path("/sqlglot")
    map_path = Path("/app/map.pkl.gz")
    tests_json_path = Path("/data/tests.json")

    map_data = sel.load_map(map_path)
    tests = json.loads(tests_json_path.read_text("utf-8"))["tests"]
    num_to_id = {t["number"]: t["id"] for t in tests}

    # Build both static-only and hybrid-verified selectors (disable batch wall cutoff during test)
    os.environ["BLAST_WALL_LIMIT_SEC"] = "3600"
    old_disable = os.environ.get("BLAST_DISABLE_VERIFIER")
    os.environ["BLAST_DISABLE_VERIFIER"] = "1"
    sel_static = sel.BlastRadiusSelector(map_data, tests)
    if old_disable is None:
        os.environ.pop("BLAST_DISABLE_VERIFIER", None)
    else:
        os.environ["BLAST_DISABLE_VERIFIER"] = old_disable
    sel_hybrid = sel.BlastRadiusSelector(map_data, tests)
    if sel_hybrid.verifier is not None:
        sel_hybrid.verifier.wall_cutoff_sec = 3600.0

    edits = generate_synthetic_edits(sqlglot_root)
    print(f"Generated {len(edits)} synthetic edits across all categories.\n")
    print(f"{'name':30s} {'kind':16s} {'static':>6s} {'verif':>6s} {'dropped':>7s} {'wrong_drops':>11s}  detail")
    print("-" * 105)

    total_wrong_drops = 0
    shadow_wrong_drops = 0
    results_summary: list[dict[str, Any]] = []

    for ed in edits:
        st_nums = sel_static.select_test_numbers(ed["patch"])
        hy_nums = sel_hybrid.select_test_numbers(ed["patch"])
        dropped_nums = sorted(set(st_nums) - set(hy_nums))
        dropped_nids = [num_to_id[n] for n in dropped_nums if n in num_to_id]

        # Check if shadow pruning could have been involved
        parsed = sel.pp.parse_unified_diff(ed["patch"])
        cats = [sel.pp.classify_file(fp.path) for fp in parsed.files]
        ppy, pnon, _, ok = sel_hybrid._build_overlay_for_verifier(parsed, cats)
        is_shadow = ver._can_shadow_instrument(ppy, pnon) if ok else False

        wrong_nids = run_pytest_on_tests(sqlglot_root, ed["files"], dropped_nids) if dropped_nids else set()
        total_wrong_drops += len(wrong_nids)
        if wrong_nids and is_shadow:
            shadow_wrong_drops += len(wrong_nids)

        detail = ""
        if wrong_nids:
            detail = f"WRONG DROP (shadow={is_shadow}): {sorted(wrong_nids)[:3]}"
        elif ed["kind"] in ("verifier_timeout", "verifier_error"):
            detail = "KEPT ALL (safe)" if not dropped_nums else f"dropped {len(dropped_nums)} passed"
        else:
            detail = f"ok (shadow={is_shadow})"

        print(
            f"{ed['name']:30s} {ed['kind']:16s} {len(st_nums):6d} {len(hy_nums):6d} "
            f"{len(dropped_nums):7d} {len(wrong_nids):11d}  {detail}"
        )
        results_summary.append(
            {
                "name": ed["name"],
                "kind": ed["kind"],
                "static": len(st_nums),
                "verified": len(hy_nums),
                "dropped": len(dropped_nums),
                "wrong_drops": sorted(wrong_nids),
                "is_shadow": is_shadow,
            }
        )

    print("-" * 105)
    print(f"TOTAL WRONG DROPS: {total_wrong_drops} (shadow-related: {shadow_wrong_drops})")
    if total_wrong_drops > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
