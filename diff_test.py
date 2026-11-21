#!/usr/bin/env python3
"""Differential test: >= 100 synthetic edits comparing build-time test-specific
profiles (and optional verifier) against real pytest (SKIP_INTEGRATION=0, pytest 9
with subtests).

Generates >= 100 synthetic patches purely from the pinned sqlglot commit across:
  - Hand-crafted adversarial & category edits (constants, dict values, conditions,
    rare-True traps including `if source is None:` -> `if source is None and False:`,
    return values, decorators, signatures, imports, class bodies, fixtures,
    infinite-loop timeout, syntax error)
  - Programmatically AST-mined single-expression edits across sqlglot/**/*.py:
      * P1: `is` -> `==`, `is not` -> `!=`
      * P2: `==` -> `is`, `!=` -> `is not` (including non-singleton traps!)
      * P3: `<` <-> `<=`, `>` <-> `>=` (boundary & non-boundary)
      * P4: `E` -> `E and False` (rare-True traps & always-False branches)
      * P5: `E` -> `E or True` (rare-False traps & always-True branches)
      * P6: `and` <-> `or` flips (pure & impure)
      * Side-effect traps: `E` -> `False` / `E` -> `True` on calls & attributes

For EVERY edit, computes:
  - base_set   (BlastRadiusSelector with profiles OFF, verifier OFF)
  - target_set (BlastRadiusSelector with profiles ON, and optional verifier mode)
  - dropped = base_set - target_set
Then applies the patch to an isolated temp copy of /sqlglot and runs real `pytest`
on EVERY test in `dropped` (catching FAILED, ERROR, timeouts, and pytest 9 SUBFAILED).
Requires 0 wrong drops across all >= 100 edits.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
if "/app" not in sys.path and Path("/app/selector.py").is_file():
    sys.path.insert(0, "/app")

import selector as sel  # noqa: E402


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


def _replace_node_span(
    text: str,
    node: ast.AST,
    replacement: str,
) -> str | None:
    """Replace the exact character span of `node` in `text` with `replacement`."""
    lineno = getattr(node, "lineno", None)
    end_lineno = getattr(node, "end_lineno", None)
    col = getattr(node, "col_offset", None)
    end_col = getattr(node, "end_col_offset", None)
    if None in (lineno, end_lineno, col, end_col):
        return None
    lines = text.splitlines(keepends=True)
    if lineno != end_lineno:
        return None  # Keep single-line edits clean and deterministic
    line = lines[lineno - 1]
    raw_line = line.encode("utf-8")
    try:
        new_raw = raw_line[:col] + replacement.encode("utf-8") + raw_line[end_col:]
        new_line = new_raw.decode("utf-8")
    except Exception:
        return None
    if new_line == line:
        return None
    lines[lineno - 1] = new_line
    new_text = "".join(lines)
    try:
        ast.parse(new_text)
    except SyntaxError:
        return None
    return new_text


def _get_node_src(text: str, node: ast.AST) -> str | None:
    lineno = getattr(node, "lineno", None)
    end_lineno = getattr(node, "end_lineno", None)
    col = getattr(node, "col_offset", None)
    end_col = getattr(node, "end_col_offset", None)
    if None in (lineno, end_lineno, col, end_col) or lineno != end_lineno:
        return None
    line = text.splitlines(keepends=True)[lineno - 1].encode("utf-8")
    try:
        return line[col:end_col].decode("utf-8")
    except Exception:
        return None


def _mine_ast_edits(
    sqlglot_root: Path,
    map_data: dict[str, Any],
    existing_keys: set[tuple[str, int]],
) -> list[dict[str, Any]]:
    """Deterministically mine diverse single-expression edits from pinned sqlglot/**/*.py."""
    line_to_tests: dict[str, dict[int, int]] = map_data["line_to_tests"]
    expr_profiles: dict[str, dict[tuple[int, int], tuple[int, ...]]] = map_data.get("expr_profiles", {})

    # Quotas per mutation family to ensure broad, balanced >= 110 coverage
    quotas = {
        "p1_is_to_eq": 18,
        "p2_eq_to_is": 18,
        "p3_order_flip": 18,
        "p4_and_false": 20,
        "p5_or_true": 16,
        "p6_boolop_flip": 14,
        "side_effect_const": 10,
    }
    collected: dict[str, list[dict[str, Any]]] = {k: [] for k in quotas}
    used_sites: set[tuple[str, int, str]] = set()

    # Deterministic file order prioritizing rich optimizer/executor/parser/generator/dialect files
    py_files = sorted(
        p.relative_to(sqlglot_root).as_posix()
        for p in (sqlglot_root / "sqlglot").rglob("*.py")
        if not p.relative_to(sqlglot_root).as_posix().startswith("sqlglot/typing/")
    )

    def _can_add(family: str) -> bool:
        return len(collected[family]) < quotas[family]

    def _add_mined(family: str, rel: str, ln: int, text: str, new_text: str) -> None:
        if (rel, ln) in existing_keys or (rel, ln, family) in used_sites:
            return
        used_sites.add((rel, ln, family))
        idx = len(collected[family]) + 1
        stem = Path(rel).stem[:12]
        name = f"{family}_{idx:02d}_{stem}_L{ln}"
        collected[family].append(
            {
                "name": name,
                "kind": family,
                "rel": rel,
                "patch": _make_diff(rel, text, new_text),
                "files": {rel: new_text},
            }
        )

    for rel in py_files:
        if all(not _can_add(fam) for fam in quotas):
            break
        lmap = line_to_tests.get(rel, {})
        raw_profs = expr_profiles.get(rel, {})
        if not lmap or not raw_profs:
            continue
        profs = {(k[0], k[1]): v for k, v in raw_profs.items()}
        text = (sqlglot_root / rel).read_text("utf-8")
        try:
            tree = ast.parse(text, filename=rel)
        except SyntaxError:
            continue

        # Walk inside function bodies only
        def walk_func_body(stmts: list[ast.stmt], in_func: bool) -> None:
            for node in stmts:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    walk_func_body(node.body, True)
                    continue
                if isinstance(node, ast.ClassDef):
                    walk_func_body(node.body, False)
                    continue
                if not in_func:
                    continue
                for sub in ast.walk(node):
                    ln = getattr(sub, "lineno", None)
                    col = getattr(sub, "col_offset", None)
                    if ln is None or col is None:
                        continue
                    cov = lmap.get(ln, 0)
                    cov_cnt = cov.bit_count()
                    if cov_cnt < 1 or cov_cnt > 150:
                        continue

                    # P1 / P2 / P3 on Compare sites
                    if isinstance(sub, ast.Compare) and (ln, col) in profs:
                        # P1: is -> == / is not -> !=
                        if (
                            _can_add("p1_is_to_eq")
                            and len(sub.ops) == 1
                            and isinstance(sub.ops[0], (ast.Is, ast.IsNot))
                        ):
                            src = _get_node_src(text, sub)
                            if src:
                                if isinstance(sub.ops[0], ast.Is) and " is " in src:
                                    repl = src.replace(" is ", " == ", 1)
                                elif isinstance(sub.ops[0], ast.IsNot) and " is not " in src:
                                    repl = src.replace(" is not ", " != ", 1)
                                else:
                                    repl = ""
                                if repl:
                                    nt = _replace_node_span(text, sub, repl)
                                    if nt:
                                        _add_mined("p1_is_to_eq", rel, ln, text, nt)

                        # P2: == -> is / != -> is not (mix of safe singletons & unsafe strings/ints!)
                        if (
                            _can_add("p2_eq_to_is")
                            and len(sub.ops) == 1
                            and isinstance(sub.ops[0], (ast.Eq, ast.NotEq))
                        ):
                            src = _get_node_src(text, sub)
                            if src:
                                if isinstance(sub.ops[0], ast.Eq) and " == " in src:
                                    repl = src.replace(" == ", " is ", 1)
                                elif isinstance(sub.ops[0], ast.NotEq) and " != " in src:
                                    repl = src.replace(" != ", " is not ", 1)
                                else:
                                    repl = ""
                                if repl:
                                    nt = _replace_node_span(text, sub, repl)
                                    if nt:
                                        _add_mined("p2_eq_to_is", rel, ln, text, nt)

                        # P3: < <-> <= and > <-> >=
                        if (
                            _can_add("p3_order_flip")
                            and len(sub.ops) == 1
                            and isinstance(sub.ops[0], (ast.Lt, ast.LtE, ast.Gt, ast.GtE))
                        ):
                            src = _get_node_src(text, sub)
                            if src:
                                op = sub.ops[0]
                                repl = ""
                                if isinstance(op, ast.Lt) and " < " in src:
                                    repl = src.replace(" < ", " <= ", 1)
                                elif isinstance(op, ast.LtE) and " <= " in src:
                                    repl = src.replace(" <= ", " < ", 1)
                                elif isinstance(op, ast.Gt) and " > " in src:
                                    repl = src.replace(" > ", " >= ", 1)
                                elif isinstance(op, ast.GtE) and " >= " in src:
                                    repl = src.replace(" >= ", " > ", 1)
                                if repl:
                                    nt = _replace_node_span(text, sub, repl)
                                    if nt:
                                        _add_mined("p3_order_flip", rel, ln, text, nt)

                    # P4 / P5 / P6 / side_effect_const on If.test
                    if isinstance(sub, ast.If):
                        t = sub.test
                        t_ln = getattr(t, "lineno", None)
                        t_col = getattr(t, "col_offset", None)
                        if t_ln is None or t_col is None or (t_ln, t_col) not in profs:
                            continue
                        tsrc = _get_node_src(text, t)
                        if not tsrc:
                            continue
                        prof = profs[(t_ln, t_col)]
                        seen_b, false_b, true_b = prof[0], prof[1], prof[2]

                        # P4: `E` -> `E and False` (include both rare-True traps and always-False!)
                        if _can_add("p4_and_false") and (seen_b & ~true_b):
                            nt = _replace_node_span(text, t, f"({tsrc}) and False" if " or " in tsrc else f"{tsrc} and False")
                            if nt:
                                _add_mined("p4_and_false", rel, t_ln, text, nt)

                        # P5: `E` -> `E or True` (include both rare-False traps and always-True!)
                        if _can_add("p5_or_true") and (seen_b & ~false_b):
                            nt = _replace_node_span(text, t, f"({tsrc}) or True" if " and " in tsrc else f"{tsrc} or True")
                            if nt:
                                _add_mined("p5_or_true", rel, t_ln, text, nt)

                        # P6: `and` <-> `or` flip
                        if _can_add("p6_boolop_flip") and isinstance(t, ast.BoolOp) and len(t.values) == 2:
                            if isinstance(t.op, ast.And) and " and " in tsrc:
                                repl = tsrc.replace(" and ", " or ", 1)
                            elif isinstance(t.op, ast.Or) and " or " in tsrc:
                                repl = tsrc.replace(" or ", " and ", 1)
                            else:
                                repl = ""
                            if repl:
                                nt = _replace_node_span(text, t, repl)
                                if nt:
                                    _add_mined("p6_boolop_flip", rel, t_ln, text, nt)

                        # Side-effect traps: replace Call/Attribute branch test with literal False/True
                        if _can_add("side_effect_const") and any(
                            isinstance(x, (ast.Call, ast.Attribute, ast.Subscript)) for x in ast.walk(t)
                        ):
                            lit = "False" if (seen_b & ~true_b) else "True"
                            nt = _replace_node_span(text, t, lit)
                            if nt:
                                _add_mined("side_effect_const", rel, t_ln, text, nt)

        for stmt in tree.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk_func_body(stmt.body, True)
            elif isinstance(stmt, ast.ClassDef):
                walk_func_body(stmt.body, False)

    out: list[dict[str, Any]] = []
    for fam in quotas:
        out.extend(collected[fam])
    return out


def generate_synthetic_edits(sqlglot_root: Path, map_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Build >= 100 diverse synthetic edits purely from pinned commit files."""
    edits: list[dict[str, Any]] = []
    existing_keys: set[tuple[str, int]] = set()

    def add(name: str, kind: str, rel: str, old_sub: str, new_sub: str) -> None:
        text = (sqlglot_root / rel).read_text("utf-8")
        pos = text.find(old_sub)
        if pos >= 0:
            ln = text[:pos].count("\n") + 1
            existing_keys.add((rel, ln))
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

    # 4b. Condition (ADVERSARIAL rare threshold — 59 tests)
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

    # 4f. Condition (MANDATORY TRAP: `if source is None:` -> `if source is None and False:`)
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

    # Add >= 100 programmatically mined AST single-expression edits across sqlglot/**/*.py
    # (Mined BEFORE phase-specific stress additions so original 133 names & 20% hold-out stay invariant!)
    mined = _mine_ast_edits(sqlglot_root, map_data, existing_keys)
    edits.extend(mined)

    # --- Phase 1 Quick-Set Stress Cases (multi-site feed, nested, short-circuit, __init__) ---
    def add_multi(name: str, kind: str, rel: str, replacements: list[tuple[str, str]]) -> None:
        text = (sqlglot_root / rel).read_text("utf-8")
        new_text = text
        for old_sub, new_sub in replacements:
            assert old_sub in new_text, f"Pattern {old_sub!r} not found in {rel}"
            new_text = new_text.replace(old_sub, new_sub, 1)
        edits.append(
            {
                "name": name,
                "kind": kind,
                "rel": rel,
                "patch": _make_diff(rel, text, new_text),
                "files": {rel: new_text},
                "quick": True,
            }
        )

    # 14a. Multi-site where Site 1 feeds Site 2 (BOTH proven equivalent)
    add_multi(
        "p1_multisite_feed_safe",
        "p1_multisite_feed",
        "sqlglot/anonymize.py",
        [
            ("is_number = token.token_type == TokenType.NUMBER", "is_number = token.token_type is TokenType.NUMBER"),
            ("if alias is None:", "if alias == None:"),
        ],
    )

    # 14b. Multi-site where Site 1 feeds Site 2 (Site 1 BREAKS, Site 2 equivalent -> MUST KEEP!)
    add_multi(
        "p1_multisite_feed_trap",
        "p1_multisite_feed",
        "sqlglot/anonymize.py",
        [
            ("is_number = token.token_type == TokenType.NUMBER", "is_number = token.token_type != TokenType.NUMBER"),
            ("if alias is None:", "if alias == None:"),
        ],
    )

    # 14c. Multi-file commit: context.py (proven equiv) + helper.py (breaking return)
    ctx_old = (sqlglot_root / "sqlglot/executor/context.py").read_text("utf-8")
    ctx_new = ctx_old.replace("if self._table is None:", "if self._table == None:", 1)
    hlp_old = (sqlglot_root / "sqlglot/helper.py").read_text("utf-8")
    hlp_new = hlp_old.replace("    if value is None:\n        return []", "    if value is None:\n        return [None]", 1)
    edits.append(
        {
            "name": "p1_multisite_two_files_trap",
            "kind": "p1_multisite_feed",
            "rel": "sqlglot/executor/context.py+sqlglot/helper.py",
            "patch": _make_diff("sqlglot/executor/context.py", ctx_old, ctx_new)
            + _make_diff("sqlglot/helper.py", hlp_old, hlp_new),
            "files": {
                "sqlglot/executor/context.py": ctx_new,
                "sqlglot/helper.py": hlp_new,
            },
            "quick": True,
        }
    )

    # 14d. Nested / overlapping sites in ONE expression (outer BoolOp + inner Compare -> MUST KEEP!)
    add(
        "p1_nested_expr_trap",
        "p1_nested_expr",
        "sqlglot/anonymize.py",
        "            token.token_type == TokenType.VAR\n            and i + 1 < len(tokens)\n            and tokens[i + 1].token_type == TokenType.L_PAREN",
        "            token.token_type == TokenType.VAR\n            or i + 1 <= len(tokens)\n            and tokens[i + 1].token_type == TokenType.L_PAREN",
    )

    # 14e. Short-circuit IfExp orelse arm mutation (athena.py L106)
    add(
        "p1_short_circuit_ifexp",
        "p1_short_circuit",
        "sqlglot/dialects/athena.py",
        "return trino_tokens if trino_tokens is not None else self._trino_tokenizer.tokenize(sql)",
        "return trino_tokens if trino_tokens is not None else []",
    )

    # 14f. Short-circuit BoolOp 3rd operand mutation (anonymize.py L80)
    add(
        "p1_short_circuit_and_rhs",
        "p1_short_circuit",
        "sqlglot/anonymize.py",
        "and tokens[i + 1].token_type == TokenType.L_PAREN",
        "and tokens[i + 1].token_type != TokenType.L_PAREN",
    )

    # 14g. sqlglot/__init__.py function-body edit inside transpile() (IfExp.orelse)
    add(
        "p1_root_init_func_transpile",
        "p1_root_init",
        "sqlglot/__init__.py",
        'write.generate(expression, copy=False, **opts) if expression else ""',
        'write.generate(expression, copy=False, **opts) if expression else "DEAD"',
    )

    # 14h. sqlglot/__init__.py function-body edit (breaking inside parse_one -> MUST KEEP callers!)
    add(
        "p1_root_init_func_break",
        "p1_root_init",
        "sqlglot/__init__.py",
        "if not result or result[0] is None:",
        "if not result or result[0] is not None:",
    )

    return edits


def is_holdout_edit(name: str) -> bool:
    """Deterministic 20% hold-out split by edit name hash (never used for dev tuning)."""
    # Keep mandatory explicit traps in dev/quick so gate.sh always tests them
    if name in (
        "cond_rare_source_none_trap",
        "cond_rare_mysql_unsigned_trap",
        "safety_timeout_infinite_loop",
        "safety_syntax_error",
    ):
        return False
    return hashlib.sha256(name.encode("utf-8")).digest()[0] < 51  # 51/256 ~= 19.9%


def select_quick_edits(edits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Select ~30-40 representative quick-mode edits covering every category,
    all hand-crafted adversarial traps, and any phase-specific stress edits."""
    hand_crafted_kinds = {
        "constant",
        "dict_value",
        "condition",
        "condition_rare",
        "condition_equiv",
        "return_value",
        "decorator",
        "signature",
        "import",
        "class_body",
        "fixture",
        "verifier_timeout",
        "verifier_error",
        # Phase 1-4 dedicated stress kinds
        "p1_multisite_feed",
        "p1_nested_expr",
        "p1_short_circuit",
        "p1_root_init",
        "p2_mapping_key",
        "p3_equiv",
        "p4_range_const_default",
    }
    picked: list[dict[str, Any]] = []
    mined_per_kind: dict[str, int] = {}
    for ed in edits:
        k = ed["kind"]
        if k in hand_crafted_kinds or ed.get("quick"):
            picked.append(ed)
        else:
            cnt = mined_per_kind.get(k, 0)
            # Pick first 2 non-holdout edits per mined family for quick gate
            if cnt < 2 and not is_holdout_edit(ed["name"]):
                picked.append(ed)
                mined_per_kind[k] = cnt + 1
    return picked


def run_pytest_on_tests(
    sqlglot_root: Path,
    file_overrides: dict[str, str],
    test_nodeids: list[str],
    hard_timeout_sec: float = 300.0,
) -> set[str]:
    """Apply `file_overrides` to an isolated /tmp copy of sqlglot_root and run real
    pytest (SKIP_INTEGRATION=0, catching FAILED, ERROR, timeouts, and pytest 9 SUBFAILED)
    on `test_nodeids`. Uses process-group kill on timeout. Returns failing nodeids."""
    if not test_nodeids:
        return set()

    with tempfile.TemporaryDirectory(prefix="diff_pytest_") as tmpdir:
        tmp = Path(tmpdir)
        override_paths = set(file_overrides.keys())

        def mirror(src_dir: Path, dst_dir: Path, rel_prefix: str = "") -> None:
            dst_dir.mkdir(parents=True, exist_ok=True)
            for entry in src_dir.iterdir():
                if entry.name == "__pycache__":
                    continue
                rel = f"{rel_prefix}{entry.name}" if not rel_prefix else f"{rel_prefix}/{entry.name}"
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

        # Chunk large test sets (>350 tests) to avoid argv/memory issues
        failing: set[str] = set()
        t_deadline = time.monotonic() + hard_timeout_sec
        for start in range(0, len(test_nodeids), 350):
            chunk = test_nodeids[start : start + 350]
            rem = t_deadline - time.monotonic()
            if rem <= 2.0:
                failing.update(chunk)
                continue
            cmd = [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "no:cacheprovider",
                "-o",
                "addopts=",
                "--timeout=180",
                "-r",
                "fE",
                "-v",
                *chunk,
            ]
            if len(chunk) >= 12 and not any("test_execute_tpcds" in n for n in chunk):
                cmd.insert(3, "-n")
                cmd.insert(4, "2")

            proc = subprocess.Popen(
                cmd,
                cwd=str(tmp),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                stdout, stderr = proc.communicate(timeout=rem)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    proc.communicate(timeout=5)
                except Exception:
                    pass
                failing.update(chunk)
                continue

            output = (stdout or "") + "\n" + (stderr or "")
            chunk_set = set(chunk)
            chunk_fail: set[str] = set()
            for line in output.splitlines():
                s = line.strip()
                # Skip passing / subpassed lines (subTest msgs may contain SQL like "ERROR ON ERROR"!)
                if s.startswith(("PASSED ", "SUBPASSED")) or " PASSED" in s or " SUBPASSED" in s:
                    continue
                if s.startswith(("FAILED ", "ERROR ", "SUBFAILED")) or (
                    s.endswith((" FAILED", " ERROR", " SUBFAILED"))
                    or " FAILED [" in s
                    or " ERROR [" in s
                    or " SUBFAILED [" in s
                ):
                    for nid in chunk_set:
                        if nid in s:
                            chunk_fail.add(nid)
            if proc.returncode != 0 and not chunk_fail:
                chunk_fail.update(chunk_set)
            failing.update(chunk_fail)

        return failing


def main() -> None:
    parser = argparse.ArgumentParser(description="Differential test vs real pytest.")
    parser.add_argument("--quick", action="store_true", help="Run representative ~30-edit quick suite")
    parser.add_argument("--split", choices=("all", "dev", "holdout"), default="all")
    args = parser.parse_args()

    sqlglot_root = Path("/sqlglot")
    map_path = Path("/app/map.pkl.gz")
    tests_json_path = Path("/data/tests.json")

    map_data = sel.load_map(map_path)
    tests = json.loads(tests_json_path.read_text("utf-8"))["tests"]
    num_to_id = {t["number"]: t["id"] for t in tests}

    verifier_mode = os.environ.get("DIFF_VERIFIER_MODE", "off").strip().lower()
    os.environ["BLAST_WALL_LIMIT_SEC"] = "3600"

    # Base selector: conservative pure-static baseline (ALL pruning phases OFF)
    saved_env = {
        k: os.environ.get(k)
        for k in (
            "BLAST_DISABLE_PROFILES",
            "BLAST_VERIFIER_MODE",
            "BLAST_DISABLE_VERIFIER",
            "BLAST_P1_REACH",
            "BLAST_P1_MULTISITE",
            "BLAST_P1_ROOT_INIT",
            "BLAST_P2_MAPPINGS",
            "BLAST_P3_EQUIV",
            "BLAST_P4_RANGES",
        )
    }
    os.environ["BLAST_DISABLE_PROFILES"] = "1"
    os.environ["BLAST_VERIFIER_MODE"] = "off"
    os.environ["BLAST_P1_REACH"] = "0"
    os.environ["BLAST_P1_MULTISITE"] = "0"
    os.environ["BLAST_P1_ROOT_INIT"] = "0"
    os.environ["BLAST_P2_MAPPINGS"] = "0"
    os.environ["BLAST_P3_EQUIV"] = "0"
    os.environ["BLAST_P4_RANGES"] = "0"
    os.environ.pop("BLAST_DISABLE_VERIFIER", None)
    sel_base = sel.BlastRadiusSelector(map_data, tests)

    # Restore target env (default: all implemented phases ON, verifier = DIFF_VERIFIER_MODE)
    for k, v in saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    os.environ["BLAST_DISABLE_PROFILES"] = "0"
    os.environ["BLAST_VERIFIER_MODE"] = verifier_mode
    sel_target = sel.BlastRadiusSelector(map_data, tests)
    if sel_target.verifier is not None:
        sel_target.verifier.wall_cutoff_sec = 3600.0

    all_edits = generate_synthetic_edits(sqlglot_root, map_data)
    assert len(all_edits) >= 100, f"Expected >= 100 synthetic edits, got {len(all_edits)}"

    if args.quick:
        edits = select_quick_edits(all_edits)
    elif args.split == "holdout":
        edits = [e for e in all_edits if is_holdout_edit(e["name"])]
    elif args.split == "dev":
        edits = [e for e in all_edits if not is_holdout_edit(e["name"])]
    else:
        edits = all_edits

    t0 = time.monotonic()
    print(
        f"Running {len(edits)} synthetic edits (mode={'quick' if args.quick else args.split}, "
        f"profiles=ON, verifier_mode={verifier_mode}).\n",
        flush=True,
    )
    print(f"{'name':38s} {'kind':18s} {'base':>5s} {'kept':>5s} {'drop':>5s} {'wrong':>5s}  detail", flush=True)
    print("-" * 105, flush=True)

    total_wrong_drops = 0
    total_dropped_tests = 0
    edits_with_drops = 0
    by_kind: dict[str, list[int]] = {}

    for ed in edits:
        st_nums = sel_base.select_test_numbers(ed["patch"])
        tg_nums = sel_target.select_test_numbers(ed["patch"])
        dropped_nums = sorted(set(st_nums) - set(tg_nums))
        dropped_nids = [num_to_id[n] for n in dropped_nums if n in num_to_id]

        wrong_nids = run_pytest_on_tests(sqlglot_root, ed["files"], dropped_nids) if dropped_nids else set()
        total_wrong_drops += len(wrong_nids)
        total_dropped_tests += len(dropped_nums)
        if dropped_nums:
            edits_with_drops += 1

        st_k = by_kind.setdefault(ed["kind"], [0, 0, 0, 0])
        st_k[0] += 1
        if dropped_nums:
            st_k[1] += 1
        st_k[2] += len(dropped_nums)
        st_k[3] += len(wrong_nids)

        detail = ""
        if wrong_nids:
            detail = f"WRONG DROP: {sorted(wrong_nids)[:3]}"
        elif ed["name"] == "cond_rare_source_none_trap":
            kept_nids = {num_to_id[n] for n in tg_nums if n in num_to_id}
            tpcds_kept = any("test_execute_tpcds" in x for x in kept_nids)
            qcols_kept = any("test_qualify_columns" in x for x in kept_nids)
            detail = f"TRAP OK (dropped={len(dropped_nums)} passed; tpcds_kept={tpcds_kept}, qcols_kept={qcols_kept})"
        elif dropped_nums:
            detail = f"all {len(dropped_nums)} dropped passed pytest"
        else:
            detail = "kept all (safe)"

        print(
            f"{ed['name'][:38]:38s} {ed['kind'][:18]:18s} {len(st_nums):5d} {len(tg_nums):5d} "
            f"{len(dropped_nums):5d} {len(wrong_nids):5d}  {detail}",
            flush=True,
        )

    print("-" * 105, flush=True)
    print("\nSummary by edit kind:", flush=True)
    print(f"{'kind':22s} {'edits':>6s} {'w/drops':>8s} {'dropped_tests':>14s} {'wrong_drops':>12s}", flush=True)
    for k, (cnt, ed_d, drp, wrg) in sorted(by_kind.items()):
        print(f"{k:22s} {cnt:6d} {ed_d:8d} {drp:14d} {wrg:12d}", flush=True)
    print("-" * 68, flush=True)
    print(
        f"TOTAL ({time.monotonic() - t0:.1f}s): {len(edits)} edits, {edits_with_drops} edits with drops, "
        f"{total_dropped_tests} dropped tests verified by pytest, "
        f"TOTAL WRONG DROPS: {total_wrong_drops}",
        flush=True,
    )
    if total_wrong_drops > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
