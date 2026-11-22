#!/usr/bin/env python3
"""Blast-Radius test selector for sqlglot.

Standard library only (runs under `python -S -E`). Loads the prebuilt
commit-derived map once, parses each unified diff in `/data/commits/*.patch`,
selects tests via principled static+dynamic blast-radius rules and cost-aware
inclusion, packs tests across optimal worker count (1..8), and emits one
validated JSON line per commit on stdout. All diagnostics go to stderr.
"""

from __future__ import annotations

import ast
import json
import math
import os
import pickle
import sys
import zlib
from pathlib import Path
from typing import Any

# Ensure script directory is on sys.path even under `python -S -E`
_APP_DIR = Path(__file__).resolve().parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

import patch_parser as pp  # noqa: E402

try:
    import packer as _packer_mod  # noqa: E402
except ImportError:
    _packer_mod = None  # type: ignore[assignment]

try:
    import expr_profiler as _expr_profiler_mod  # noqa: E402
except ImportError:
    _expr_profiler_mod = None  # type: ignore[assignment]

try:
    import mapping_profiler as _mapping_profiler_mod  # noqa: E402
except ImportError:
    _mapping_profiler_mod = None  # type: ignore[assignment]

_verifier_mod: Any = None

DATA_DIR = Path("/data")
DEFAULT_MAP_PATH = _APP_DIR / "map.pkl.gz"
DEFAULT_PROFILES_PATH = _APP_DIR / "profiles.pkl.gz"
DEFAULT_SQLGLOT_DIR = Path(os.environ.get("BLAST_SQLGLOT_DIR", "/sqlglot"))
MISS_PENALTY_SEC = 300.0
WORKER_COST_MS = 2_000
MAX_WORKERS = 8

# ---------------------------------------------------------------------------
# Tunable probability / cost thresholds (one central config block for FR4)
# Include test t when P(fail) * 300.0 > marginal_cost_sec(t)
# ---------------------------------------------------------------------------
DEFAULT_CONFIG: dict[str, float] = {
    # Direct line hit, direct fixture hit, syntax error, config/broad fallback
    "p_direct": 1.0,
    # Enclosing multi-line statement of a changed line (e.g. short-circuited branch)
    "p_enclosing_stmt": 0.90,
    # Other tests in enclosing function that did NOT reach the changed statement
    # (when function signature, decorators, and local scope bindings are unchanged)
    "p_enclosing_func": 0.0,
    # Enclosing function hit when statement had zero direct/enclosing-stmt coverage
    # (0.0 when provably unreachable & scope-preserving; >0 for extra conservatism)
    "p_dead_stmt_func": 0.0,
    # Class attribute / dialect table: tests intersecting dialect AND attr reader
    "p_dialect_attr_intersect": 0.90,
    # Class attribute / dialect table: other tests using the dialect (non-intersecting)
    "p_dialect_attr_fallback": 0.0,
    # Expression class attribute: tests instantiating/referencing the Expr subclass
    "p_expr_attr": 0.90,
    # Module-level var/constant: functions referencing the changed variable
    "p_module_var_ref": 0.85,
    # Broad module/importer fallback when change is localized weakly
    "p_module_fallback": 0.05,
    # Pure AST-identical Python comment/whitespace change (verified by encoding + ast.dump)
    "p_ast_identical": 0.0,
    # Assumed effective parallelism divisor k for FR4 marginal cost check
    "fr4_k_divisor": 1.0,
}


def _duration_ms(seconds: float) -> int:
    return math.floor(seconds * 1000 + 0.5)


def _iter_set_bits(bitset: int):
    """Yield indices of 1-bits in a Python integer bitset (fast via bit tricks)."""
    while bitset:
        lsb = bitset & -bitset
        yield lsb.bit_length() - 1
        bitset ^= lsb


def _fallback_pack(test_nums: list[int], dur_ms: dict[int, int]) -> list[list[int]]:
    """Simple LPT packer (replaced by packer.py in Phase 5)."""
    if not test_nums:
        return []
    ordered = sorted(test_nums, key=lambda n: (-dur_ms[n], n))
    best_cost = 10**18
    best_workers: list[list[int]] = [ordered]
    max_k = min(MAX_WORKERS, len(ordered))
    for k in range(1, max_k + 1):
        workers: list[list[int]] = [[] for _ in range(k)]
        loads = [0] * k
        for n in ordered:
            idx = loads.index(min(loads))
            workers[idx].append(n)
            loads[idx] += dur_ms[n]
        cost = max(loads) + WORKER_COST_MS * k
        if cost < best_cost:
            best_cost = cost
            best_workers = workers
    return [w for w in best_workers if w]


class BlastRadiusSelector:
    def __init__(
        self,
        map_data: dict[str, Any],
        tests_list: list[dict[str, Any]],
        config: dict[str, float] | None = None,
    ) -> None:
        self.m = map_data
        self.cfg = dict(DEFAULT_CONFIG)
        if config:
            self.cfg.update(config)

        # Runtime test metadata from /data/tests.json
        self.tests = tests_list
        self.valid_test_nums: set[int] = {t["number"] for t in tests_list}
        self.dur_ms: dict[int, int] = {t["number"]: _duration_ms(t["seconds"]) for t in tests_list}
        self.dur_sec: dict[int, float] = {t["number"]: t["seconds"] for t in tests_list}
        self.id_to_num: dict[str, int] = {t["id"]: t["number"] for t in tests_list}
        self.num_to_id: dict[int, str] = {t["number"]: t["id"] for t in tests_list}
        self.all_test_nums: list[int] = sorted(self.valid_test_nums)

        # Build-time per-test profile toggles (every phase independently toggleable for ablation)
        self.use_expr_profiles: bool = (
            _expr_profiler_mod is not None
            and os.environ.get("BLAST_DISABLE_PROFILES", "0") != "1"
        )
        self.enable_p1_reach: bool = os.environ.get("BLAST_P1_REACH", "1") != "0"
        self.enable_p1_multisite: bool = os.environ.get("BLAST_P1_MULTISITE", "1") != "0"
        self.enable_p1_root_init: bool = os.environ.get("BLAST_P1_ROOT_INIT", "1") != "0"
        self.enable_p2_mappings: bool = os.environ.get("BLAST_P2_MAPPINGS", "1") != "0"
        self.enable_p3_equiv: bool = os.environ.get("BLAST_P3_EQUIV", "1") != "0"
        self.enable_p4_ranges: bool = os.environ.get("BLAST_P4_RANGES", "1") != "0"

        # Optional hybrid in-container verifier mode:
        #   "off"          - no test execution (lowest CPU)
        #   "single_cheap" - only verify a single cheap candidate (<= 0.15 s) to remove 2.0 s worker cost
        #   "full"         - full v3 economic-gated verifier
        # Legacy BLAST_DISABLE_VERIFIER=1 forces "off".
        if os.environ.get("BLAST_DISABLE_VERIFIER", "0") == "1":
            self.verifier_mode: str = "off"
        else:
            self.verifier_mode = os.environ.get("BLAST_VERIFIER_MODE", "off").strip().lower()
        self.single_cheap_max_sec: float = float(os.environ.get("BLAST_SINGLE_CHEAP_MAX_SEC", "0.15"))

        self.verifier: Any = None
        if self.verifier_mode in ("single_cheap", "full") and DEFAULT_SQLGLOT_DIR.is_dir():
            global _verifier_mod
            if _verifier_mod is None:
                try:
                    import verifier as _vmod

                    _verifier_mod = _vmod
                except ImportError:
                    _verifier_mod = None
            if _verifier_mod is not None:
                v = _verifier_mod.HybridVerifier(DEFAULT_SQLGLOT_DIR)
                if v.available:
                    self.verifier = v

        # Map internal bit indices (0..N-1) to /data/tests.json test numbers
        map_test_ids: list[str] = self.m["test_ids"]
        self.idx_to_num: list[int | None] = [self.id_to_num.get(tid) for tid in map_test_ids]
        self.all_map_bits: int = (1 << len(map_test_ids)) - 1

        # Any test present in tests.json but missing from map (defensive safety)
        mapped_nums = {n for n in self.idx_to_num if n is not None}
        self.unmapped_runtime_tests: list[int] = sorted(self.valid_test_nums - mapped_nums)

        # Convenience references to map tables
        self.line_to_tests: dict[str, dict[int, int]] = self.m["line_to_tests"]
        self.func_to_tests: dict[str, dict[str, int]] = self.m["func_to_tests"]
        self.class_to_tests: dict[str, dict[str, int]] = self.m["class_to_tests"]
        self.module_exec_tests: dict[str, int] = self.m["module_exec_tests"]
        self.fixture_to_tests: dict[str, int] = self.m["fixture_to_tests"]
        self.dialect_to_tests: dict[str, int] = self.m["dialect_to_tests"]
        self.expr_to_tests: dict[str, int] = self.m["expr_to_tests"]
        self.test_file_to_tests: dict[str, int] = self.m["test_file_to_tests"]
        self.file_to_module: dict[str, str] = self.m["file_to_module"]
        self.module_to_file: dict[str, str] = self.m["module_to_file"]
        self.module_importers: dict[str, list[str]] = self.m["module_importers"]
        self.ast_index: dict[str, dict[str, Any]] = self.m["ast_index"]
        self.class_subclasses: dict[str, list[str]] = self.m["class_subclasses"]
        self.expr_class_file: dict[str, str] = self.m["expr_class_file"]
        self.test_file_expr_refs: dict[str, list[str]] = self.m.get("test_file_expr_refs", {})
        self.base_sources: dict[str, bytes] = self.m["base_sources"]
        self.expr_profiles: dict[str, dict[tuple[int, ...], tuple[int, ...]]] = self.m.get("expr_profiles", {})
        self.reach_profiles: dict[str, dict[tuple[int, ...], int]] = self.m.get("reach_profiles", {})
        self.mapping_profiles: dict[tuple[str, str], dict[str, Any]] = self.m.get("mapping_profiles", {})
        self.defined_dict_tables: frozenset[tuple[str, str]] = frozenset(self.m.get("defined_dict_tables", ()))
        self.range_profiles: dict[str, dict[tuple[int, ...], Any]] = self.m.get("range_profiles", {})
        self.const_profiles: dict[str, dict[tuple[int, ...], Any]] = self.m.get("const_profiles", {})
        self.default_arg_profiles: dict[tuple[str, str], tuple[frozenset[str], dict[str, int]]] = self.m.get(
            "default_arg_profiles", {}
        )
        self.import_time_funcs: dict[str, set[str]] = {
            k: set(v) for k, v in self.m.get("import_time_funcs", {}).items()
        }
        self.import_time_func_lines: dict[str, dict[str, frozenset[int]]] = {
            rel: {fq: frozenset(ls) for fq, ls in fdict.items()}
            for rel, fdict in self.m.get("import_time_func_lines", {}).items()
        }
        self.import_time_exprs: frozenset[str] = frozenset(self.m.get("import_time_exprs", ()))
        self.import_time_dict_keys: dict[tuple[str, str], frozenset[Any]] = {
            k: frozenset(v) for k, v in self.m.get("import_time_dict_keys", {}).items()
        }
        self._lazy_importers_baked: bool = bool(self.m.get("lazy_importers_baked", False))
        self._all_expr_cls_ids: frozenset[str] = (
            frozenset(self.class_subclasses.get("sqlglot/expressions/core.py:Expr", ()))
            | {"sqlglot/expressions/core.py:Expr"}
        )

        # Tests that spawn subprocesses or were not profiled in-process -> never drop via unreached
        self._unprofiled_test_bits: int = 0
        unprof_ids = set(self.m.get("unprofiled_test_ids", ()))
        unprof_ids.add("tests/dialects/test_dialect.py::TestDialect::test_lazy_load")
        for tid in unprof_ids:
            try:
                mi = map_test_ids.index(tid)
                self._unprofiled_test_bits |= 1 << mi
            except ValueError:
                pass

        # Cache per-commit patched bytes & AST analyses to avoid duplicate ast.parse
        self._last_commit_py_overlay: dict[str, tuple[bytes | None, bytes | None]] = {}
        self._last_commit_ast_identical: set[str] = set()
        # (rel, old_tree, new_tree, file_probs, scope_clean)
        self._last_commit_source_trees: list[tuple[str, ast.AST, ast.AST, dict[int, float], bool]] = []
        self._src_cache: dict[str, bytes] = {}
        self._mod_ast_cache: dict[str, ast.AST | None] = {}
        self._stmt_spans_cache: dict[str, list[tuple[int, int]]] = {}
        self._lambda_spans_cache: dict[str, list[tuple[int, int]]] = {}
        self._func_node_cache: dict[str, dict[str, ast.AST]] = {}

        # Precompute reverse index: identifier name -> bitset of tests executing
        # any function in sqlglot/ that references that name
        self._name_ref_tests_cache: dict[Any, int] = {}
        self._name_to_locations: dict[str, list[tuple[str, str]]] = {}
        self._all_referenced_names: set[str] | None = None
        self._lazy_importers_ready: bool = False
        self._metaclass_compiled_attrs: set[str] | None = None
        self._augmented_module_importers: dict[str, set[str]] | None = None
        self._importer_tests_cache: dict[str, int] = {}
        self._test_name_refs_cache: dict[str, int] | None = None
        for rel, info in self.ast_index.items():
            if not rel.startswith("sqlglot/"):
                continue
            for name, qnames in info["names_referenced_by"].items():
                lst = self._name_to_locations.setdefault(name, [])
                for qn in qnames:
                    lst.append((rel, qn))

        # Precompute docs test bits / runtime numbers (for docstring & added-public-func safety)
        self._docs_test_bits: int = self.test_file_to_tests.get("tests/test_docs.py", 0)
        self._docs_runtime_nums: list[int] = sorted(
            t["number"] for t in tests_list if t["id"].startswith("tests/test_docs.py")
        )
        self._last_commit_extra_nums: set[int] = set()

        # Precompute bitset of tests that load fixtures from each fixture subdir
        self._fixture_dir_tests: dict[str, int] = {}
        self._all_fixture_tests: int = 0
        for frel, bits in self.fixture_to_tests.items():
            if frel.startswith("tests/fixtures/"):
                self._all_fixture_tests |= bits
                parent = frel.rsplit("/", 1)[0]
                self._fixture_dir_tests[parent] = self._fixture_dir_tests.get(parent, 0) | bits

        # Precompute dialect meta-tests (imports, enum, typing)
        self._dialect_meta_bits: int = (
            self.test_file_to_tests.get("tests/test_dialect_imports.py", 0)
            | self.test_file_to_tests.get("tests/test_dialect_entry_points.py", 0)
            | self.test_file_to_tests.get("tests/test_typing.py", 0)
        )
        for tid in (
            "tests/dialects/test_dialect.py::TestDialect::test_enum",
            "tests/dialects/test_dialect.py::TestDialect::test_lazy_load",
        ):
            if tid in self.id_to_num:
                # Find map index
                try:
                    mi = map_test_ids.index(tid)
                    self._dialect_meta_bits |= 1 << mi
                except ValueError:
                    pass

        # Precompute per-test-file test nodeid -> map bit for surgical test-file edits
        self._test_method_bits: dict[str, dict[str, int]] = {}
        for idx, tid in enumerate(map_test_ids):
            parts = tid.split("::")
            tfile = parts[0]
            if len(parts) >= 2:
                # e.g. "TestParser.test_union" and "test_union"
                short_name = parts[-1]
                qname = ".".join(parts[1:])
                d = self._test_method_bits.setdefault(tfile, {})
                d[qname] = d.get(qname, 0) | (1 << idx)
                d[short_name] = d.get(short_name, 0) | (1 << idx)

    def _get_base_source(self, rel: str) -> bytes | None:
        cached = self._src_cache.get(rel)
        if cached is not None:
            return cached
        comp = self.base_sources.get(rel)
        if comp is None:
            return None
        raw = zlib.decompress(comp)
        self._src_cache[rel] = raw
        return raw

    def _get_module_ast(self, mod_name: str) -> ast.AST | None:
        if mod_name in self._mod_ast_cache:
            return self._mod_ast_cache[mod_name]
        rel = self.module_to_file.get(mod_name)
        tree: ast.AST | None = None
        if rel:
            raw = self._get_base_source(rel)
            if raw is not None:
                try:
                    tree = ast.parse(raw)
                except Exception:
                    tree = None
        self._mod_ast_cache[mod_name] = tree
        return tree

    def _ensure_ast_caches(self, rel: str, pre_parsed: ast.AST | None = None) -> None:
        if rel in self._stmt_spans_cache:
            return
        spans: list[tuple[int, int]] = []
        lambdas: list[tuple[int, int]] = []
        fnodes: dict[str, ast.AST] = {}
        tree = pre_parsed
        if tree is None:
            raw = self._get_base_source(rel)
            if raw is not None:
                try:
                    tree = ast.parse(raw)
                except Exception:
                    tree = None
        if tree is not None:
            scope: list[str] = []

            def walk_defs(body: list[ast.stmt]) -> None:
                for st in body:
                    if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        qn = ".".join((*scope, st.name))
                        fnodes[qn] = st
                        scope.append(st.name)
                        walk_defs(st.body)
                        scope.pop()
                    elif isinstance(st, ast.ClassDef):
                        scope.append(st.name)
                        walk_defs(st.body)
                        scope.pop()

            walk_defs(tree.body)
            for node in ast.walk(tree):
                if isinstance(node, ast.stmt) and not isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    s = getattr(node, "lineno", None)
                    e = getattr(node, "end_lineno", None)
                    if s is not None and e is not None:
                        spans.append((s, e))
                elif isinstance(node, ast.Lambda):
                    s = getattr(node, "lineno", None)
                    e = getattr(node, "end_lineno", None)
                    if s is not None and e is not None:
                        lambdas.append((s, e))
        self._stmt_spans_cache[rel] = spans
        self._lambda_spans_cache[rel] = lambdas
        self._func_node_cache[rel] = fnodes

    def _enclosing_stmt_lines(self, rel: str, lineno: int) -> tuple[int, int] | None:
        """Find the narrowest non-def/non-class statement span containing lineno."""
        self._ensure_ast_caches(rel)
        best: tuple[int, int] | None = None
        for s, e in self._stmt_spans_cache[rel]:
            if s <= lineno <= e:
                if best is None or (e - s) < (best[1] - best[0]):
                    best = (s, e)
        return best

    def _line_in_lambda(self, rel: str, lineno: int) -> bool:
        self._ensure_ast_caches(rel)
        return any(s <= lineno <= e for s, e in self._lambda_spans_cache[rel])

    @staticmethod
    def _has_future_annotations(tree: ast.AST | None) -> bool:
        """Return True iff the module activates `from __future__ import annotations`."""
        if not isinstance(tree, ast.Module):
            return False
        for st in tree.body:
            if isinstance(st, ast.ImportFrom) and st.module == "__future__":
                if any(a.name == "annotations" for a in st.names):
                    return True
            elif not (
                isinstance(st, ast.Expr)
                and isinstance(st.value, ast.Constant)
                and isinstance(st.value.value, str)
            ):
                # __future__ imports must precede non-docstring statements
                break
        return False

    @staticmethod
    def _func_scope_signature(node: ast.AST) -> tuple[str, str, str, str, str | None, frozenset[str], bool]:
        """Extract compile-time properties of a function that affect the whole body:
        arguments/defaults, return annotation, type params, decorators, docstring,
        locally bound names, and yield flag."""
        assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        args_dump = ast.dump(node.args, include_attributes=False)
        ret_dump = ast.dump(node.returns, include_attributes=False) if node.returns is not None else ""
        tp_nodes = getattr(node, "type_params", ())
        tp_dump = "|".join(ast.dump(tp, include_attributes=False) for tp in tp_nodes) if tp_nodes else ""
        decs_dump = "|".join(ast.dump(d, include_attributes=False) for d in node.decorator_list)
        doc = ast.get_docstring(node, clean=False)
        bound: set[str] = set()
        has_yield = False
        # Walk only this function's own scope (do not descend into nested def/class)
        stack: list[ast.AST] = list(node.body)
        while stack:
            cur = stack.pop()
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                bound.add(cur.name)
                continue
            if isinstance(cur, ast.ClassDef):
                bound.add(cur.name)
                continue
            if isinstance(cur, (ast.Yield, ast.YieldFrom)):
                has_yield = True
            elif isinstance(cur, (ast.Global, ast.Nonlocal)):
                bound.update(cur.names)
            elif isinstance(cur, ast.Name) and isinstance(cur.ctx, (ast.Store, ast.Del)):
                bound.add(cur.id)
            elif isinstance(cur, ast.ExceptHandler) and cur.name:
                bound.add(cur.name)
            elif isinstance(cur, (ast.Import, ast.ImportFrom)):
                for a in cur.names:
                    bound.add(a.asname or a.name.split(".")[0])
            stack.extend(ast.iter_child_nodes(cur))
        return (args_dump, ret_dump, tp_dump, decs_dump, doc, frozenset(bound), has_yield)

    def _func_scope_changed(self, rel: str, fq: str, new_tree: ast.AST | None) -> bool:
        """Return True if signature, decorators, local bindings, or yield-ness changed."""
        if new_tree is None:
            return True
        self._ensure_ast_caches(rel)
        old_node = self._func_node_cache[rel].get(fq)
        if old_node is None:
            return True
        # Locate matching qualname in new_tree
        parts = fq.split(".")
        cur_body: list[ast.stmt] = getattr(new_tree, "body", [])
        new_node: ast.AST | None = None
        for idx, part in enumerate(parts):
            found = None
            for st in cur_body:
                if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and st.name == part:
                    found = st
                    break
            if found is None:
                return True
            if idx == len(parts) - 1:
                new_node = found
            else:
                cur_body = getattr(found, "body", [])
        if not isinstance(new_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return True
        return self._func_scope_signature(old_node) != self._func_scope_signature(new_node)

    def _func_reads_dict_values(self, rel: str, qn: str, attr_name: str) -> bool:
        """Return True if function `qn` in `rel` may read values from dict `attr_name`
        (as opposed to only testing truthiness `bool(d)` / `if d:` or key membership `k in d`)."""
        self._ensure_ast_caches(rel)
        fnode = self._func_node_cache.get(rel, {}).get(qn)
        if fnode is None:
            return True

        # Build parent map for this function AST
        parent_map: dict[ast.AST, ast.AST] = {}
        for p in ast.walk(fnode):
            for ch in ast.iter_child_nodes(p):
                parent_map[ch] = p

        # Collect direct references to attr_name AND any simple local aliases (`x = self.attr_name`)
        tracked_names: set[str] = {attr_name}
        for node in ast.walk(fnode):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                val = node.value
                if (isinstance(val, ast.Attribute) and val.attr == attr_name) or (
                    isinstance(val, ast.Name) and val.id == attr_name
                ):
                    tracked_names.add(node.targets[0].id)

        def _is_truthiness_or_key_check(expr: ast.AST) -> bool:
            p = parent_map.get(expr)
            if p is None:
                return False
            # Assignment into a tracked local alias is safe (we check all uses of the alias too)
            if isinstance(p, ast.Assign) and p.value is expr:
                if len(p.targets) == 1 and isinstance(p.targets[0], ast.Name) and p.targets[0].id in tracked_names:
                    return True
                return False
            # `if expr:` / `while expr:` / `x if expr else y`
            if isinstance(p, (ast.If, ast.While, ast.IfExp)) and p.test is expr:
                return True
            # `bool(expr)`
            if isinstance(p, ast.Call) and isinstance(p.func, ast.Name) and p.func.id == "bool":
                return True
            # `not expr`
            if isinstance(p, ast.UnaryOp) and isinstance(p.op, ast.Not):
                return True
            # `a and expr` / `expr or b` — only safe if the enclosing BoolOp is itself in truthiness context!
            if isinstance(p, ast.BoolOp):
                return _is_truthiness_or_key_check(p)
            # `k in expr` / `k not in expr`
            if (
                isinstance(p, ast.Compare)
                and len(p.ops) == 1
                and isinstance(p.ops[0], (ast.In, ast.NotIn))
                and len(p.comparators) == 1
                and p.comparators[0] is expr
            ):
                return True
            return False

        for node in ast.walk(fnode):
            is_ref = (
                (isinstance(node, ast.Attribute) and node.attr == attr_name and isinstance(node.ctx, ast.Load))
                or (isinstance(node, ast.Name) and node.id in tracked_names and isinstance(node.ctx, ast.Load))
            )
            if is_ref:
                if not _is_truthiness_or_key_check(node):
                    return True
        return False

    def _tests_referencing_name(
        self,
        name: str,
        *,
        skip_plumbing_ctors: bool = False,
        dict_values_only: bool = False,
        also_lower: bool = False,
    ) -> int:
        cache_key = (name, skip_plumbing_ctors, dict_values_only, also_lower)
        cached = self._name_ref_tests_cache.get(cache_key)
        if cached is not None:
            return cached
        names_to_check = (name, name.lower()) if (also_lower and name.isupper() and name.lower() != name) else (name,)
        runtime_bits = 0
        ctor_bits = 0
        for nm in names_to_check:
            for rel, qn in self._name_to_locations.get(nm, ()):
                # Only runtime functions/methods (exclude class-body definitions which are writes)
                fb = self.func_to_tests.get(rel, {}).get(qn, 0)
                if not fb:
                    continue
                short = qn.rsplit(".", 1)[-1]
                if skip_plumbing_ctors and short in ("__init__", "__new__", "_init_core"):
                    ctor_bits |= fb
                else:
                    if dict_values_only and not self._func_reads_dict_values(rel, qn, nm):
                        continue
                    runtime_bits |= fb
        bits = runtime_bits if runtime_bits else ctor_bits
        self._name_ref_tests_cache[cache_key] = bits
        return bits

    def _is_metaclass_compiled_attr(self, attr_name: str) -> bool:
        """Return True iff `attr_name` is read in a class-creation hook (`__new__` or
        `__init_subclass__`), meaning runtime code may read derived compiled attributes
        rather than `attr_name` directly."""
        if self._metaclass_compiled_attrs is None:
            compiled: set[str] = set()
            for nm, locs in self._name_to_locations.items():
                if any(qn.endswith((".__init_subclass__", ".__new__")) for _, qn in locs):
                    compiled.add(nm)
            self._metaclass_compiled_attrs = compiled
        return attr_name in self._metaclass_compiled_attrs

    def _is_import_time_referenced_name(self, name: str, *, also_lower: bool = False) -> bool:
        """Return True iff `name` (or its lowercase variant) is referenced inside any
        function recorded in `import_time_funcs`."""
        if not self.import_time_funcs:
            return False
        names = (name, name.lower()) if (also_lower and name.isupper() and name.lower() != name) else (name,)
        for nm in names:
            for loc_rel, qn in self._name_to_locations.get(nm, ()):
                if qn in self.import_time_funcs.get(loc_rel, ()):
                    return True
        return False

    def _ensure_lazy_importers(self) -> dict[str, Any]:
        """Augment static `module_importers` with PEP 562 `__getattr__` lazy-import edges
        (e.g., `from sqlglot.dialects import BigQuery` in `tests/dialects/test_dialect.py`
        transitively imports `sqlglot.dialects.bigquery`, `sqlglot.typing.bigquery`, etc.)."""
        if self._lazy_importers_baked:
            return self.module_importers
        if self._augmented_module_importers is not None:
            return self._augmented_module_importers

        aug: dict[str, set[str]] = {m: set(v) for m, v in self.module_importers.items()}

        # 1. Extract lazy attribute -> target submodule mappings from PEP 562 packages
        lazy_pkg_maps: dict[str, dict[str, str]] = {}

        d_tree = self._get_module_ast("sqlglot.dialects")
        if isinstance(d_tree, ast.Module):
            dmap: dict[str, str] = {
                "Dialect": "sqlglot.dialects.dialect",
                "Dialects": "sqlglot.dialects.dialect",
            }
            for st in d_tree.body:
                if (
                    isinstance(st, ast.Assign)
                    and len(st.targets) == 1
                    and isinstance(st.targets[0], ast.Name)
                    and st.targets[0].id == "DIALECTS"
                    and isinstance(st.value, (ast.List, ast.Tuple))
                ):
                    for elt in st.value.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            dmap[elt.value] = f"sqlglot.dialects.{elt.value.lower()}"
            lazy_pkg_maps["sqlglot.dialects"] = dmap

        o_tree = self._get_module_ast("sqlglot.optimizer")
        if isinstance(o_tree, ast.Module):
            omap: dict[str, str] = {}
            for st in o_tree.body:
                val = None
                if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name) and st.targets[0].id == "_LAZY_ATTRS":
                    val = st.value
                elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) and st.target.id == "_LAZY_ATTRS":
                    val = st.value
                if isinstance(val, ast.Dict):
                    for k, v in zip(val.keys, val.values):
                        if (
                            isinstance(k, ast.Constant)
                            and isinstance(k.value, str)
                            and isinstance(v, ast.Tuple)
                            and v.elts
                            and isinstance(v.elts[0], ast.Constant)
                            and isinstance(v.elts[0].value, str)
                        ):
                            omap[k.value] = v.elts[0].value
            lazy_pkg_maps["sqlglot.optimizer"] = omap

        # 2. Discover direct lazy `from <pkg> import <attr>` edges (bytes pre-filter avoids
        #    parsing hundreds of unrelated transitive importers!)
        new_edges: list[tuple[str, str]] = []  # (importer_mod, target_mod)
        for pkg, amap in lazy_pkg_maps.items():
            needle = f"from {pkg} import".encode("ascii")
            for imp_mod in list(aug.get(pkg, ())):
                imp_rel = self.module_to_file.get(imp_mod)
                if not imp_rel:
                    continue
                raw = self._get_base_source(imp_rel)
                if raw is None or needle not in raw:
                    continue
                itree = self._get_module_ast(imp_mod)
                if not isinstance(itree, ast.Module):
                    continue
                for st in itree.body:
                    if isinstance(st, ast.ImportFrom) and st.module == pkg and st.level == 0:
                        for alias in st.names:
                            if alias.name == "*":
                                for tm in amap.values():
                                    new_edges.append((imp_mod, tm))
                            elif alias.name in amap:
                                new_edges.append((imp_mod, amap[alias.name]))
                            else:
                                cand_sub = f"{pkg}.{alias.name}"
                                if cand_sub in self.module_to_file:
                                    new_edges.append((imp_mod, cand_sub))

        if new_edges:
            # Build reverse dependency index: mod -> all modules `dep` such that `mod` imports `dep`
            imports_of: dict[str, set[str]] = {}
            for dep, imps in aug.items():
                for im in imps:
                    imports_of.setdefault(im, set()).add(dep)

            for imp_mod, target_mod in new_edges:
                # Everyone who imports imp_mod (plus imp_mod itself) now transitively imports
                # target_mod and everything target_mod transitively imports.
                upstream = {imp_mod} | aug.get(imp_mod, set())
                downstream = {target_mod} | imports_of.get(target_mod, set())
                for dep in downstream:
                    aug.setdefault(dep, set()).update(upstream)

        self._augmented_module_importers = aug
        return aug

    def _importer_tests(self, rel: str) -> int:
        """All tests in files that transitively import `rel` (plus direct exec/dialect tests)."""
        cached = self._importer_tests_cache.get(rel)
        if cached is not None:
            return cached
        bits = self.module_exec_tests.get(rel, 0) | self.dialect_to_tests.get(rel, 0)
        mod = self.file_to_module.get(rel)
        if mod:
            importers_map = self._ensure_lazy_importers()
            for imp_mod in importers_map.get(mod, ()):
                imp_rel = self.module_to_file.get(imp_mod)
                if not imp_rel:
                    continue
                if imp_rel.startswith("tests/"):
                    bits |= self.test_file_to_tests.get(imp_rel, 0)
                else:
                    bits |= self.module_exec_tests.get(imp_rel, 0)
                    bits |= self.dialect_to_tests.get(imp_rel, 0)
        self._importer_tests_cache[rel] = bits
        return bits

    def _expr_used_at_import(self, rel: str, cls_qname: str) -> bool:
        """Return True iff `cls_qname` or any transitive subclass was instantiated during
        module import / test collection."""
        short = cls_qname.split(".")[-1]
        if self.import_time_exprs:
            if short in self.import_time_exprs:
                return True
            cls_id = f"{rel}:{cls_qname}"
            for sub_id in self.class_subclasses.get(cls_id, ()):
                sub_short = sub_id.split(":", 1)[1].split(".")[-1]
                if sub_short in self.import_time_exprs:
                    return True
            return False
        return self._is_import_time_referenced_name(short)

    def _test_method_refs_for_name(self, name: str) -> int:
        """All test methods in `tests/` whose AST statically references `name`
        (catches direct subclass instantiation such as `BigQuery.Tokenizer()`
        in `tests/test_tokens.py::TestTokens::test_jinja` that bypasses
        `Dialect.get_or_raise`)."""
        if self._test_name_refs_cache is None:
            cache: dict[str, int] = {}
            for tref, info in self.ast_index.items():
                if not tref.startswith("tests/"):
                    continue
                mbits = self._test_method_bits.get(tref)
                if not mbits:
                    continue
                for nm, qns in info.get("names_referenced_by", {}).items():
                    matched = 0
                    for qn in qns:
                        if qn in mbits:
                            matched |= mbits[qn]
                    if matched:
                        cache[nm] = cache.get(nm, 0) | matched
            self._test_name_refs_cache = cache
        return self._test_name_refs_cache.get(name, 0)

    def _subclass_dialect_tests(self, rel: str, cls_qname: str) -> int:
        """Dialect & class tests for cls_qname and all its transitive subclasses."""
        _GENERIC_BASE_NAMES = frozenset({"Dialect", "Tokenizer", "Parser", "Generator", "Expr", "_TokenizerBase"})
        bits = self.dialect_to_tests.get(rel, 0)
        bits |= self.class_to_tests.get(rel, {}).get(cls_qname, 0)
        top = cls_qname.split(".", 1)[0]
        if top and top not in _GENERIC_BASE_NAMES:
            bits |= self._test_method_refs_for_name(top)
        cls_id = f"{rel}:{cls_qname}"
        for sub_id in self.class_subclasses.get(cls_id, ()):
            sub_rel, sub_q = sub_id.split(":", 1)
            bits |= self.dialect_to_tests.get(sub_rel, 0)
            bits |= self.class_to_tests.get(sub_rel, {}).get(sub_q, 0)
            sub_top = sub_q.split(".", 1)[0]
            if sub_top and sub_top not in _GENERIC_BASE_NAMES:
                bits |= self._test_method_refs_for_name(sub_top)
        return bits

    def _expr_hierarchy_tests(self, rel: str, cls_qname: str) -> int:
        """All tests instantiating or method-referencing an Expr class or its subclasses."""
        def _method_refs_for(expr_name: str) -> int:
            rbits = 0
            for tref in self.test_file_expr_refs.get(expr_name, ()):
                qns = self.ast_index.get(tref, {}).get("names_referenced_by", {}).get(expr_name, ())
                mbits = self._test_method_bits.get(tref, {})
                matched = 0
                for qn in qns:
                    if qn in mbits:
                        matched |= mbits[qn]
                rbits |= matched if matched else self.test_file_to_tests.get(tref, 0)
            return rbits

        short = cls_qname.split(".")[-1]
        bits = self.expr_to_tests.get(short, 0) | _method_refs_for(short)
        cls_id = f"{rel}:{cls_qname}"
        for sub_id in self.class_subclasses.get(cls_id, ()):
            sub_q = sub_id.split(":", 1)[1]
            sub_short = sub_q.split(".")[-1]
            bits |= self.expr_to_tests.get(sub_short, 0) | _method_refs_for(sub_short)
        return bits

    def _add_prob(self, probs: dict[int, float], bitset: int, p: float) -> None:
        if p <= 0.0 or bitset == 0:
            return
        for idx in _iter_set_bits(bitset):
            prev = probs.get(idx, 0.0)
            if p > prev:
                probs[idx] = p

    def _select_for_python_source(
        self,
        fp: pp.FilePatch,
        cat: str,
        probs: dict[int, float],
    ) -> None:
        cfg = self.cfg
        rel = fp.base_path

        # Binary, deleted, or renamed source file -> wide importer selection
        if fp.is_binary or fp.is_deleted or fp.is_rename:
            imp = self._importer_tests(rel)
            if imp:
                bits = imp | self._dialect_meta_bits | self._docs_test_bits
                if rel.startswith("sqlglot/typing/"):
                    bits |= self.test_file_to_tests.get("tests/test_typing.py", 0)
            else:
                bits = self.all_map_bits
            self._add_prob(probs, bits, cfg["p_direct"])
            if self._docs_runtime_nums:
                self._last_commit_extra_nums.update(self._docs_runtime_nums)
            return

        # New file in sqlglot/ -> importers of its package __init__.py + meta/docs tests
        # (falls back to all tests if the package __init__.py itself is new/unmapped!)
        if fp.is_new or rel not in self.base_sources:
            pkg_dir = rel.rsplit("/", 1)[0] if "/" in rel else ""
            pkg_init = f"{pkg_dir}/__init__.py" if pkg_dir else "sqlglot/__init__.py"
            imp = self._importer_tests(pkg_init)
            if imp:
                bits = imp | self._dialect_meta_bits | self._docs_test_bits
                if rel.startswith("sqlglot/typing/"):
                    bits |= self.test_file_to_tests.get("tests/test_typing.py", 0)
            else:
                bits = self.all_map_bits
            self._add_prob(probs, bits, cfg["p_direct"])
            if self._docs_runtime_nums:
                self._last_commit_extra_nums.update(self._docs_runtime_nums)
            return

        # Root sqlglot/__init__.py -> broad (unless proven AST-identical)
        old_bytes = self._get_base_source(rel)
        new_bytes = pp.apply_hunks(old_bytes, fp.hunks) if old_bytes is not None else None
        if old_bytes is None or new_bytes is None:
            # Patch failed to apply cleanly to base source -> conservative fallback
            bits = self._importer_tests(rel)
            self._add_prob(probs, bits if bits else self.all_map_bits, cfg["p_direct"])
            return

        ana = pp.analyze_python_change(old_bytes, new_bytes)
        if ana.ast_identical:
            self._last_commit_ast_identical.add(rel)
        else:
            self._last_commit_py_overlay[rel] = (old_bytes, new_bytes)

        # 1. Syntax error or encoding change -> all importers + dialect tests + meta
        if ana.syntax_error:
            bits = self._importer_tests(rel)
            if rel.startswith(("sqlglot/dialects/", "sqlglot/parsers/", "sqlglot/generators/", "sqlglot/typing/")):
                bits |= self._dialect_meta_bits
            self._add_prob(probs, bits if bits else self.all_map_bits, cfg["p_direct"])
            return

        # 2. Proven AST-identical (pure Python comment / harmless whitespace)
        if ana.ast_identical:
            p_ident = cfg["p_ast_identical"]
            if p_ident > 0.0:
                bits = self.module_exec_tests.get(rel, 0) | self.dialect_to_tests.get(rel, 0)
                self._add_prob(probs, bits if bits else self.all_map_bits, p_ident)
            return

        # 2b. Phase 3: Commit-level equivalence (outside sqlglot/typing/ which inspects
        #     co_varnames and co_consts in test_typing.py)
        if (
            self.enable_p3_equiv
            and ana.old_tree is not None
            and ana.new_tree is not None
            and not rel.startswith("sqlglot/typing/")
            and not ana.changed_imports
            and not ana.changed_all
            and not ana.added_or_removed_public_names
            and not ana.changed_class_attrs
            and not ana.changed_module_vars
        ):
            if pp.is_docstring_only_change(ana.old_tree, ana.new_tree, ana=ana):
                self._last_commit_ast_identical.add(rel)
                if self._docs_test_bits:
                    self._add_prob(probs, self._docs_test_bits, cfg["p_direct"])
                if self._docs_runtime_nums:
                    self._last_commit_extra_nums.update(self._docs_runtime_nums)
                return
            if (
                ana.changed_funcs
                and not ana.changed_class_headers
                and not ana.changed_other_module_code
                and pp.is_safe_local_rename(ana.old_tree, ana.new_tree, ana=ana)
            ):
                self._last_commit_ast_identical.add(rel)
                return

        # 3. Root __init__.py: use function-body scoping ONLY when exclusively existing
        #    non-import-time function bodies changed (e.g. transpile, tokenize); if any
        #    changed function runs during module import (e.g. parse_one) or import_time_funcs
        #    is unavailable, conservatively select all tests.
        if cat == "root_init":
            imp_fns = self.import_time_funcs.get(rel)
            pure_func_only = (
                self.enable_p1_root_init
                and bool(self.import_time_funcs)
                and bool(ana.changed_funcs)
                and not (imp_fns and any(fq in imp_fns for fq in ana.changed_funcs))
                and not ana.changed_all
                and not ana.added_or_removed_public_names
                and not ana.changed_other_module_code
                and not ana.changed_imports
                and not ana.changed_class_headers
                and not ana.changed_class_attrs
                and not ana.changed_module_vars
            )
            if not pure_func_only:
                self._add_prob(probs, self.all_map_bits, cfg["p_direct"])
                return

        # Phase 3: Narrow added_or_removed_public_names when ONLY unreferenced,
        # undecorated, import-time-pure top-level public functions are added.
        safe_added_public_funcs: set[str] = set()
        if (
            self.enable_p3_equiv
            and ana.added_or_removed_public_names
            and cat == "source"
            and not rel.startswith(("sqlglot/expressions/", "sqlglot/dialects/", "sqlglot/typing/"))
            and ana.old_tree is not None
            and ana.new_tree is not None
            and not ana.changed_all
            and not ana.changed_other_module_code
        ):
            cand_safe = pp.find_safe_added_public_funcs(
                ana.old_tree, ana.new_tree, ana.added_or_removed_public_names
            )
            if cand_safe:
                all_refs = self._all_referenced_names
                if all_refs is None:
                    all_refs = (
                        set(__builtins__.keys()) if isinstance(__builtins__, dict) else set(dir(__builtins__))
                    )
                    all_refs.update(self.test_file_expr_refs.keys())
                    for _info in self.ast_index.values():
                        all_refs.update(_info["names_referenced_by"].keys())
                    self._all_referenced_names = all_refs
                if all(nm not in all_refs for nm in cand_safe):
                    safe_added_public_funcs = cand_safe
                    enum_bits = self._dialect_meta_bits | self._docs_test_bits
                    if enum_bits:
                        self._add_prob(probs, enum_bits, cfg["p_direct"])
                    if self._docs_runtime_nums:
                        self._last_commit_extra_nums.update(self._docs_runtime_nums)

        # 4. Package __init__.py, changed imports, __all__, added/removed public names,
        #    or arbitrary top-level executable statements -> all importers
        if (
            cat == "pkg_init"
            or ana.changed_all
            or (ana.added_or_removed_public_names and not safe_added_public_funcs)
            or ana.changed_other_module_code
        ):
            bits = self._importer_tests(rel)
            if rel.startswith(("sqlglot/dialects/", "sqlglot/parsers/", "sqlglot/generators/", "sqlglot/typing/")):
                bits |= self._dialect_meta_bits
            self._add_prob(probs, bits if bits else self.all_map_bits, cfg["p_direct"])
            return

        if ana.changed_imports:
            bits = self._importer_tests(rel)
            self._add_prob(probs, bits if bits else self.all_map_bits, cfg["p_direct"])

        # 5. Class header / base / metaclass changes -> full class + subclass + importers
        for cq in ana.changed_class_headers:
            bits = self._subclass_dialect_tests(rel, cq) | self._importer_tests(rel)
            if rel.startswith("sqlglot/expressions/"):
                bits |= self._expr_hierarchy_tests(rel, cq)
            self._add_prob(probs, bits if bits else self.all_map_bits, cfg["p_direct"])

        # 6. Function / method body changes
        changed_lines = fp.all_changed_old_lines
        file_lmap = self.line_to_tests.get(rel, {})
        file_fmap = self.func_to_tests.get(rel, {})
        ast_funcs = self.ast_index.get(rel, {}).get("funcs", [])
        func_spans = {qn: (s, e) for s, e, qn in ast_funcs}
        if ana.old_tree is not None and (ana.changed_funcs or ana.changed_class_attrs):
            self._ensure_ast_caches(rel, pre_parsed=ana.old_tree)
        new_tree = ana.new_tree
        has_future_ann = self._has_future_annotations(new_tree)
        imp_fns = self.import_time_funcs.get(rel, ())
        has_imp_line_data = bool(self.import_time_func_lines)
        imp_fl_by_func = self.import_time_func_lines.get(rel, {})

        any_scope_changed = False
        for fq in ana.changed_funcs:
            if fq in safe_added_public_funcs:
                continue
            ftests = file_fmap.get(fq, 0)
            span = func_spans.get(fq)
            scope_changed = self._func_scope_changed(rel, fq, new_tree)
            if scope_changed:
                any_scope_changed = True

            # Collect lines belonging to this function that were touched
            fn_changed_lines = (
                [ln for ln in changed_lines if span[0] <= ln <= span[1]]
                if span is not None
                else changed_lines
            )
            direct_line_bits = 0
            enclosing_stmt_bits = 0
            fn_stmt_spans: list[tuple[int, int]] = []
            for ln in fn_changed_lines:
                direct_line_bits |= file_lmap.get(ln, 0)
                st_span = self._enclosing_stmt_lines(rel, ln)
                if st_span is not None:
                    fn_stmt_spans.append(st_span)
                    # Include tests executing the enclosing statement (handles multiline
                    # boolean conditions / short-circuiting / else branches)
                    for sln in range(st_span[0], st_span[1] + 1):
                        enclosing_stmt_bits |= file_lmap.get(sln, 0)

            # Precise First-Divergence import/collection-time check:
            # A function called during module import / pytest collection can only alter
            # import-time behaviour if (a) its compile-time signature/decorators/defaults/
            # annotations/locals/docstring/yield-ness changed (`scope_changed`), OR (b) at
            # least one changed line or its enclosing statement actually executed during
            # import/collection (`imp_fl`). Otherwise import execution cannot diverge and
            # normal test-body coverage + profile provers apply soundly.
            if has_imp_line_data:
                imp_fl = imp_fl_by_func.get(fq)
                if imp_fl:
                    imp_line_hit = (
                        scope_changed
                        or any(ln in imp_fl for ln in fn_changed_lines)
                        or any(sln in imp_fl for s, e in fn_stmt_spans for sln in range(s, e + 1))
                    )
                    if imp_line_hit:
                        imp_bits = self._importer_tests(rel)
                        self._add_prob(probs, imp_bits if imp_bits else self.all_map_bits, cfg["p_direct"])
                        any_scope_changed = True
                        continue
            elif fq in imp_fns:
                # Fallback when `import_time_func_lines` is absent on an older map
                imp_bits = self._importer_tests(rel)
                self._add_prob(probs, imp_bits if imp_bits else self.all_map_bits, cfg["p_direct"])
                any_scope_changed = True
                continue

            if scope_changed:
                # Without `from __future__ import annotations`, parameter and return
                # annotations are evaluated at import/definition time, so a broken
                # annotation fails all importers of this module at import time.
                if not has_future_ann:
                    imp_bits = self._importer_tests(rel)
                    self._add_prob(probs, imp_bits if imp_bits else self.all_map_bits, cfg["p_direct"])
                    continue

                # Signature, decorator, local variable binding, or yield-ness changed:
                # whole function must be treated as directly impacted — EXCEPT Phase 4c:
                # if ONLY pure default argument value(s) changed on a profiled undecorated
                # function, keep only tests whose own profile used one of the changed defaults.
                if (
                    ftests
                    and self.use_expr_profiles
                    and self.enable_p4_ranges
                    and self.default_arg_profiles
                    and _expr_profiler_mod is not None
                    and ana.old_tree is not None
                    and new_tree is not None
                    and not rel.startswith("sqlglot/typing/")
                ):
                    def_prof = self.default_arg_profiles.get((rel, fq))
                    if def_prof is not None:
                        old_fn = pp._find_node_by_qualname(ana.old_tree, fq)
                        new_fn = pp._find_node_by_qualname(new_tree, fq)
                        if old_fn is not None and new_fn is not None:
                            changed_def_params = _expr_profiler_mod.find_default_only_param_edits(
                                old_fn, new_fn, ana.old_tree
                            )
                            if changed_def_params:
                                imp_params, pmap = def_prof
                                if not any(p in imp_params for p in changed_def_params):
                                    used_bits = self._unprofiled_test_bits
                                    for p in changed_def_params:
                                        used_bits |= pmap.get(p, 0)
                                    narrowed = ftests & used_bits
                                    if narrowed:
                                        self._add_prob(probs, narrowed, cfg["p_direct"])
                                    continue

                if ftests:
                    self._add_prob(probs, ftests, cfg["p_direct"])
                else:
                    top_cls = fq.split(".")[0] if "." in fq else ""
                    fallback_bits = self._subclass_dialect_tests(rel, top_cls) if top_cls else 0
                    fallback_bits |= self._tests_referencing_name(fq.split(".")[-1])
                    if not fallback_bits:
                        fallback_bits = self.module_exec_tests.get(rel, 0) | self.dialect_to_tests.get(rel, 0)
                    self._add_prob(probs, fallback_bits if fallback_bits else self.all_map_bits, cfg["p_direct"])
                continue

            if direct_line_bits:
                self._add_prob(probs, direct_line_bits, cfg["p_direct"])
                if enclosing_stmt_bits:
                    self._add_prob(probs, enclosing_stmt_bits, cfg["p_enclosing_stmt"])
                if ftests and cfg["p_enclosing_func"] > 0.0:
                    self._add_prob(probs, ftests, cfg["p_enclosing_func"])
            elif enclosing_stmt_bits:
                self._add_prob(probs, enclosing_stmt_bits, cfg["p_direct"])
                if ftests and cfg["p_enclosing_func"] > 0.0:
                    self._add_prob(probs, ftests, cfg["p_enclosing_func"])
            elif ftests:
                # Zero tests executed the changed line OR its enclosing statement, AND
                # compile-time function scope/signature/decorators are unchanged:
                # the edited statement is dynamically unreachable under the test suite.
                if cfg["p_dead_stmt_func"] > 0.0:
                    self._add_prob(probs, ftests, cfg["p_dead_stmt_func"])
            else:
                # Function itself was never executed directly (e.g. closure builder or new method)
                # Check if any nested function inside this function was executed!
                nested_bits = 0
                prefix_dot = fq + "."
                for other_q, obits in file_fmap.items():
                    if other_q.startswith(prefix_dot):
                        nested_bits |= obits
                if nested_bits:
                    self._add_prob(probs, nested_bits, cfg["p_direct"])
                else:
                    top_cls = fq.split(".")[0] if "." in fq else ""
                    fallback_bits = 0
                    if top_cls:
                        fallback_bits |= self._subclass_dialect_tests(rel, top_cls)
                        if rel.startswith("sqlglot/expressions/"):
                            fallback_bits |= self._expr_hierarchy_tests(rel, top_cls)
                    fallback_bits |= self._tests_referencing_name(fq.split(".")[-1])
                    if not fallback_bits:
                        fallback_bits = self.module_exec_tests.get(rel, 0) | self.dialect_to_tests.get(rel, 0)
                    self._add_prob(probs, fallback_bits if fallback_bits else self.all_map_bits, cfg["p_direct"])

        # 7. Class attribute / declarative table changes (e.g. FUNCTIONS, SHOW_PARSERS,
        #    NUMERIC_ESCAPES, WEEK_OFFSET, arg_types, TRANSFORMS, KEYWORDS)
        for cq, attr_name, dkeys, dict_values_only in ana.changed_class_attrs:
            # Base-class tables in core tokenizer/parser/generator/dialect that are read by
            # import-time functions can break module importers during test collection before
            # per-test line or mapping profiling runs (even when the entry value is a lambda!).
            # When exact `import_time_dict_keys` is present and the edit is a clean, pure,
            # spread-preserving dict key edit, escalate ONLY if a changed/added/removed key
            # was actually looked up at import time (or key set changed on a trie/key-derived table).
            is_core_base = rel in (
                "sqlglot/tokens.py",
                "sqlglot/parser.py",
                "sqlglot/generator.py",
                "sqlglot/dialects/dialect.py",
            )
            if is_core_base and self._is_import_time_referenced_name(attr_name, also_lower=True):
                escalate_base = True
                if (
                    self.import_time_dict_keys
                    and _mapping_profiler_mod is not None
                    and attr_name in _mapping_profiler_mod.TRACKED_CLASS_DICT_ATTRS
                    and ana.old_tree is not None
                    and ana.new_tree is not None
                ):
                    dict_diff = _mapping_profiler_mod.diff_class_dict_typed(
                        ana.old_tree,
                        ana.new_tree,
                        cq,
                        attr_name,
                        resolve_module_tree=self._get_module_ast,
                    )
                    if dict_diff is not None:
                        chg_v, add_k, rem_k, _, _ = dict_diff
                        vals_only = (not add_k) and (not rem_k)
                        key_derived = (
                            attr_name in _mapping_profiler_mod.KEYS_DERIVED_ATTRS
                            or attr_name == _mapping_profiler_mod.TIME_MAPPING_ATTR
                        )
                        if not (key_derived and not vals_only):
                            imp_keys = self.import_time_dict_keys.get((f"{rel}:{cq}", attr_name), frozenset())
                            if not any(k in imp_keys for k in (chg_v | add_k | rem_k)):
                                escalate_base = False
                if escalate_base:
                    imp_bits = self._importer_tests(rel)
                    self._add_prob(probs, imp_bits if imp_bits else self.all_map_bits, cfg["p_direct"])
                    continue

            # Only treat direct line coverage as a runtime hit when the changed line is
            # inside a lambda (avoiding accidental lazy-import first-test hits on class bodies!)
            direct_attr_lines = 0
            for ln in changed_lines:
                if self._line_in_lambda(rel, ln):
                    direct_attr_lines |= file_lmap.get(ln, 0)

            if direct_attr_lines:
                self._add_prob(probs, direct_attr_lines, cfg["p_direct"])
                dial_bits = self._subclass_dialect_tests(rel, cq)
                if dial_bits:
                    self._add_prob(probs, dial_bits, cfg["p_dialect_attr_fallback"])
                continue

            # Expression module class attribute (e.g. TriggerProperties.arg_types vs DType Enum)
            if rel.startswith("sqlglot/expressions/"):
                if f"{rel}:{cq}" not in self._all_expr_cls_ids:
                    # Non-Expr class inside sqlglot/expressions/ (e.g. DType Enum):
                    # enum members feed class-body dicts at import time -> all importers.
                    imp_bits = self._importer_tests(rel)
                    self._add_prob(probs, imp_bits if imp_bits else self.all_map_bits, cfg["p_direct"])
                    continue
                ebits = self._expr_hierarchy_tests(rel, cq)
                # Expr classes constructed during import/collection (e.g. Select) can
                # fail importers during collection if arg_types/required_args change.
                if self._expr_used_at_import(rel, cq):
                    ebits |= self._importer_tests(rel)
                if ebits:
                    self._add_prob(probs, ebits, cfg["p_expr_attr"])
                    # Also include cheap typing/expression metadata checks
                    self._add_prob(
                        probs,
                        self.test_file_to_tests.get("tests/test_typing.py", 0),
                        cfg["p_expr_attr"],
                    )
                else:
                    # Uninstantiated Expr class -> cheap expression/typing tests
                    self._add_prob(
                        probs,
                        self.test_file_to_tests.get("tests/test_expressions.py", 0)
                        | self.test_file_to_tests.get("tests/test_typing.py", 0),
                        cfg["p_module_fallback"],
                    )
                continue

            # Dialect / Parser / Generator / Tokenizer class attribute
            dial_bits = self._subclass_dialect_tests(rel, cq)
            ref_bits = self._tests_referencing_name(
                attr_name,
                skip_plumbing_ctors=True,
                dict_values_only=dict_values_only,
                also_lower=True,
            )
            # Attributes read by __new__ / __init_subclass__ (e.g. QUOTES, IDENTIFIERS,
            # SINGLE_TOKENS, COMMENTS) are compiled into derived attributes at class creation,
            # so intersecting `dial_bits & ref_bits` is unsound for them.
            is_meta_attr = self._is_metaclass_compiled_attr(attr_name)

            # If dict keys are Expr class names (e.g. TRANSFORMS = {exp.Foo: ...}),
            # we can intersect even more tightly with expr_to_tests!
            expr_key_bits = 0
            if dkeys:
                all_keys_are_exprs = True
                for dk in dkeys:
                    if dk in self.expr_to_tests or dk in self.expr_class_file:
                        expr_key_bits |= self.expr_to_tests.get(dk, 0)
                        for tref in self.test_file_expr_refs.get(dk, ()):
                            expr_key_bits |= self.test_file_to_tests.get(tref, 0)
                    else:
                        all_keys_are_exprs = False
                        break
                if not all_keys_are_exprs:
                    expr_key_bits = 0

            if dial_bits:
                if expr_key_bits and (dial_bits & expr_key_bits):
                    primary_bits, primary_p = dial_bits & expr_key_bits, cfg["p_dialect_attr_intersect"]
                    fallback_bits, fallback_p = dial_bits, cfg["p_dialect_attr_fallback"]
                elif not is_meta_attr and ref_bits and (dial_bits & ref_bits):
                    primary_bits, primary_p = dial_bits & ref_bits, cfg["p_dialect_attr_intersect"]
                    fallback_bits, fallback_p = dial_bits, cfg["p_dialect_attr_fallback"]
                else:
                    primary_bits, primary_p = dial_bits, cfg["p_dialect_attr_intersect"]
                    fallback_bits, fallback_p = 0, 0.0
            elif ref_bits:
                primary_bits, primary_p = ref_bits, cfg["p_module_var_ref"]
                fallback_bits, fallback_p = 0, 0.0
            else:
                fb = self.module_exec_tests.get(rel, 0)
                primary_bits, primary_p = (fb if fb else self.all_map_bits), cfg["p_module_fallback"]
                fallback_bits, fallback_p = 0, 0.0

            # Phase 2: Build-time per-test mapping-key lookup prover
            if (
                self.use_expr_profiles
                and self.enable_p2_mappings
                and _mapping_profiler_mod is not None
                and self.mapping_profiles
                and ana.old_tree is not None
                and ana.new_tree is not None
            ):
                cand_attr_bits = primary_bits | fallback_bits
                if cand_attr_bits:
                    proven = _mapping_profiler_mod.prove_unchanged_for_class_dict(
                        rel,
                        cq,
                        attr_name,
                        ana.old_tree,
                        ana.new_tree,
                        cand_attr_bits,
                        class_subclasses=self.class_subclasses,
                        mapping_profiles=self.mapping_profiles,
                        defined_dict_tables=self.defined_dict_tables,
                        unprofiled_test_bits=self._unprofiled_test_bits,
                        resolve_module_tree=self._get_module_ast,
                    )
                    if proven:
                        primary_bits &= ~proven
                        fallback_bits &= ~proven

            self._add_prob(probs, primary_bits, primary_p)
            self._add_prob(probs, fallback_bits, fallback_p)

        # 8. Module-level variable / constant changes (e.g. EXPRESSION_METADATA, constants)
        for var_name, dkeys in ana.changed_module_vars:
            direct_var_lines = 0
            for ln in changed_lines:
                direct_var_lines |= file_lmap.get(ln, 0)
            if direct_var_lines:
                self._add_prob(probs, direct_var_lines, cfg["p_direct"])

            # Special principled handling for sqlglot/typing/*.py EXPRESSION_METADATA
            if rel.startswith("sqlglot/typing/") and dkeys:
                self._add_prob(
                    probs,
                    self.test_file_to_tests.get("tests/test_typing.py", 0),
                    cfg["p_direct"],
                )
                dial_bits = self.dialect_to_tests.get(rel, 0)
                expr_bits = 0
                for dk in dkeys:
                    expr_bits |= self.expr_to_tests.get(dk, 0)
                if dial_bits and expr_bits and (dial_bits & expr_bits):
                    self._add_prob(probs, dial_bits & expr_bits, cfg["p_dialect_attr_intersect"])
                    self._add_prob(probs, dial_bits, cfg["p_dialect_attr_fallback"])
                else:
                    self._add_prob(probs, dial_bits if dial_bits else self._importer_tests(rel), cfg["p_module_var_ref"])
                continue

            # General module constant/variable: find all functions referencing var_name
            ref_bits = self._tests_referencing_name(var_name)
            if ref_bits:
                self._add_prob(probs, ref_bits, cfg["p_module_var_ref"])
            else:
                fb = self._importer_tests(rel)
                self._add_prob(probs, fb if fb else self.all_map_bits, cfg["p_module_var_ref"])

        # Safety net: if AST was NOT identical, yet none of the buckets above matched
        if (
            not ana.changed_funcs
            and not ana.changed_class_attrs
            and not ana.changed_module_vars
            and not ana.changed_class_headers
            and not ana.changed_imports
        ):
            fb = self._importer_tests(rel)
            self._add_prob(probs, fb if fb else self.all_map_bits, cfg["p_direct"])
            return

        # Record clean function-body-only source edits for build-time expression-site proving
        expr_provable_file = (
            ana.old_tree is not None
            and ana.new_tree is not None
            and bool(ana.changed_funcs)
            and not any_scope_changed
            and not ana.changed_class_attrs
            and not ana.changed_module_vars
            and not ana.changed_class_headers
            and not ana.changed_imports
            and not ana.changed_all
            and not ana.added_or_removed_public_names
            and not ana.changed_other_module_code
        )
        if expr_provable_file:
            self._last_commit_source_trees.append((rel, ana.old_tree, ana.new_tree, probs, True))

    def _select_for_test_file(self, fp: pp.FilePatch, probs: dict[int, float]) -> None:
        cfg = self.cfg
        rel = fp.base_path
        all_in_file = self.test_file_to_tests.get(rel, 0) | self._importer_tests(rel)

        if fp.is_binary or fp.is_deleted or fp.is_rename or fp.is_new or rel not in self.base_sources:
            self._add_prob(probs, all_in_file if all_in_file else self.all_map_bits, cfg["p_direct"])
            return

        old_bytes = self._get_base_source(rel)
        new_bytes = pp.apply_hunks(old_bytes, fp.hunks) if old_bytes is not None else None
        if old_bytes is None or new_bytes is None:
            self._add_prob(probs, all_in_file if all_in_file else self.all_map_bits, cfg["p_direct"])
            return

        ana = pp.analyze_python_change(old_bytes, new_bytes)
        if ana.ast_identical:
            self._last_commit_ast_identical.add(rel)
        else:
            self._last_commit_py_overlay[rel] = (old_bytes, new_bytes)
        if ana.syntax_error:
            self._add_prob(probs, all_in_file if all_in_file else self.all_map_bits, cfg["p_direct"])
            return
        if ana.ast_identical:
            if cfg["p_ast_identical"] > 0.0:
                self._add_prob(probs, all_in_file, cfg["p_ast_identical"])
            return

        # If ONLY specific test_* methods changed (no helpers, setUp, imports, or module vars),
        # select just those exact test methods + any importers of the test file.
        # Guard against intra-file inheritance (editing a method on a base class in the same file).
        if (
            ana.changed_funcs
            and not ana.changed_class_attrs
            and not ana.changed_class_headers
            and not ana.changed_module_vars
            and not ana.changed_imports
            and not ana.changed_all
            and not ana.changed_other_module_code
            and not ana.added_or_removed_public_names
        ):
            subclassed_in_file: set[str] = set()
            if ana.old_tree is not None and isinstance(ana.old_tree, ast.Module):
                cls_in_file = {n.name for n in ana.old_tree.body if isinstance(n, ast.ClassDef)}
                for n in ana.old_tree.body:
                    if isinstance(n, ast.ClassDef):
                        for b in n.bases:
                            bn = b.id if isinstance(b, ast.Name) else (b.attr if isinstance(b, ast.Attribute) else "")
                            if bn in cls_in_file:
                                subclassed_in_file.add(bn)
            mbits = self._test_method_bits.get(rel, {})
            targeted = 0
            all_matched = True
            for fq in ana.changed_funcs:
                owner_cls = fq.split(".", 1)[0] if "." in fq else ""
                if owner_cls and owner_cls in subclassed_in_file:
                    all_matched = False
                    break
                if fq in mbits:
                    targeted |= mbits[fq]
                else:
                    all_matched = False
                    break
            if all_matched and targeted:
                # Also include any tests from OTHER test files that import this file
                mod = self.file_to_module.get(rel)
                if mod:
                    for imp_mod in self.module_importers.get(mod, ()):
                        imp_rel = self.module_to_file.get(imp_mod)
                        if imp_rel and imp_rel != rel:
                            targeted |= self.test_file_to_tests.get(imp_rel, 0)
                self._add_prob(probs, targeted, cfg["p_direct"])
                return

        self._add_prob(probs, all_in_file if all_in_file else self.all_map_bits, cfg["p_direct"])

    def _select_for_test_helper(self, fp: pp.FilePatch, probs: dict[int, float]) -> None:
        cfg = self.cfg
        rel = fp.base_path
        importers = self._importer_tests(rel)

        if fp.is_binary or fp.is_deleted or fp.is_rename or fp.is_new or rel not in self.base_sources:
            self._add_prob(probs, importers if importers else self.all_map_bits, cfg["p_direct"])
            return

        old_bytes = self._get_base_source(rel)
        new_bytes = pp.apply_hunks(old_bytes, fp.hunks) if old_bytes is not None else None
        if old_bytes is None or new_bytes is None:
            self._add_prob(probs, importers if importers else self.all_map_bits, cfg["p_direct"])
            return

        ana = pp.analyze_python_change(old_bytes, new_bytes)
        if ana.ast_identical:
            self._last_commit_ast_identical.add(rel)
        else:
            self._last_commit_py_overlay[rel] = (old_bytes, new_bytes)
        if ana.syntax_error or ana.changed_imports or ana.changed_other_module_code or ana.added_or_removed_public_names:
            self._add_prob(probs, importers if importers else self.all_map_bits, cfg["p_direct"])
            return
        if ana.ast_identical:
            if cfg["p_ast_identical"] > 0.0:
                self._add_prob(probs, importers, cfg["p_ast_identical"])
            return

        if ana.changed_funcs and not ana.changed_module_vars and not ana.changed_class_attrs:
            fmap = self.func_to_tests.get(rel, {})
            bits = 0
            for fq in ana.changed_funcs:
                bits |= fmap.get(fq, 0)
            if bits:
                self._add_prob(probs, bits, cfg["p_direct"])
                return

        self._add_prob(probs, importers if importers else self.all_map_bits, cfg["p_direct"])

    def _select_for_fixture(self, fp: pp.FilePatch, probs: dict[int, float]) -> None:
        cfg = self.cfg
        paths = [p for p in (fp.old_path, fp.new_path) if p] or [fp.base_path]
        bits = 0
        for rel in paths:
            b = self.fixture_to_tests.get(rel, 0)
            if b:
                bits |= b
            else:
                parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
                bits |= self._fixture_dir_tests.get(parent, 0) or self._all_fixture_tests or self.all_map_bits
        self._add_prob(probs, bits if bits else self.all_map_bits, cfg["p_direct"])

    def _build_overlay_for_verifier(
        self,
        parsed: pp.ParsedPatch,
        categories: list[str],
    ) -> tuple[dict[str, tuple[bytes | None, bytes | None]], dict[str, bytes | None], bool, bool]:
        """Build in-memory patched bytes for the hybrid verifier.
        Returns `(patched_py, patched_nonpy, is_fixture_only, overlay_ok)`."""
        patched_py: dict[str, tuple[bytes | None, bytes | None]] = {}
        patched_nonpy: dict[str, bytes | None] = {}
        saw_real_py = False
        saw_fixture = False

        for fp, cat in zip(parsed.files, categories):
            if cat == "docs":
                continue
            if fp.is_binary or fp.is_rename or cat in ("config", "rust_or_c", "other"):
                return {}, {}, False, False
            rel = fp.path
            if rel.endswith(".py") and rel.startswith(("sqlglot/", "tests/")):
                if fp.is_deleted:
                    patched_py[fp.base_path] = (self._get_base_source(fp.base_path), None)
                    saw_real_py = True
                elif fp.is_new:
                    new_b = pp.apply_hunks(b"", fp.hunks)
                    if new_b is None:
                        return {}, {}, False, False
                    patched_py[rel] = (None, new_b)
                    saw_real_py = True
                else:
                    if fp.base_path in self._last_commit_ast_identical:
                        continue
                    cached_pair = self._last_commit_py_overlay.get(fp.base_path)
                    if cached_pair is not None:
                        patched_py[rel] = cached_pair
                        saw_real_py = True
                    else:
                        old_b = self._get_base_source(fp.base_path)
                        if old_b is None:
                            return {}, {}, False, False
                        new_b = pp.apply_hunks(old_b, fp.hunks)
                        if new_b is None:
                            return {}, {}, False, False
                        ana = pp.analyze_python_change(old_b, new_b)
                        if ana.ast_identical:
                            continue
                        patched_py[rel] = (old_b, new_b)
                        saw_real_py = True
            elif cat == "fixture":
                saw_fixture = True
                base_fp = DEFAULT_SQLGLOT_DIR / fp.base_path
                if fp.is_deleted:
                    patched_nonpy[fp.base_path] = None
                elif fp.is_new:
                    new_b = pp.apply_hunks(b"", fp.hunks)
                    if new_b is None:
                        return {}, {}, False, False
                    patched_nonpy[rel] = new_b
                elif base_fp.is_file():
                    old_b = base_fp.read_bytes()
                    new_b = pp.apply_hunks(old_b, fp.hunks)
                    if new_b is None:
                        return {}, {}, False, False
                    patched_nonpy[rel] = new_b
                else:
                    return {}, {}, False, False
            else:
                return {}, {}, False, False

        is_fixture_only = saw_fixture and not saw_real_py
        return patched_py, patched_nonpy, is_fixture_only, True

    def select_test_numbers(self, patch_text: str) -> list[int]:
        """Return sorted list of selected test numbers from tests.json for one patch."""
        parsed = pp.parse_unified_diff(patch_text)

        # Empty patch -> nothing changed
        if parsed.is_empty:
            return []

        # Malformed patch -> conservative broad selection
        if parsed.parse_error or not parsed.files:
            return list(self.all_test_nums)

        # Build work items: for any rename (including cross-category renames such as
        # sqlglot/foo.py -> docs/foo.md or tests/fixtures/a.json -> docs/a.json),
        # classify and evaluate BOTH the removed old_path AND the added new_path so
        # a rename into docs/ or sqlglotc/ can never hide a deleted source/test/fixture.
        work_items: list[tuple[pp.FilePatch, str]] = []
        for fp in parsed.files:
            is_ren = fp.is_rename or bool(fp.old_path and fp.new_path and fp.old_path != fp.new_path)
            if is_ren:
                if fp.old_path:
                    old_fp = pp.FilePatch(
                        old_path=fp.old_path,
                        new_path=None,
                        is_binary=fp.is_binary,
                        is_deleted=True,
                        is_rename=True,
                    )
                    work_items.append((old_fp, pp.classify_file(fp.old_path)))
                if fp.new_path:
                    new_fp = pp.FilePatch(
                        old_path=None,
                        new_path=fp.new_path,
                        is_binary=fp.is_binary,
                        is_new=True,
                        is_rename=True,
                    )
                    work_items.append((new_fp, pp.classify_file(fp.new_path)))
            else:
                work_items.append((fp, pp.classify_file(fp.path)))

        # Docs-only commit -> run nothing!
        categories = [cat for _, cat in work_items]
        if all(c == "docs" for c in categories):
            return []

        probs: dict[int, float] = {}
        force_all = False
        self._last_commit_py_overlay.clear()
        self._last_commit_ast_identical.clear()
        self._last_commit_source_trees.clear()
        self._last_commit_extra_nums.clear()

        per_file_probs: list[tuple[str, str, dict[int, float]]] = []
        _SAFE_SQLGLOTC_FILES = ("sqlglotc/setup.py", "sqlglotc/pyproject.toml", "sqlglotc/MANIFEST.in")

        for fp, cat in work_items:
            if cat == "docs":
                continue
            fprobs: dict[int, float] = {}
            if cat in ("config", "rust_or_c", "other"):
                # Narrow sqlglotc/ ONLY for in-place text edits to the 3 verified packaging
                # files (only tests/test_sqlglotc.py reads sqlglotc/setup.py; verified 0/1617
                # failures elsewhere). Any rename, deletion, binary, C/ext, or new file
                # under sqlglotc/ falls through to conservative full selection.
                if (
                    not fp.is_rename
                    and not fp.is_deleted
                    and not fp.is_binary
                    and fp.path in _SAFE_SQLGLOTC_FILES
                ):
                    bits = self.test_file_to_tests.get("tests/test_sqlglotc.py", 0)
                    if bits:
                        self._add_prob(fprobs, bits, self.cfg["p_direct"])
                        per_file_probs.append((fp.base_path, cat, fprobs))
                        continue
                force_all = True
                break
            elif cat == "fixture":
                self._select_for_fixture(fp, fprobs)
            elif cat == "test":
                self._select_for_test_file(fp, fprobs)
            elif cat == "test_helper":
                self._select_for_test_helper(fp, fprobs)
            elif cat in ("source", "root_init", "pkg_init"):
                self._select_for_python_source(fp, cat, fprobs)
            else:
                force_all = True
                break
            per_file_probs.append((fp.base_path, cat, fprobs))

        if force_all:
            return list(self.all_test_nums)

        # Build-time per-test expression-site & reachability profile proving:
        # - With BLAST_P1_MULTISITE=1: prove per-file on each clean function-body source
        #   file BEFORE taking the union across all files in the commit, so test T is
        #   dropped ONLY IF every modified site in every modified file is unreached or
        #   proven unchanged for T (First-Divergence Induction).
        # - With BLAST_P1_MULTISITE=0: only prove when active_files == 1 and 1 expr diff.
        if self.use_expr_profiles and (self.expr_profiles or self.reach_profiles) and self._last_commit_source_trees:
            active_files = sum(
                1
                for fp, cat in work_items
                if cat != "docs" and fp.base_path not in self._last_commit_ast_identical
            )
            if self.enable_p1_multisite or active_files == 1:
                for rel_p, old_t, new_t, fprobs, scope_clean in self._last_commit_source_trees:
                    if not scope_clean or not fprobs:
                        continue
                    file_profs = self.expr_profiles.get(rel_p)
                    file_reach = self.reach_profiles.get(rel_p)
                    if not file_profs and not file_reach:
                        continue
                    cand_bits = 0
                    for idx in fprobs:
                        cand_bits |= 1 << idx
                    proven = _expr_profiler_mod.prove_unchanged_test_bits(
                        old_t,
                        new_t,
                        file_profs,
                        cand_bits,
                        file_reach_profiles=file_reach,
                        unprofiled_test_bits=self._unprofiled_test_bits,
                        enable_reach=self.enable_p1_reach,
                        enable_multisite=self.enable_p1_multisite,
                        file_range_profiles=self.range_profiles.get(rel_p),
                        file_const_profiles=self.const_profiles.get(rel_p),
                        enable_p4_ranges=self.enable_p4_ranges,
                    )
                    if proven:
                        for idx in _iter_set_bits(proven):
                            fprobs.pop(idx, None)

        for _, _, fprobs in per_file_probs:
            for idx, p in fprobs.items():
                if p > probs.get(idx, 0.0):
                    probs[idx] = p

        if not probs and not self._last_commit_extra_nums:
            return []

        # FR4: Cost-aware inclusion: include test t when P(fail) * 300 > runtime / k_div
        k_div = max(1.0, self.cfg["fr4_k_divisor"])
        selected: set[int] = set(self._last_commit_extra_nums)
        for idx, p in probs.items():
            tnum = self.idx_to_num[idx]
            if tnum is None:
                continue
            if p >= 0.999:
                selected.add(tnum)
                continue
            runtime = self.dur_sec[tnum]
            if p * MISS_PENALTY_SEC > (runtime / k_div):
                selected.add(tnum)

        # If tests.json had any test not present in our map, include it whenever selection is non-empty
        if selected and self.unmapped_runtime_tests:
            selected.update(self.unmapped_runtime_tests)

        selected_sorted = sorted(selected)

        # Optional Hybrid In-Container Verification
        if self.verifier is not None and selected_sorted:
            should_try = True
            if self.verifier_mode == "single_cheap":
                # Only run a single cheap, lightweight test (avoiding duckdb/pandas import cost)
                # to eliminate a 2.00 s worker startup cost (~10:1 payoff ratio).
                if len(selected_sorted) == 1:
                    tnum0 = selected_sorted[0]
                    nid0 = self.num_to_id.get(tnum0, "")
                    should_try = (
                        self.dur_sec[tnum0] <= self.single_cheap_max_sec
                        and not nid0.startswith(("tests/test_optimizer.py::", "tests/test_executor.py::"))
                    )
                else:
                    should_try = False
            if should_try:
                patched_py, patched_nonpy, is_fixture_only, ok = self._build_overlay_for_verifier(
                    parsed, [pp.classify_file(fp.path) for fp in parsed.files]
                )
                if ok and (patched_py or patched_nonpy):
                    cands = [
                        (n, self.num_to_id[n], self.dur_sec[n])
                        for n in selected_sorted
                        if n in self.num_to_id
                    ]
                    selected_sorted = self.verifier.verify_candidates(
                        cands, patched_py, patched_nonpy, is_fixture_only=is_fixture_only
                    )

        return selected_sorted

    def select_and_pack(self, commit_id: str, patch_text: str) -> dict[str, Any]:
        """Run full selection + packing with graceful degradation and strict output validation."""
        try:
            test_nums = self.select_test_numbers(patch_text)
        except Exception as exc:
            print(f"[selector] ERROR on commit {commit_id}: {exc!r}; falling back to all tests", file=sys.stderr)
            test_nums = list(self.all_test_nums)

        try:
            if _packer_mod is not None:
                workers = _packer_mod.pack_tests(test_nums, self.dur_ms)
            else:
                workers = _fallback_pack(test_nums, self.dur_ms)
        except Exception as exc:
            print(f"[selector] PACK ERROR on commit {commit_id}: {exc!r}; using fallback pack", file=sys.stderr)
            workers = _fallback_pack(test_nums, self.dur_ms)

        # Strict final validation (no duplicates, valid IDs, <= 8 non-empty workers)
        seen: set[int] = set()
        clean_workers: list[list[int]] = []
        for w in workers:
            cw = [int(n) for n in w if n in self.valid_test_nums and n not in seen]
            seen.update(cw)
            if cw:
                clean_workers.append(cw)
        if len(clean_workers) > MAX_WORKERS:
            merged = [n for w in clean_workers for n in w]
            clean_workers = _fallback_pack(merged, self.dur_ms)[:MAX_WORKERS]

        return {"commit": commit_id, "workers": clean_workers}


def load_map(
    map_path: Path = DEFAULT_MAP_PATH,
    profiles_path: Path | None = None,
) -> dict[str, Any]:
    data = pickle.loads(zlib.decompress(map_path.read_bytes()))
    if profiles_path is None:
        env_p = os.environ.get("BLAST_PROFILES_PATH")
        if env_p:
            profiles_path = Path(env_p)
        else:
            cand = map_path.parent / "profiles.pkl.gz"
            if cand.is_file():
                profiles_path = cand
    if profiles_path is not None and profiles_path.is_file():
        try:
            prof_data = pickle.loads(zlib.decompress(profiles_path.read_bytes()))
            if not isinstance(prof_data, dict):
                raise ValueError(f"Expected dict in profiles, got {type(prof_data).__name__}")
        except Exception as exc:
            print(
                f"[selector] WARNING: ignoring unreadable profiles {profiles_path}: {exc!r}",
                file=sys.stderr,
            )
            prof_data = None
        # Verify profile commit matches map commit (conservative: ignore on mismatch)
        if prof_data is not None and prof_data.get("commit") == data.get("commit") and prof_data.get("test_ids") == data.get("test_ids"):
            for k in (
                "expr_profiles",
                "reach_profiles",
                "import_time_funcs",
                "unprofiled_test_ids",
                "mapping_profiles",
                "defined_dict_tables",
                "range_profiles",
                "const_profiles",
                "default_arg_profiles",
            ):
                if k in prof_data:
                    data[k] = prof_data[k]
        elif prof_data is not None and "expr_profiles" in prof_data and "test_ids" not in prof_data and prof_data.get("commit") == data.get("commit"):
            for k in (
                "expr_profiles",
                "reach_profiles",
                "import_time_funcs",
                "unprofiled_test_ids",
                "mapping_profiles",
                "defined_dict_tables",
                "range_profiles",
                "const_profiles",
                "default_arg_profiles",
            ):
                if k in prof_data:
                    data[k] = prof_data[k]
    return data


def run_batch(
    data_dir: Path = DATA_DIR,
    map_path: Path = DEFAULT_MAP_PATH,
    config: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    map_data = load_map(map_path)
    tests_list = json.loads((data_dir / "tests.json").read_text("utf-8"))["tests"]
    selector = BlastRadiusSelector(map_data, tests_list, config=config)

    results: list[dict[str, Any]] = []
    commits_dir = data_dir / "commits"
    for patch_path in sorted(commits_dir.glob("*.patch")):
        commit_id = patch_path.name.removesuffix(".patch")
        try:
            patch_text = patch_path.read_text("utf-8", errors="replace")
        except Exception as exc:
            print(f"[selector] READ ERROR on {commit_id}: {exc!r}", file=sys.stderr)
            patch_text = "MALFORMED"
        results.append(selector.select_and_pack(commit_id, patch_text))
    return results


def main() -> None:
    data_dir = Path(os.environ.get("BLAST_DATA_DIR", str(DATA_DIR)))
    map_path = Path(os.environ.get("BLAST_MAP_PATH", str(DEFAULT_MAP_PATH)))
    results = run_batch(data_dir=data_dir, map_path=map_path)
    out_lines = [json.dumps(r, separators=(",", ":")) for r in results]
    sys.stdout.write("\n".join(out_lines) + ("\n" if out_lines else ""))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
