"""Build-time per-test expression-site profiler and runtime soundness prover.

Pure Python standard library only.
- At build time (inside build_map.py), instruments Compare and If/IfExp/While
  branch test sites across sqlglot/**/*.py in memory (preserving exact line
  numbers) to record per-test observed outcomes and operand-type safety bits.
- At runtime (inside selector.py), proves that a single-expression edit cannot
  alter a specific test T's behaviour based STRICTLY on test T's own profile at
  that exact (file, lineno, col_offset) site. Any missing profile or uncertainty
  keeps the test.
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

# Compare operator codes for fast runtime dispatch inside _cp()
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
_SAFE_ORDER_ATOMS = (int, float, str, bytes)
_IDENTITY_SINGLETON_TYPES = (type(None), bool)


def _is_pure_order_val(val: Any) -> bool:
    """Return True if `val` is a primitive or recursive tuple of primitives with
    pure, deterministic total ordering and equality."""
    t = type(val)
    if t in _SAFE_ORDER_ATOMS:
        # Exclude NaN floats (nan != nan breaks < <-> <= assumptions)
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
    # sqlglot Expression / DataType / Token classes have pure structural __eq__
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
# Build-time AST instrumenter & ultra-fast site flag recorder
# ---------------------------------------------------------------------------
class BuildTimeProfileRecorder:
    """Fast per-test expression-site recorder used inside build_map.py workers."""

    def __init__(self) -> None:
        # Global site table for this worker: sid -> (rel, lineno, col_offset, is_compare)
        self.sites: list[tuple[str, int, int, bool]] = []
        # Current test's flat array of observed flags (indexed by sid)
        self.cur_flags: list[int] = []
        self.touched_sids: list[int] = []
        self.active: bool = False
        # Completed per-target records: target_key -> dict[sid, flags]
        self.records: dict[Any, dict[int, int]] = {}
        self.cur_key: Any = None
        # Sites executed outside any test (e.g. module import / collection time) -> never prune
        self.inactive_sids: set[int] = set()
        # Memoize type safety checks for hot-path speed
        self._safe_eq_cache: dict[type, bool] = {}
        self._ident_eq_cache: dict[type, bool] = {}

    def register_site(self, rel: str, lineno: int, col: int, is_compare: bool) -> int:
        sid = len(self.sites)
        self.sites.append((rel, lineno, col, is_compare))
        self.cur_flags.append(0)
        return sid

    def switch_target(self, key: Any) -> None:
        """Flush current test's touched sites and switch active recording target."""
        if self.cur_key is not None and self.touched_sids:
            dest = self.records.get(self.cur_key)
            if dest is None:
                dest = {}
                self.records[self.cur_key] = dest
            flags = self.cur_flags
            for sid in self.touched_sids:
                dest[sid] = dest.get(sid, 0) | flags[sid]
                flags[sid] = 0
            self.touched_sids.clear()
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

    def branch_probe(self, sid: int, val: Any) -> Any:
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
        # Execute exact Python comparison first
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

        if not self.active:
            self.inactive_sids.add(sid)
            return res

        prev = self.cur_flags[sid]
        if prev == ALL_COMPARE_BITS:
            return res

        add = 0
        # 1. Boolean outcome
        try:
            if type(res) is bool:
                add |= BIT_TRUE if res else BIT_FALSE
            else:
                add |= (BIT_TRUE if res else BIT_FALSE) | BIT_IS_TO_EQ_UNSAFE | BIT_EQ_TO_IS_UNSAFE | BIT_ORDER_UNSAFE
        except Exception:
            add |= BIT_IS_TO_EQ_UNSAFE | BIT_EQ_TO_IS_UNSAFE | BIT_ORDER_UNSAFE

        # 2. Identity vs equality & ordering checks (only for non-`in` comparisons)
        if op_code <= OP_GTE:
            tl = type(lhs)
            tr = type(rhs)

            # Check is <-> == safety
            if not (prev & BIT_IS_TO_EQ_UNSAFE) or not (prev & BIT_EQ_TO_IS_UNSAFE) or not (prev & BIT_EQ):
                if op_code in (OP_IS, OP_IS_NOT, OP_EQ, OP_NOT_EQ):
                    is_same = lhs is rhs
                    if self._type_safe_eq(tl) and self._type_safe_eq(tr):
                        try:
                            eq_val = (lhs == rhs)
                            if type(eq_val) is not bool:
                                add |= BIT_IS_TO_EQ_UNSAFE | BIT_EQ_TO_IS_UNSAFE | BIT_ORDER_UNSAFE
                            else:
                                if eq_val:
                                    add |= BIT_EQ
                                if eq_val != is_same:
                                    add |= BIT_IS_TO_EQ_UNSAFE | BIT_EQ_TO_IS_UNSAFE
                                elif eq_val and not (self._type_ident_eq(tl) and self._type_ident_eq(tr)):
                                    # Equal by value on non-singleton type (e.g. small int/str interning)
                                    add |= BIT_EQ_TO_IS_UNSAFE
                        except Exception:
                            add |= BIT_IS_TO_EQ_UNSAFE | BIT_EQ_TO_IS_UNSAFE | BIT_ORDER_UNSAFE
                    else:
                        add |= BIT_IS_TO_EQ_UNSAFE | BIT_EQ_TO_IS_UNSAFE

            # Check < <-> <= and > <-> >= ordering safety
            if op_code in (OP_LT, OP_LTE, OP_GT, OP_GTE):
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
                add |= BIT_ORDER_UNSAFE
        else:
            add |= BIT_IS_TO_EQ_UNSAFE | BIT_EQ_TO_IS_UNSAFE | BIT_ORDER_UNSAFE

        new_flags = prev | add
        if new_flags != prev:
            if prev == 0:
                self.touched_sids.append(sid)
            self.cur_flags[sid] = new_flags

        return res


def _has_walrus_or_yield(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, (ast.NamedExpr, ast.Yield, ast.YieldFrom, ast.Await)):
            return True
    return False


class _SiteInstrumenter(ast.NodeTransformer):
    """Instrument Compare and If/IfExp/While test expressions inside function bodies."""

    def __init__(self, rel: str, recorder: BuildTimeProfileRecorder) -> None:
        self.rel = rel
        self.recorder = recorder
        self._func_depth = 0

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        # Do not instrument default arguments (evaluated at import/def time)
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

    def _wrap_branch_test(self, test_node: ast.expr) -> ast.expr:
        if self._func_depth <= 0 or _has_walrus_or_yield(test_node):
            return self.visit(test_node)
        # If test is already a single-op Compare, visit_Compare instruments it with richer bits
        if isinstance(test_node, ast.Compare) and len(test_node.ops) == 1:
            return self.visit(test_node)
        ln = test_node.lineno
        col = test_node.col_offset
        visited_test = self.visit(test_node)
        sid = self.recorder.register_site(self.rel, ln, col, is_compare=False)
        call = ast.Call(
            func=ast.Name(id="__blast_bp__", ctx=ast.Load()),
            args=[ast.Constant(value=sid), visited_test],
            keywords=[],
        )
        ast.copy_location(call, test_node)
        return call

    def visit_If(self, node: ast.If) -> ast.AST:
        node.test = self._wrap_branch_test(node.test)
        node.body = [self.visit(s) for s in node.body]
        node.orelse = [self.visit(s) for s in node.orelse]
        return node

    def visit_IfExp(self, node: ast.IfExp) -> ast.AST:
        node.test = self._wrap_branch_test(node.test)
        node.body = self.visit(node.body)
        node.orelse = self.visit(node.orelse)
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
        left = self.visit(node.left)
        right = self.visit(node.comparators[0])
        sid = self.recorder.register_site(self.rel, ln, col, is_compare=True)
        call = ast.Call(
            func=ast.Name(id="__blast_cp__", ctx=ast.Load()),
            args=[ast.Constant(value=sid), ast.Constant(value=op_code), left, right],
            keywords=[],
        )
        ast.copy_location(call, node)
        return call


class _ProfilingLoader(importlib.abc.Loader):
    def __init__(self, filepath: str, rel: str, is_pkg: bool, recorder: BuildTimeProfileRecorder) -> None:
        self.filepath = filepath
        self.rel = rel
        self.is_pkg = is_pkg
        self.recorder = recorder

    def create_module(self, spec: Any) -> Any:
        return None

    def exec_module(self, module: Any) -> None:
        module.__file__ = self.filepath
        if self.is_pkg:
            module.__path__ = [os.path.dirname(self.filepath)]
        raw = Path(self.filepath).read_bytes()
        tree = ast.parse(raw, filename=self.filepath)
        # Skip sqlglot/typing/*.py so test_typing.py sees exact unmodified bytecode!
        if not self.rel.startswith("sqlglot/typing/"):
            tree = _SiteInstrumenter(self.rel, self.recorder).visit(tree)
            ast.fix_missing_locations(tree)
        code = compile(tree, self.filepath, "exec")
        exec(code, module.__dict__)


class ProfilingMetaPathFinder(importlib.abc.MetaPathFinder):
    """Intercepts sqlglot.* imports during build_map.py to inject expression probes."""

    def __init__(self, sqlglot_dir: Path, recorder: BuildTimeProfileRecorder) -> None:
        self.root = sqlglot_dir.resolve()
        self.recorder = recorder

    def find_spec(self, fullname: str, path: Any, target: Any = None) -> Any:
        if fullname != "sqlglot" and not fullname.startswith("sqlglot."):
            return None
        rel_base = fullname.replace(".", "/")
        pkg_init = self.root / rel_base / "__init__.py"
        if pkg_init.is_file():
            rel = f"{rel_base}/__init__.py"
            loader = _ProfilingLoader(str(pkg_init), rel, True, self.recorder)
            return importlib.machinery.ModuleSpec(fullname, loader, origin=str(pkg_init), is_package=True)
        mod_file = self.root / f"{rel_base}.py"
        if mod_file.is_file():
            rel = f"{rel_base}.py"
            loader = _ProfilingLoader(str(mod_file), rel, False, self.recorder)
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


def find_single_expr_edit(
    old_tree: ast.AST,
    new_tree: ast.AST,
) -> tuple[ast.expr, ast.expr, bool] | None:
    """If `old_tree` and `new_tree` differ at EXACTLY ONE expression site inside a
    function body (and nowhere else in the file), return `(old_expr, new_expr, is_branch_test)`.
    Otherwise return None."""
    diffs: list[tuple[ast.expr, ast.expr, bool]] = []

    def walk(a: ast.AST, b: ast.AST, in_func: bool, is_branch: bool) -> bool:
        if type(a) is not type(b):
            if in_func and isinstance(a, ast.expr) and isinstance(b, ast.expr):
                diffs.append((a, b, is_branch))
                return len(diffs) <= 1
            return False

        is_func_def = isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef))

        for fname in a._fields:
            va = getattr(a, fname)
            vb = getattr(b, fname)
            child_in_func = in_func or (is_func_def and fname == "body")
            child_branch = isinstance(a, (ast.If, ast.IfExp, ast.While)) and fname == "test"
            if isinstance(va, ast.AST) and isinstance(vb, ast.AST):
                if _dump(va) != _dump(vb):
                    before = len(diffs)
                    if not walk(va, vb, child_in_func, child_branch):
                        del diffs[before:]
                        if child_in_func and isinstance(va, ast.expr) and isinstance(vb, ast.expr):
                            diffs.append((va, vb, child_branch))
                            if len(diffs) > 1:
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
                            if not walk(xa, xb, child_in_func, False):
                                del diffs[before:]
                                if child_in_func and isinstance(xa, ast.expr) and isinstance(xb, ast.expr):
                                    diffs.append((xa, xb, False))
                                    if len(diffs) > 1:
                                        return False
                                else:
                                    return False
                    elif xa != xb:
                        return False
            else:
                if va != vb:
                    return False
        return len(diffs) <= 1

    if not walk(old_tree, new_tree, False, False) or len(diffs) != 1:
        return None
    return diffs[0]


def prove_unchanged_test_bits(
    old_tree: ast.AST,
    new_tree: ast.AST,
    file_profiles: dict[tuple[int, int], tuple[int, ...]],
    candidate_bits: int,
) -> int:
    """Return bitset of tests in `candidate_bits` whose OWN recorded profile at the
    modified expression site mathematically proves the edit cannot change their
    execution trace (by first-divergence induction).

    Any missing profile, unseen test, or unprovable edit returns 0 (keeps the test).
    """
    if not file_profiles or not candidate_bits:
        return 0

    edit = find_single_expr_edit(old_tree, new_tree)
    if edit is None:
        return 0

    old_expr, new_expr, is_branch = edit
    site = (getattr(old_expr, "lineno", -1), getattr(old_expr, "col_offset", -1))
    prof = file_profiles.get(site)
    if prof is None:
        return 0

    # Unpack profile: 3-tuple (branch) or 7-tuple (compare)
    seen_bits = prof[0]
    false_bits = prof[1]
    true_bits = prof[2]
    is_compare_prof = len(prof) == 7

    # Only tests that ACTUALLY executed this site and were profiled can be proven unchanged
    eligible = candidate_bits & seen_bits
    if not eligible:
        return 0

    # --- Rule P1 & P2 & P3: Single-op Compare operator swaps with identical operands ---
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
    # When E only ever evaluated falsy for test T, `E and X` short-circuits and returns E
    # without ever evaluating X.
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

    # Branch test replaced with literal False when test T only ever saw False
    # and old_expr is strictly side-effect-free (since old_expr is no longer evaluated)
    if (
        is_branch
        and isinstance(new_expr, ast.Constant)
        and new_expr.value is False
        and _is_strictly_pure_ast_expr(old_expr)
    ):
        return eligible & ~true_bits

    # --- Rule P5: Short-circuit disjunction addition: `E` -> `E or X ...` ---
    # When E only ever evaluated truthy for test T, `E or X` short-circuits and returns E
    # without ever evaluating X.
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

    # Branch test replaced with literal True when test T only ever saw True
    # and old_expr is strictly side-effect-free (since old_expr is no longer evaluated)
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
        # `v1 and v2` -> `v1 or v2`: identical when `v1 and v2` was ALWAYS True (every v_i truthy)
        if isinstance(old_expr.op, ast.And) and isinstance(new_expr.op, ast.Or):
            return eligible & ~false_bits
        # `v1 or v2` -> `v1 and v2`: identical when `v1 or v2` was ALWAYS False (every v_i falsy)
        if isinstance(old_expr.op, ast.Or) and isinstance(new_expr.op, ast.And):
            return eligible & ~true_bits

    return 0
