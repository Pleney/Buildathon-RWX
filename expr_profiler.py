"""Build-time per-test expression-site profiler, reachability instrumenter, and
runtime formal soundness prover (Proposal 1: sub-expression reachability +
multi-site conjunction + compare_probe fast path).

Pure Python standard library only.
- At build time (inside build_profiles.py / build_map.py), instruments:
    1. Single-op ast.Compare and If/IfExp/While branch test expressions across
       sqlglot/**/*.py (excluding sqlglot/typing/*) to record per-test observed
       outcomes and operand-type safety bits.
    2. Short-circuitable sub-expressions (BoolOp.values[1:], IfExp.body,
       IfExp.orelse) using an exception-safe pre-evaluation reachability probe
       (`__blast_rm__(sid) or <expr>`, where `__blast_rm__` always returns False).
  Any site hit outside an active test (import/collection time) is excluded, and
  any site unstable across two single-process plain-pytest runs is excluded.
- At runtime (inside selector.py), proves that a file's expression edit(s)
  cannot alter a specific test T's behaviour based STRICTLY on test T's own
  recorded profile (First-Divergence Induction). Any missing/unstable profile,
  nested/overlapping edit, scope change, or uncertainty keeps the test.
"""

from __future__ import annotations

import ast
import enum
import importlib.abc
import importlib.machinery
import os
import sys
from pathlib import Path
from typing import Any

# Per-test per-site bit flags recorded at build time
BIT_FALSE = 1
BIT_TRUE = 2
BIT_EQ = 4
BIT_IS_TO_EQ_UNSAFE = 8
BIT_EQ_TO_IS_UNSAFE = 16
BIT_ORDER_UNSAFE = 32
ALL_COMPARE_BITS = (
    BIT_FALSE
    | BIT_TRUE
    | BIT_EQ
    | BIT_IS_TO_EQ_UNSAFE
    | BIT_EQ_TO_IS_UNSAFE
    | BIT_ORDER_UNSAFE
)
ALL_BRANCH_BITS = BIT_FALSE | BIT_TRUE

# Compare operator codes for fast runtime dispatch inside compare_probe()
OP_IS = 0
OP_IS_NOT = 1
OP_EQ = 2
OP_NOT_EQ = 3
OP_LT = 4
OP_LTE = 5
OP_GT = 6
OP_GTE = 7
OP_IN = 8
OP_NOT_IN = 9

_AST_OP_TO_CODE: dict[type[ast.cmpop], int] = {
    ast.Is: OP_IS,
    ast.IsNot: OP_IS_NOT,
    ast.Eq: OP_EQ,
    ast.NotEq: OP_NOT_EQ,
    ast.Lt: OP_LT,
    ast.LtE: OP_LTE,
    ast.Gt: OP_GT,
    ast.GtE: OP_GTE,
    ast.In: OP_IN,
    ast.NotIn: OP_NOT_IN,
}

_SAFE_BUILTIN_EQ_TYPES = frozenset(
    {type(None), bool, int, float, str, bytes, tuple, list, dict, set, frozenset}
)
_REFL_NON_NONE_BUILTIN = frozenset({type(None), bool, int, str, bytes})
_CONTAINER_OR_FLOAT = frozenset({float, tuple, list, dict, set, frozenset})
_SAFE_ORDER_ATOMS = (int, float, str, bytes)
_IDENTITY_SINGLETON_TYPES = (type(None), bool)


def _is_pure_order_val(val: Any) -> bool:
    """Return True if `val` is a primitive or recursive tuple of primitives with
    pure, deterministic total ordering and equality."""
    t = type(val)
    if t in _SAFE_ORDER_ATOMS:
        if t is float and val != val:
            return False
        return True
    if t is tuple:
        return all(_is_pure_order_val(x) for x in val)
    return False


def _is_safe_eq_type(t: type) -> bool:
    """Return True if type `t` has side-effect-free, deterministic `__eq__`."""
    if t in _SAFE_BUILTIN_EQ_TYPES:
        return True
    if t.__eq__ is object.__eq__:
        return True
    if issubclass(t, enum.Enum):
        return True
    mod = getattr(t, "__module__", "")
    if mod.startswith("sqlglot."):
        return True
    return False


def _is_identity_eq_type(t: type) -> bool:
    """Return True if two instances of `t` satisfy `(a == b) <=> (a is b)` without
    relying on CPython small-int/string interning."""
    if t in _IDENTITY_SINGLETON_TYPES:
        return True
    if t.__eq__ is object.__eq__:
        return True
    if issubclass(t, enum.Enum):
        return True
    return False


# ---------------------------------------------------------------------------
# Build-time AST instrumenter & fast re-entrancy-safe recorder
# ---------------------------------------------------------------------------
class BuildTimeProfileRecorder:
    """Fast per-test expression-site & reachability recorder for build_profiles.py."""

    def __init__(self) -> None:
        # Compare / branch sites: sid -> (rel, lineno, col_offset, end_lineno, end_col_offset, is_compare)
        self.sites: list[tuple[str, int, int, int, int, bool]] = []
        self.cur_flags: list[int] = []
        self.touched_sids: list[int] = []
        self.records: dict[Any, dict[int, int]] = {}
        self.inactive_sids: set[int] = set()

        # Short-circuit reachability sites: rsid -> (rel, lineno, col_offset, end_lineno, end_col_offset)
        self.reach_sites: list[tuple[str, int, int, int, int]] = []
        self.cur_reach: bytearray = bytearray()
        self.touched_rsids: list[int] = []
        self.reach_records: dict[Any, set[int]] = {}
        self.inactive_rsids: set[int] = set()

        self.active: bool = False
        self.in_probe: bool = False
        self.cur_key: Any = None

        # Memoized type safety caches
        self._safe_eq_cache: dict[type, bool] = {}
        self._ident_eq_cache: dict[type, bool] = {}
        self._clean_eq_cache: dict[type, bool] = {}

    def register_site(
        self, rel: str, lineno: int, col: int, end_lineno: int, end_col: int, is_compare: bool
    ) -> int:
        sid = len(self.sites)
        self.sites.append((rel, lineno, col, end_lineno, end_col, is_compare))
        self.cur_flags.append(0)
        return sid

    def register_reach_site(
        self, rel: str, lineno: int, col: int, end_lineno: int, end_col: int
    ) -> int:
        rsid = len(self.reach_sites)
        self.reach_sites.append((rel, lineno, col, end_lineno, end_col))
        self.cur_reach.append(0)
        return rsid

    def switch_target(self, key: Any) -> None:
        """Flush current test's touched sites and switch active recording target."""
        if self.cur_key is not None:
            if self.touched_sids:
                dest = self.records.get(self.cur_key)
                if dest is None:
                    dest = {}
                    self.records[self.cur_key] = dest
                flags = self.cur_flags
                for sid in self.touched_sids:
                    dest[sid] = dest.get(sid, 0) | flags[sid]
                    flags[sid] = 0
                self.touched_sids.clear()
            if self.touched_rsids:
                rdest = self.reach_records.get(self.cur_key)
                if rdest is None:
                    rdest = set()
                    self.reach_records[self.cur_key] = rdest
                rflags = self.cur_reach
                for rsid in self.touched_rsids:
                    rdest.add(rsid)
                    rflags[rsid] = 0
                self.touched_rsids.clear()
        self.cur_key = key
        self.active = key is not None

    def stop(self) -> None:
        self.switch_target(None)

    def _type_safe_eq(self, t: type) -> bool:
        v = self._safe_eq_cache.get(t)
        if v is None:
            v = _is_safe_eq_type(t)
            self._safe_eq_cache[t] = v
        return v

    def _type_ident_eq(self, t: type) -> bool:
        v = self._ident_eq_cache.get(t)
        if v is None:
            v = _is_identity_eq_type(t)
            self._ident_eq_cache[t] = v
        return v

    def _is_clean_sqlglot_eq(self, t: type) -> bool:
        """True if `t` never equals None (unless NoneType), is reflexive (`x == x` is True),
        and satisfies `(a != b) == (not (a == b))`."""
        v = self._clean_eq_cache.get(t)
        if v is not None:
            return v
        if t in _REFL_NON_NONE_BUILTIN or t.__eq__ is object.__eq__ or issubclass(t, enum.Enum):
            v = True
        elif t.__name__ == "reverse_key" and getattr(t, "__module__", "") == "sqlglot.executor.env":
            v = False
        elif getattr(t, "__module__", "").startswith("sqlglot."):
            v = True
        else:
            v = False
        self._clean_eq_cache[t] = v
        return v

    def reach_mark(self, rsid: int) -> bool:
        """Pre-evaluation reachability probe. Always returns False so
        `__blast_rm__(rsid) or <expr>` evaluates `<expr>` transparently and
        records reachability even if `<expr>` raises an exception."""
        if self.in_probe:
            return False
        if not self.active:
            self.inactive_rsids.add(rsid)
            return False
        if not self.cur_reach[rsid]:
            self.cur_reach[rsid] = 1
            self.touched_rsids.append(rsid)
        return False

    def branch_probe(self, sid: int, val: Any) -> Any:
        if self.in_probe:
            return val
        if not self.active:
            self.inactive_sids.add(sid)
            return val
        prev = self.cur_flags[sid]
        if prev != ALL_BRANCH_BITS:
            bit = BIT_TRUE if val else BIT_FALSE
            if not (prev & bit):
                if prev == 0:
                    self.touched_sids.append(sid)
                self.cur_flags[sid] = prev | bit
        return val

    def compare_probe(self, sid: int, op_code: int, lhs: Any, rhs: Any) -> Any:
        # Execute exact Python comparison first (with real in_probe state so
        # genuine calls to Expr.__eq__ / _Dialect.__eq__ are still profiled!)
        if op_code == OP_IS:
            res = lhs is rhs
        elif op_code == OP_IS_NOT:
            res = lhs is not rhs
        elif op_code == OP_EQ:
            res = lhs == rhs
        elif op_code == OP_NOT_EQ:
            res = lhs != rhs
        elif op_code == OP_LT:
            res = lhs < rhs
        elif op_code == OP_LTE:
            res = lhs <= rhs
        elif op_code == OP_GT:
            res = lhs > rhs
        elif op_code == OP_GTE:
            res = lhs >= rhs
        elif op_code == OP_IN:
            res = lhs in rhs
        else:
            res = lhs not in rhs

        if self.in_probe:
            return res
        if not self.active:
            self.inactive_sids.add(sid)
            return res

        prev = self.cur_flags[sid]
        if prev == ALL_COMPARE_BITS:
            return res

        # Guard probe-internal equality checks against re-entering instrumented __eq__/__hash__
        self.in_probe = True
        try:
            if type(res) is bool:
                add = BIT_TRUE if res else BIT_FALSE
            else:
                try:
                    add = (BIT_TRUE if res else BIT_FALSE) | BIT_IS_TO_EQ_UNSAFE | BIT_EQ_TO_IS_UNSAFE | BIT_ORDER_UNSAFE
                except Exception:
                    add = BIT_IS_TO_EQ_UNSAFE | BIT_EQ_TO_IS_UNSAFE | BIT_ORDER_UNSAFE

            if op_code <= OP_NOT_EQ:
                add |= BIT_ORDER_UNSAFE
                if (prev & (BIT_IS_TO_EQ_UNSAFE | BIT_EQ_TO_IS_UNSAFE | BIT_EQ)) != (
                    BIT_IS_TO_EQ_UNSAFE | BIT_EQ_TO_IS_UNSAFE | BIT_EQ
                ):
                    tl = type(lhs)
                    tr = type(rhs)
                    if self._type_safe_eq(tl) and self._type_safe_eq(tr):
                        is_same = lhs is rhs
                        if op_code == OP_EQ:
                            eq_val = res
                            eq_ok = True
                        elif (
                            op_code == OP_NOT_EQ
                            and type(res) is bool
                            and (self._is_clean_sqlglot_eq(tl) or tl in _CONTAINER_OR_FLOAT)
                            and (self._is_clean_sqlglot_eq(tr) or tr in _CONTAINER_OR_FLOAT)
                        ):
                            eq_val = not res
                            eq_ok = True
                        elif (
                            (lhs is None or rhs is None)
                            and (self._is_clean_sqlglot_eq(tl) or tl in _CONTAINER_OR_FLOAT)
                            and (self._is_clean_sqlglot_eq(tr) or tr in _CONTAINER_OR_FLOAT)
                        ):
                            eq_val = is_same
                            eq_ok = True
                        elif is_same and self._is_clean_sqlglot_eq(tl):
                            eq_val = True
                            eq_ok = True
                        elif (
                            not is_same
                            and tl is not tr
                            and getattr(tl, "__module__", "").startswith("sqlglot.expressions")
                            and getattr(tr, "__module__", "").startswith("sqlglot.expressions")
                        ):
                            eq_val = False
                            eq_ok = True
                        else:
                            try:
                                eq_val = lhs == rhs
                                eq_ok = True
                            except Exception:
                                eq_val = False
                                eq_ok = False
                                add |= BIT_IS_TO_EQ_UNSAFE | BIT_EQ_TO_IS_UNSAFE | BIT_ORDER_UNSAFE

                        if eq_ok:
                            if type(eq_val) is not bool:
                                add |= BIT_IS_TO_EQ_UNSAFE | BIT_EQ_TO_IS_UNSAFE | BIT_ORDER_UNSAFE
                            else:
                                if eq_val:
                                    add |= BIT_EQ
                                if eq_val != is_same:
                                    add |= BIT_IS_TO_EQ_UNSAFE | BIT_EQ_TO_IS_UNSAFE
                                elif eq_val and not (self._type_ident_eq(tl) and self._type_ident_eq(tr)):
                                    add |= BIT_EQ_TO_IS_UNSAFE
                    else:
                        add |= BIT_IS_TO_EQ_UNSAFE | BIT_EQ_TO_IS_UNSAFE
            elif op_code <= OP_GTE:
                if not (prev & BIT_ORDER_UNSAFE) or not (prev & BIT_EQ):
                    if _is_pure_order_val(lhs) and _is_pure_order_val(rhs):
                        try:
                            if lhs == rhs:
                                add |= BIT_EQ
                        except Exception:
                            add |= BIT_ORDER_UNSAFE
                    else:
                        add |= BIT_ORDER_UNSAFE
            else:
                add |= BIT_IS_TO_EQ_UNSAFE | BIT_EQ_TO_IS_UNSAFE | BIT_ORDER_UNSAFE

            new_flags = prev | add
            if new_flags != prev:
                if prev == 0:
                    self.touched_sids.append(sid)
                self.cur_flags[sid] = new_flags
        finally:
            self.in_probe = False

        return res


def _has_walrus_or_yield(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, (ast.NamedExpr, ast.Yield, ast.YieldFrom, ast.Await)):
            return True
    return False


class _SiteInstrumenter(ast.NodeTransformer):
    """Instrument Compare, If/IfExp/While branch tests, and short-circuitable
    sub-expressions (BoolOp.values[1:], IfExp.body, IfExp.orelse) inside function bodies."""

    def __init__(self, rel: str, recorder: BuildTimeProfileRecorder, enable_reach: bool = True) -> None:
        self.rel = rel
        self.recorder = recorder
        self.enable_reach = enable_reach
        self._func_depth = 0

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        node.decorator_list = [self.visit(d) for d in node.decorator_list]
        self._func_depth += 1
        node.body = [self.visit(s) for s in node.body]
        self._func_depth -= 1
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        node.decorator_list = [self.visit(d) for d in node.decorator_list]
        self._func_depth += 1
        node.body = [self.visit(s) for s in node.body]
        self._func_depth -= 1
        return node

    def _wrap_reach(self, expr_node: ast.expr) -> ast.expr:
        if not self.enable_reach or self._func_depth <= 0 or _has_walrus_or_yield(expr_node):
            return self.visit(expr_node)
        ln = expr_node.lineno
        col = expr_node.col_offset
        eln = getattr(expr_node, "end_lineno", None)
        ecol = getattr(expr_node, "end_col_offset", None)
        if eln is None or ecol is None:
            return self.visit(expr_node)
        visited = self.visit(expr_node)
        rsid = self.recorder.register_reach_site(self.rel, ln, col, eln, ecol)
        mark_call = ast.Call(
            func=ast.Name(id="__blast_rm__", ctx=ast.Load()),
            args=[ast.Constant(value=rsid)],
            keywords=[],
        )
        wrapped = ast.BoolOp(op=ast.Or(), values=[mark_call, visited])
        ast.copy_location(mark_call, expr_node)
        ast.copy_location(wrapped, expr_node)
        return wrapped

    def _wrap_branch_test(self, test_node: ast.expr) -> ast.expr:
        if self._func_depth <= 0 or _has_walrus_or_yield(test_node):
            return self.visit(test_node)
        if isinstance(test_node, ast.Compare) and len(test_node.ops) == 1:
            return self.visit(test_node)
        ln = test_node.lineno
        col = test_node.col_offset
        eln = getattr(test_node, "end_lineno", None)
        ecol = getattr(test_node, "end_col_offset", None)
        if eln is None or ecol is None:
            return self.visit(test_node)
        visited_test = self.visit(test_node)
        sid = self.recorder.register_site(self.rel, ln, col, eln, ecol, is_compare=False)
        call = ast.Call(
            func=ast.Name(id="__blast_bp__", ctx=ast.Load()),
            args=[ast.Constant(value=sid), visited_test],
            keywords=[],
        )
        ast.copy_location(call, test_node)
        return call

    def visit_BoolOp(self, node: ast.BoolOp) -> ast.AST:
        if self._func_depth <= 0 or not self.enable_reach or len(node.values) < 2:
            return self.generic_visit(node)
        new_vals = [self.visit(node.values[0])]
        for v in node.values[1:]:
            new_vals.append(self._wrap_reach(v))
        node.values = new_vals
        return node

    def visit_If(self, node: ast.If) -> ast.AST:
        node.test = self._wrap_branch_test(node.test)
        node.body = [self.visit(s) for s in node.body]
        node.orelse = [self.visit(s) for s in node.orelse]
        return node

    def visit_IfExp(self, node: ast.IfExp) -> ast.AST:
        node.test = self._wrap_branch_test(node.test)
        node.body = self._wrap_reach(node.body)
        node.orelse = self._wrap_reach(node.orelse)
        return node

    def visit_While(self, node: ast.While) -> ast.AST:
        node.test = self._wrap_branch_test(node.test)
        node.body = [self.visit(s) for s in node.body]
        node.orelse = [self.visit(s) for s in node.orelse]
        return node

    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        if (
            self._func_depth <= 0
            or len(node.ops) != 1
            or len(node.comparators) != 1
            or _has_walrus_or_yield(node)
        ):
            return self.generic_visit(node)
        op_code = _AST_OP_TO_CODE.get(type(node.ops[0]))
        if op_code is None:
            return self.generic_visit(node)
        ln = node.lineno
        col = node.col_offset
        eln = getattr(node, "end_lineno", None)
        ecol = getattr(node, "end_col_offset", None)
        if eln is None or ecol is None:
            return self.generic_visit(node)
        left = self.visit(node.left)
        right = self.visit(node.comparators[0])
        sid = self.recorder.register_site(self.rel, ln, col, eln, ecol, is_compare=True)
        call = ast.Call(
            func=ast.Name(id="__blast_cp__", ctx=ast.Load()),
            args=[ast.Constant(value=sid), ast.Constant(value=op_code), left, right],
            keywords=[],
        )
        ast.copy_location(call, node)
        return call


class _ProfilingLoader(importlib.abc.Loader):
    def __init__(
        self,
        filepath: str,
        rel: str,
        is_pkg: bool,
        recorder: BuildTimeProfileRecorder,
        enable_reach: bool = True,
        mapping_recorder: Any = None,
        post_exec_hook: Any = None,
    ) -> None:
        self.filepath = filepath
        self.rel = rel
        self.is_pkg = is_pkg
        self.recorder = recorder
        self.enable_reach = enable_reach
        self.mapping_recorder = mapping_recorder
        self.post_exec_hook = post_exec_hook

    def create_module(self, spec: Any) -> Any:
        return None

    def exec_module(self, module: Any) -> None:
        module.__file__ = self.filepath
        if self.is_pkg:
            module.__path__ = [os.path.dirname(self.filepath)]
        raw = Path(self.filepath).read_bytes()
        tree = ast.parse(raw, filename=self.filepath)
        # Skip sqlglot/typing/*.py so test_typing.py sees exact unmodified bytecode/co_varnames!
        if not self.rel.startswith("sqlglot/typing/"):
            tree = _SiteInstrumenter(self.rel, self.recorder, enable_reach=self.enable_reach).visit(tree)
            ast.fix_missing_locations(tree)
            # Bind probes directly in module globals for fast LOAD_GLOBAL lookup
            module.__dict__["__blast_bp__"] = self.recorder.branch_probe
            module.__dict__["__blast_cp__"] = self.recorder.compare_probe
            module.__dict__["__blast_rm__"] = self.recorder.reach_mark
        code = compile(tree, self.filepath, "exec")
        mr = self.mapping_recorder
        if mr is not None:
            mr.in_import += 1
        try:
            exec(code, module.__dict__)
        finally:
            if mr is not None:
                mr.in_import -= 1
        if self.post_exec_hook is not None:
            self.post_exec_hook(module, self.rel)


class ProfilingMetaPathFinder(importlib.abc.MetaPathFinder):
    """Intercepts sqlglot.* imports during build to inject expression & reachability probes."""

    def __init__(
        self,
        sqlglot_dir: Path,
        recorder: BuildTimeProfileRecorder,
        enable_reach: bool = True,
        mapping_recorder: Any = None,
        post_exec_hook: Any = None,
    ) -> None:
        self.root = sqlglot_dir.resolve()
        self.recorder = recorder
        self.enable_reach = enable_reach
        self.mapping_recorder = mapping_recorder
        self.post_exec_hook = post_exec_hook

    def find_spec(self, fullname: str, path: Any, target: Any = None) -> Any:
        if fullname != "sqlglot" and not fullname.startswith("sqlglot."):
            return None
        rel_base = fullname.replace(".", "/")
        pkg_init = self.root / rel_base / "__init__.py"
        if pkg_init.is_file():
            rel = f"{rel_base}/__init__.py"
            loader = _ProfilingLoader(
                str(pkg_init),
                rel,
                True,
                self.recorder,
                self.enable_reach,
                self.mapping_recorder,
                self.post_exec_hook,
            )
            return importlib.machinery.ModuleSpec(fullname, loader, origin=str(pkg_init), is_package=True)
        mod_file = self.root / f"{rel_base}.py"
        if mod_file.is_file():
            rel = f"{rel_base}.py"
            loader = _ProfilingLoader(
                str(mod_file),
                rel,
                False,
                self.recorder,
                self.enable_reach,
                self.mapping_recorder,
                self.post_exec_hook,
            )
            return importlib.machinery.ModuleSpec(fullname, loader, origin=str(mod_file), is_package=False)
        return None


# ---------------------------------------------------------------------------
# Runtime Formal Soundness Prover (used inside selector.py)
# ---------------------------------------------------------------------------
_STRICTLY_PURE_EXPR_TYPES = (
    ast.Compare,
    ast.BoolOp,
    ast.BinOp,
    ast.UnaryOp,
    ast.IfExp,
    ast.Name,
    ast.Constant,
    ast.Tuple,
    ast.List,
    ast.Set,
    ast.Dict,
)


def _is_strictly_pure_ast_expr(node: ast.AST) -> bool:
    """Return True only if `node` cannot invoke arbitrary Python code
    (no Call, Attribute/@property, Subscript/__getitem__, NamedExpr, Yield, etc.)."""
    if not isinstance(node, ast.expr):
        return False
    for sub in ast.walk(node):
        if isinstance(sub, ast.expr) and not isinstance(sub, _STRICTLY_PURE_EXPR_TYPES):
            return False
    return True


def _dump(node: ast.AST) -> str:
    return ast.dump(node, include_attributes=False)


def _node_span(node: ast.AST) -> tuple[int, int, int, int] | None:
    ln = getattr(node, "lineno", None)
    col = getattr(node, "col_offset", None)
    eln = getattr(node, "end_lineno", None)
    ecol = getattr(node, "end_col_offset", None)
    if ln is None or col is None or eln is None or ecol is None:
        return None
    return (ln, col, eln, ecol)


def _spans_overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    a_start, a_end = (a[0], a[1]), (a[2], a[3])
    b_start, b_end = (b[0], b[1]), (b[2], b[3])
    return a_start < b_end and b_start < a_end


def find_expr_edits(
    old_tree: ast.AST,
    new_tree: ast.AST,
    max_diffs: int = 32,
) -> list[tuple[ast.expr, ast.expr, bool, tuple[tuple[int, int, int, int], ...]]] | None:
    """If `old_tree` and `new_tree` differ ONLY at disjoint expression sites inside
    function bodies (and nowhere else in the module: no import, class header/attr,
    module var, signature/decorator, or statement-structure changes), return a list
    of `(old_expr, new_expr, is_branch_test, ancestor_expr_spans)`.

    Returns None on any non-expression diff, nested/overlapping diff, walrus/yield/await
    diff, or if > max_diffs sites changed.
    """
    diffs: list[tuple[ast.expr, ast.expr, bool, tuple[tuple[int, int, int, int], ...]]] = []

    def walk(
        a: ast.AST,
        b: ast.AST,
        in_func: bool,
        is_branch: bool,
        anc: tuple[tuple[int, int, int, int], ...],
    ) -> bool:
        if type(a) is not type(b):
            if in_func and isinstance(a, ast.expr) and isinstance(b, ast.expr):
                if _has_walrus_or_yield(a) or _has_walrus_or_yield(b):
                    return False
                diffs.append((a, b, is_branch, anc))
                return len(diffs) <= max_diffs
            return False

        is_func_def = isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef))
        next_anc = anc
        if in_func and isinstance(a, ast.expr):
            sp = _node_span(a)
            if sp is not None:
                next_anc = (*anc, sp)

        for fname in a._fields:
            va = getattr(a, fname)
            vb = getattr(b, fname)
            child_in_func = in_func or (is_func_def and fname == "body")
            # Reset ancestor chain when entering a nested function body
            child_anc = () if (is_func_def and fname == "body") else next_anc
            child_branch = isinstance(a, (ast.If, ast.IfExp, ast.While)) and fname == "test"

            if isinstance(va, ast.AST) and isinstance(vb, ast.AST):
                if _dump(va) != _dump(vb):
                    before = len(diffs)
                    if not walk(va, vb, child_in_func, child_branch, child_anc):
                        del diffs[before:]
                        if (
                            child_in_func
                            and isinstance(va, ast.expr)
                            and isinstance(vb, ast.expr)
                            and not _has_walrus_or_yield(va)
                            and not _has_walrus_or_yield(vb)
                        ):
                            diffs.append((va, vb, child_branch, child_anc))
                            if len(diffs) > max_diffs:
                                return False
                        else:
                            return False
            elif isinstance(va, list) and isinstance(vb, list):
                if len(va) != len(vb):
                    return False
                for xa, xb in zip(va, vb):
                    if isinstance(xa, ast.AST) and isinstance(xb, ast.AST):
                        if _dump(xa) != _dump(xb):
                            before = len(diffs)
                            if not walk(xa, xb, child_in_func, False, child_anc):
                                del diffs[before:]
                                if (
                                    child_in_func
                                    and isinstance(xa, ast.expr)
                                    and isinstance(xb, ast.expr)
                                    and not _has_walrus_or_yield(xa)
                                    and not _has_walrus_or_yield(xb)
                                ):
                                    diffs.append((xa, xb, False, child_anc))
                                    if len(diffs) > max_diffs:
                                        return False
                                else:
                                    return False
                    elif xa != xb:
                        return False
            else:
                if va != vb:
                    return False
        return len(diffs) <= max_diffs

    if not walk(old_tree, new_tree, False, False, ()) or not diffs:
        return None

    # Defensive check: reject any overlapping or nested source spans
    spans: list[tuple[int, int, int, int]] = []
    for old_e, _, _, _ in diffs:
        sp = _node_span(old_e)
        if sp is None:
            return None
        for prev_sp in spans:
            if _spans_overlap(sp, prev_sp):
                return None
        spans.append(sp)

    return diffs


def find_single_expr_edit(
    old_tree: ast.AST,
    new_tree: ast.AST,
) -> tuple[ast.expr, ast.expr, bool] | None:
    """Backwards-compatible wrapper returning the single diff if exactly 1 site changed."""
    diffs = find_expr_edits(old_tree, new_tree, max_diffs=1)
    if not diffs or len(diffs) != 1:
        return None
    old_e, new_e, is_br, _ = diffs[0]
    return (old_e, new_e, is_br)


def _prove_value_equiv_on_seen(
    old_expr: ast.expr,
    new_expr: ast.expr,
    is_branch: bool,
    prof: tuple[int, ...],
    eligible: int,
) -> int:
    """Apply Rules P1..P6 to tests in `eligible` (which are guaranteed to be in prof[0])."""
    if not eligible:
        return 0

    false_bits = prof[1]
    true_bits = prof[2]
    is_compare_prof = len(prof) == 7

    # --- Rules P1, P2, P3: Single-op Compare operator swaps with identical operands ---
    if (
        is_compare_prof
        and isinstance(old_expr, ast.Compare)
        and isinstance(new_expr, ast.Compare)
        and len(old_expr.ops) == 1
        and len(new_expr.ops) == 1
        and len(old_expr.comparators) == 1
        and len(new_expr.comparators) == 1
        and _dump(old_expr.left) == _dump(new_expr.left)
        and _dump(old_expr.comparators[0]) == _dump(new_expr.comparators[0])
    ):
        eq_bits = prof[3]
        is2eq_unsafe = prof[4]
        eq2is_unsafe = prof[5]
        order_unsafe = prof[6]

        op_old = type(old_expr.ops[0])
        op_new = type(new_expr.ops[0])

        # P1: `is` -> `==` or `is not` -> `!=`
        if (op_old is ast.Is and op_new is ast.Eq) or (op_old is ast.IsNot and op_new is ast.NotEq):
            return eligible & ~is2eq_unsafe

        # P2: `==` -> `is` or `!=` -> `is not`
        if (op_old is ast.Eq and op_new is ast.Is) or (op_old is ast.NotEq and op_new is ast.IsNot):
            return eligible & ~eq2is_unsafe

        # P3: `<` <-> `<=` or `>` <-> `>=` (only differs when lhs == rhs)
        if (
            (op_old is ast.Lt and op_new is ast.LtE)
            or (op_old is ast.LtE and op_new is ast.Lt)
            or (op_old is ast.Gt and op_new is ast.GtE)
            or (op_old is ast.GtE and op_new is ast.Gt)
        ):
            return eligible & ~order_unsafe & ~eq_bits

    # --- Rule P4: Short-circuit conjunction addition: `E` -> `E and X ...` ---
    if isinstance(new_expr, ast.BoolOp) and isinstance(new_expr.op, ast.And):
        if len(new_expr.values) >= 2 and _dump(new_expr.values[0]) == _dump(old_expr):
            return eligible & ~true_bits
        if (
            isinstance(old_expr, ast.BoolOp)
            and isinstance(old_expr.op, ast.And)
            and len(new_expr.values) > len(old_expr.values)
            and all(_dump(a) == _dump(b) for a, b in zip(old_expr.values, new_expr.values))
        ):
            return eligible & ~true_bits

    if (
        is_branch
        and isinstance(new_expr, ast.Constant)
        and new_expr.value is False
        and _is_strictly_pure_ast_expr(old_expr)
    ):
        return eligible & ~true_bits

    # --- Rule P5: Short-circuit disjunction addition: `E` -> `E or X ...` ---
    if isinstance(new_expr, ast.BoolOp) and isinstance(new_expr.op, ast.Or):
        if len(new_expr.values) >= 2 and _dump(new_expr.values[0]) == _dump(old_expr):
            return eligible & ~false_bits
        if (
            isinstance(old_expr, ast.BoolOp)
            and isinstance(old_expr.op, ast.Or)
            and len(new_expr.values) > len(old_expr.values)
            and all(_dump(a) == _dump(b) for a, b in zip(old_expr.values, new_expr.values))
        ):
            return eligible & ~false_bits

    if (
        is_branch
        and isinstance(new_expr, ast.Constant)
        and new_expr.value is True
        and _is_strictly_pure_ast_expr(old_expr)
    ):
        return eligible & ~false_bits

    # --- Rule P6: Pure BoolOp `and` <-> `or` in branch context ---
    if (
        is_branch
        and isinstance(old_expr, ast.BoolOp)
        and isinstance(new_expr, ast.BoolOp)
        and len(old_expr.values) == len(new_expr.values)
        and all(_dump(a) == _dump(b) for a, b in zip(old_expr.values, new_expr.values))
        and all(_is_strictly_pure_ast_expr(v) for v in old_expr.values[1:])
    ):
        if isinstance(old_expr.op, ast.And) and isinstance(new_expr.op, ast.Or):
            return eligible & ~false_bits
        if isinstance(old_expr.op, ast.Or) and isinstance(new_expr.op, ast.And):
            return eligible & ~true_bits

    return 0


def prove_unchanged_test_bits(
    old_tree: ast.AST,
    new_tree: ast.AST,
    file_profiles: dict[tuple[int, ...], tuple[int, ...]] | None,
    candidate_bits: int,
    file_reach_profiles: dict[tuple[int, ...], int] | None = None,
    unprofiled_test_bits: int = 0,
    enable_reach: bool = True,
    enable_multisite: bool = True,
) -> int:
    """Return bitset of tests in `candidate_bits` whose OWN recorded profile(s)
    mathematically prove ALL edited expression sites in this file cannot change
    their execution trace (by First-Divergence Induction).

    Rules:
    - Any non-expression edit, nested/overlapping edit, or walrus/yield edit -> 0.
    - Multi-site conjunction: test T is proven unchanged ONLY IF EVERY edited site
      in the file is either (a) unreached by T (on an instrumented, stable,
      non-import-time site, excluding unprofiled/subprocess tests) OR (b) proven
      value-equivalent for T by P1..P6.
    - Any missing/unstable profile keeps all tests for that site -> 0.
    """
    if not candidate_bits:
        return 0
    if not file_profiles and not file_reach_profiles:
        return 0

    max_diffs = 32 if enable_multisite else 1
    diffs = find_expr_edits(old_tree, new_tree, max_diffs=max_diffs)
    if not diffs:
        return 0
    if not enable_multisite and len(diffs) != 1:
        return 0

    f_expr = file_profiles or {}
    f_reach = file_reach_profiles or {}
    safe_reach_mask = candidate_bits & ~unprofiled_test_bits

    # Conjunction across ALL modified sites in this file
    file_proven = candidate_bits
    for old_expr, new_expr, is_branch, anc_sites in diffs:
        site4 = _node_span(old_expr)
        if site4 is None:
            return 0
        prof = f_expr.get(site4)
        reach_seen = f_reach.get(site4)

        site_proven = 0

        # 1. Sub-expression reachability proof:
        #    - `reach_seen` / ancestor `aseen` ran BEFORE evaluating the sub-expression
        #      (via `__blast_rm__(rsid) or <expr>`), so `T not in seen` is 100% sound
        #      even if `<expr>` could raise an exception.
        #    - `prof[0]` ran AFTER evaluating sub-operands, so `T not in prof[0]` is
        #      only used when `old_expr` and `new_expr` evaluate identical prefix/operands
        #      before the changed operator (Compare op-only swap, or `E -> E and/or X`).
        if enable_reach:
            if reach_seen is not None:
                site_proven |= safe_reach_mask & ~reach_seen
            for asite in anc_sites:
                aseen = f_reach.get(asite)
                if aseen is not None:
                    site_proven |= safe_reach_mask & ~aseen
            if prof is not None:
                same_prefix = False
                if (
                    isinstance(old_expr, ast.Compare)
                    and isinstance(new_expr, ast.Compare)
                    and len(old_expr.ops) == 1
                    and len(new_expr.ops) == 1
                    and len(old_expr.comparators) == 1
                    and len(new_expr.comparators) == 1
                    and _dump(old_expr.left) == _dump(new_expr.left)
                    and _dump(old_expr.comparators[0]) == _dump(new_expr.comparators[0])
                ):
                    same_prefix = True
                elif (
                    isinstance(new_expr, ast.BoolOp)
                    and len(new_expr.values) >= 2
                    and _dump(new_expr.values[0]) == _dump(old_expr)
                ):
                    same_prefix = True
                if same_prefix:
                    site_proven |= safe_reach_mask & ~prof[0]

        # 2. Value-equivalence proof (P1..P6) for tests that DID reach `site`
        if prof is not None:
            eligible = candidate_bits & prof[0]
            if eligible:
                site_proven |= _prove_value_equiv_on_seen(old_expr, new_expr, is_branch, prof, eligible)

        file_proven &= site_proven
        if not file_proven:
            return 0

    return file_proven
