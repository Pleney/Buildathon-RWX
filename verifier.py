"""In-container forked test verifier & AST shadow expression checker.

Runs selected fast candidate tests in an isolated Linux forked child process
against the patched sqlglot code loaded strictly in memory (zero writes to the
read-only root filesystem, zero stale sys.modules references across commits).

- Any test that runs and passes (including all subTests) is dropped with 0% risk.
- Any failing subTest aborts immediately (since 1 failing subTest fails the test).
- Any failing test stops further verification immediately so unrun tests are kept.
- For single pure-expression edits where expensive tests (>1.8s) remain unrun after
  many cheap tests pass, AST shadow instrumentation checks whether the old and new
  expressions ever diverged at runtime across all evaluated calls.
"""

from __future__ import annotations

import ast
import builtins
import concurrent.futures
import copy
import gzip
import importlib
import importlib.abc
import importlib.machinery
import io
import json
import os
import pickle
import resource
import signal
import sys
import time
import unittest
from pathlib import Path
from typing import Any

import packer  # noqa: E402

# Default verification budgets
DEFAULT_VERIFIER_CONFIG: dict[str, Any] = {
    # Skip verification when candidate set is larger than this (broad fallback)
    "max_candidates_to_verify": 450,
    # Skip verification when static selection cost (s) is already at or below this threshold
    "min_static_cost_to_verify": 0.0,
    # Minimum ratio of max_possible_saving / est_cpu_cost required to fork and verify
    "min_roi_ratio": 1.0,
    # Estimated fixed fork + sqlglot import CPU overhead per verified commit (s)
    "fork_overhead_sec": 0.14,
    # Do not run any single test whose recorded duration exceeds this (seconds)
    "max_single_test_sec": 1.60,
    # If only 1 candidate remains after all others passed, allow running up to this duration
    # because proving it passes saves 2.00 s worker overhead + its duration!
    "last_test_max_sec": 2.20,
    # Maximum actual CPU seconds to spend verifying a single commit
    "max_commit_cpu_sec": 1.60,
    # Maximum number of passing tests to run before stopping when unrun tests remain
    "max_canary_tests": 45,
    # Per-test wall alarm timeout in seconds (prevents infinite loops)
    "per_test_timeout_sec": 8,
    # Speculative pruning of unrun tests (shadow-expression & Laplace) DISABLED after
    # differential testing found wrong drops on rare-True branch conditions.
    # Only tests that actually execute and pass in the forked child may be dropped!
    "enable_shadow_pruning": False,
    "enable_laplace_pruning": False,
    # Shadow-verified equivalence thresholds (only used if enable_shadow_pruning=True)
    "shadow_early_stop_passed": 16,
    "shadow_early_stop_calls": 150,
    "shadow_min_passed": 12,
    "shadow_min_calls": 50,
    # Non-shadow Bayesian Laplace pruning (only used if enable_laplace_pruning=True)
    "laplace_alpha": 3.0,
    "laplace_min_passed": 35,
    # Skip verifying fixture-only commits (almost always fail their single test)
    "skip_fixture_only": True,
    # Batch wall-time deadline guard: stop verifying past 60% of the batch limit
    # (practice limit = 180s -> 108s cutoff; final limit = 270s -> safely within both)
    "wall_limit_sec": float(os.environ.get("BLAST_WALL_LIMIT_SEC", "180.0")),
    "wall_cutoff_frac": float(os.environ.get("BLAST_WALL_CUTOFF_FRAC", "0.60")),
}


def _pack_cost_sec(cands: list[tuple[int, str, float]]) -> float:
    """Exact packed cost (max_load + 2k) in seconds for a list of (num, nodeid, dur_sec)."""
    if not cands:
        return 0.0
    dur_ms = {n: int(d * 1000.0 + 0.5) for n, _, d in cands}
    nums = [n for n, _, _ in cands]
    ws = packer.pack_tests(nums, dur_ms)
    if not ws:
        return 0.0
    return (max(sum(dur_ms[n] for n in w) for w in ws) + 2000 * len(ws)) / 1000.0


def _est_test_cpu_sec(nodeid: str, rec_sec: float, seen_mods: set[str]) -> float:
    """Estimate actual single-threaded in-container CPU seconds for running one test."""
    mod_file = nodeid.split("::", 1)[0]
    if mod_file == "tests/test_optimizer.py":
        # Some optimizer tests run multi-core in tests.json, others single-threaded
        base = rec_sec * 1.45 + 0.015
        setup = 0.18 if mod_file not in seen_mods else 0.0
    elif mod_file == "tests/test_executor.py":
        base = rec_sec * 1.15 + 0.01
        setup = 0.25 if mod_file not in seen_mods else 0.0
    else:
        base = rec_sec * 1.05 + 0.005
        setup = 0.04 if mod_file not in seen_mods else 0.0
    return base + setup


def _can_shadow_instrument(
    patched_py: dict[str, tuple[bytes | None, bytes | None]],
    patched_nonpy: dict[str, bytes | None],
) -> bool:
    """Quick AST check whether this commit qualifies for shadow expression instrumentation."""
    if len(patched_py) != 1 or patched_nonpy:
        return False
    rel, (old_b, new_b) = next(iter(patched_py.items()))
    if old_b is None or new_b is None:
        return False
    try:
        old_tree = ast.parse(old_b, filename=rel)
        new_tree = ast.parse(new_b, filename=rel)
        return _find_single_expr_diff(old_tree, new_tree) is not None
    except Exception:
        return False


def _economic_plan(
    candidates: list[tuple[int, str, float]],
    can_shadow: bool,
    cfg: dict[str, Any],
) -> tuple[bool, list[tuple[int, str, float]]]:
    """Economic gate & saving-per-CPU ordering before forking.

    Returns `(should_verify, ordered_candidates_to_pass_to_child)`.
    """
    if not candidates:
        return False, []

    static_cost = _pack_cost_sec(candidates)
    if static_cost <= float(cfg["min_static_cost_to_verify"]):
        return False, []

    # Filter out subprocess-only test that cannot be verified in-memory
    verifiable = [c for c in candidates if not c[1].endswith("::test_lazy_load")]
    if not verifiable:
        return False, []

    max_single = float(cfg["max_single_test_sec"])
    last_test_max = float(cfg["last_test_max_sec"])
    fork_ovh = float(cfg["fork_overhead_sec"])
    min_roi = float(cfg["min_roi_ratio"])
    sh_min_p = int(cfg["shadow_min_passed"])
    sh_stop_p = int(cfg["shadow_early_stop_passed"])
    lap_min_p = int(cfg["laplace_min_passed"])
    lap_alpha = float(cfg["laplace_alpha"])

    # Determine which tests are runnable under single-test caps
    by_dur = sorted(verifiable, key=lambda x: (x[2], x[0]))
    runnable: list[tuple[int, str, float]] = []
    unrunnable: list[tuple[int, str, float]] = [c for c in candidates if c[1].endswith("::test_lazy_load")]

    for idx, c in enumerate(by_dur):
        is_last = (idx == len(by_dur) - 1) and (len(unrunnable) == 0)
        cap = last_test_max if is_last else max_single
        if c[2] <= cap:
            runnable.append(c)
        else:
            unrunnable.append(c)

    if not runnable:
        return False, []

    # Estimate best-case remaining set & CPU cost
    if not unrunnable:
        # Case A: ALL tests in S are runnable -> passing all eliminates entire static_cost!
        seen_m: set[str] = set()
        est_cpu = fork_ovh
        for _, nid, d in runnable:
            est_cpu += _est_test_cpu_sec(nid, d, seen_m)
            seen_m.add(nid.split("::", 1)[0])
        max_saving = static_cost
        can_eliminate_all = True
    elif can_shadow and bool(cfg.get("enable_shadow_pruning", False)) and len(runnable) >= sh_min_p:
        # Case B: Shadow-instrumentable canary proof can eliminate entire static_cost!
        canary_slice = runnable[: min(len(runnable), sh_stop_p)]
        seen_m = set()
        est_cpu = fork_ovh
        for _, nid, d in canary_slice:
            est_cpu += _est_test_cpu_sec(nid, d, seen_m)
            seen_m.add(nid.split("::", 1)[0])
        max_saving = static_cost
        can_eliminate_all = True
    elif bool(cfg.get("enable_laplace_pruning", False)) and len(runnable) >= lap_min_p:
        # Case C: Enough runnable canaries for conservative Laplace pruning of >15s monsters
        m_est = min(len(runnable), int(cfg["max_canary_tests"]))
        p_post = lap_alpha / (m_est + lap_alpha)
        kept_after = [c for c in unrunnable if p_post * 300.0 > c[2]]
        max_saving = static_cost - _pack_cost_sec(kept_after)
        seen_m = set()
        est_cpu = fork_ovh
        for _, nid, d in runnable[:m_est]:
            est_cpu += _est_test_cpu_sec(nid, d, seen_m)
            seen_m.add(nid.split("::", 1)[0])
        can_eliminate_all = len(kept_after) == 0
    else:
        # Case D: Unrunnable tests WILL remain; only marginal drop in pack_cost is achievable
        max_saving = static_cost - _pack_cost_sec(unrunnable)
        seen_m = set()
        est_cpu = fork_ovh
        for _, nid, d in runnable:
            est_cpu += _est_test_cpu_sec(nid, d, seen_m)
            seen_m.add(nid.split("::", 1)[0])
        can_eliminate_all = False

    # Commit-level economic gate: verify only if max_possible_saving > est_cpu * min_roi
    if max_saving <= est_cpu * min_roi:
        return False, []

    # Per-candidate greedy ordering by saving per CPU second (accounting for module reuse!)
    # and per-candidate economic gate in Case D.
    use_exact_marginal = len(candidates) <= 40
    is_canary_mode = bool(unrunnable) and can_eliminate_all
    worker_share = (2.0 / len(runnable)) if can_eliminate_all else 0.0

    # Precompute direct marginal saving for each runnable candidate once
    direct_sav_map: dict[int, float] = {}
    for c in runnable:
        n, _, d = c
        if use_exact_marginal:
            rem = [x for x in candidates if x[0] != n]
            direct_sav_map[n] = static_cost - _pack_cost_sec(rem)
        else:
            direct_sav_map[n] = d

    # Greedy selection by highest saving per CPU second (with module-load amortization),
    # keeping any last-test-rescue candidate (d > max_single) at the end.
    regular = [c for c in runnable if c[2] <= max_single]
    rescue = [c for c in runnable if c[2] > max_single]

    ordered_runnable: list[tuple[int, str, float]] = []
    seen_probe: set[str] = set()
    pool = list(regular)
    while pool:
        best_idx = -1
        best_key: tuple[float, float, int] | None = None
        for idx_p, c in enumerate(pool):
            n, nid, d = c
            c_cpu = _est_test_cpu_sec(nid, d, seen_probe)
            dsav = direct_sav_map[n]
            if (not can_eliminate_all) and (dsav <= c_cpu):
                continue
            if is_canary_mode:
                # In canary mode every passing test contributes equally to the shadow/Laplace
                # proof that eliminates static_cost -> saving per CPU second is proportional to 1 / c_cpu
                roi = static_cost / max(c_cpu, 0.005)
            else:
                eff_sav = dsav + worker_share
                roi = eff_sav / max(c_cpu, 0.005)
            key = (-roi, d, n)
            if best_key is None or key < best_key:
                best_key = key
                best_idx = idx_p
        if best_idx < 0:
            break
        chosen = pool.pop(best_idx)
        ordered_runnable.append(chosen)
        seen_probe.add(chosen[1].split("::", 1)[0])

    for c in sorted(rescue, key=lambda x: (x[2], x[0])):
        n, nid, d = c
        c_cpu = _est_test_cpu_sec(nid, d, seen_probe)
        if can_eliminate_all or direct_sav_map[n] > c_cpu:
            ordered_runnable.append(c)

    if not ordered_runnable:
        return False, []

    # Re-append unrunnable tests at the tail so child knows full candidate list (e.g. is_last check)
    return True, ordered_runnable + unrunnable

# Sentinel exception for instant subTest fail-fast
class _SubTestFastFail(BaseException):
    pass


class _TestTimeout(BaseException):
    pass


# ---------------------------------------------------------------------------
# AST Shadow Expression Instrumentation (pure expression single-site edits)
# ---------------------------------------------------------------------------
_PURE_EXPR_NODES = (
    ast.Compare,
    ast.BoolOp,
    ast.BinOp,
    ast.UnaryOp,
    ast.IfExp,
    ast.Name,
    ast.Attribute,
    ast.Subscript,
    ast.Slice,
    ast.Constant,
    ast.Tuple,
    ast.List,
    ast.Set,
    ast.Dict,
)


def _is_pure_expr(node: ast.AST) -> bool:
    """Return True if `node` is a side-effect-free Python expression (no calls,
    walrus, yield, await, lambdas, or comprehensions)."""
    if not isinstance(node, ast.expr):
        return False
    for sub in ast.walk(node):
        if isinstance(
            sub,
            (
                ast.Call,
                ast.NamedExpr,
                ast.Yield,
                ast.YieldFrom,
                ast.Await,
                ast.Lambda,
                ast.ListComp,
                ast.SetComp,
                ast.DictComp,
                ast.GeneratorExp,
            ),
        ):
            return False
        if isinstance(sub, ast.expr) and not isinstance(sub, _PURE_EXPR_NODES):
            return False
    return True


def _dump(n: ast.AST) -> str:
    return ast.dump(n, include_attributes=False)


def _find_single_expr_diff(
    old_node: ast.AST,
    new_node: ast.AST,
    in_func: bool = False,
) -> tuple[ast.expr, ast.expr, ast.AST, str, int | None] | None:
    """If `old_node` and `new_node` differ at exactly ONE pure expression site inside
    a function body, return `(old_expr, new_expr, parent_new_node, field_name, list_idx)`.
    Otherwise return None."""
    diffs: list[tuple[ast.expr, ast.expr, ast.AST, str, int | None]] = []

    def walk(a: ast.AST, b: ast.AST, parent: ast.AST | None, field: str, idx: int | None, fn_ctx: bool) -> bool:
        if type(a) is not type(b):
            if fn_ctx and parent is not None and _is_pure_expr(a) and _is_pure_expr(b):
                diffs.append((a, b, parent, field, idx))  # type: ignore[arg-type]
                return len(diffs) <= 1
            return False

        now_fn = fn_ctx or isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef))

        # Check all AST fields
        for fname in a._fields:
            va = getattr(a, fname)
            vb = getattr(b, fname)
            if isinstance(va, ast.AST) and isinstance(vb, ast.AST):
                if _dump(va) != _dump(vb):
                    # Try descending deeper first to find the tightest pure expression
                    before_len = len(diffs)
                    if not walk(va, vb, b, fname, None, now_fn):
                        # Descending failed (e.g. operator changed on Compare/BoolOp):
                        # try wrapping this expression itself!
                        del diffs[before_len:]
                        if now_fn and _is_pure_expr(va) and _is_pure_expr(vb):
                            diffs.append((va, vb, b, fname, None))  # type: ignore[arg-type]
                            if len(diffs) > 1:
                                return False
                        else:
                            return False
            elif isinstance(va, list) and isinstance(vb, list):
                if len(va) != len(vb):
                    return False
                for li, (xa, xb) in enumerate(zip(va, vb)):
                    if isinstance(xa, ast.AST) and isinstance(xb, ast.AST):
                        if _dump(xa) != _dump(xb):
                            before_len = len(diffs)
                            if not walk(xa, xb, b, fname, li, now_fn):
                                del diffs[before_len:]
                                if now_fn and _is_pure_expr(xa) and _is_pure_expr(xb):
                                    diffs.append((xa, xb, b, fname, li))  # type: ignore[arg-type]
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

    if not walk(old_node, new_node, None, "", None, in_func) or len(diffs) != 1:
        return None
    return diffs[0]


def build_shadow_code(
    old_bytes: bytes,
    new_bytes: bytes,
    filename: str,
) -> tuple[Any, bool]:
    """Compile `new_bytes` into a code object. If `old_bytes` -> `new_bytes` is a
    single pure-expression edit inside a function body, instrument that expression
    with `__blast_shadow__(lambda: old_expr, lambda: new_expr)` and return `(code, True)`.
    Otherwise return `(compile(new_bytes, filename, 'exec'), False)`."""
    try:
        old_tree = ast.parse(old_bytes, filename=filename)
        new_tree = ast.parse(new_bytes, filename=filename)
    except Exception:
        return compile(new_bytes, filename, "exec"), False

    diff = _find_single_expr_diff(old_tree, new_tree)
    if diff is None:
        return compile(new_bytes, filename, "exec"), False

    old_expr, new_expr, parent, field_name, list_idx = diff
    # Build: __blast_shadow__(lambda: <old_expr>, lambda: <new_expr>)
    empty_args = ast.arguments(
        posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]
    )
    wrapper = ast.Call(
        func=ast.Name(id="__blast_shadow__", ctx=ast.Load()),
        args=[
            ast.Lambda(args=empty_args, body=copy.deepcopy(old_expr)),
            ast.Lambda(args=empty_args, body=copy.deepcopy(new_expr)),
        ],
        keywords=[],
    )
    ast.copy_location(wrapper, new_expr)
    ast.fix_missing_locations(wrapper)

    if list_idx is None:
        setattr(parent, field_name, wrapper)
    else:
        getattr(parent, field_name)[list_idx] = wrapper

    try:
        code = compile(new_tree, filename, "exec")
        return code, True
    except Exception:
        return compile(new_bytes, filename, "exec"), False


# ---------------------------------------------------------------------------
# In-memory module & fixture overlay (runs strictly inside forked child)
# ---------------------------------------------------------------------------
class _SyncExecutor:
    """In-process ProcessPoolExecutor replacement to avoid spawning 16 workers."""

    _do_pickle: bool = False

    def __init__(
        self,
        max_workers: int | None = None,
        mp_context: Any = None,
        initializer: Any = None,
        initargs: tuple = (),
        **kwargs: Any,
    ) -> None:
        if initializer is not None:
            if self._do_pickle:
                try:
                    initargs = pickle.loads(pickle.dumps(initargs))
                except Exception:
                    pass
            initializer(*initargs)

    def __enter__(self) -> "_SyncExecutor":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> concurrent.futures.Future:
        fut: concurrent.futures.Future = concurrent.futures.Future()
        try:
            if self._do_pickle:
                fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
            res = fn(*args, **kwargs)
            if self._do_pickle:
                res = pickle.loads(pickle.dumps(res))
            fut.set_result(res)
        except BaseException as exc:
            fut.set_exception(exc)
        return fut

    def map(self, fn: Any, *iterables: Any, **kwargs: Any) -> Any:
        return map(fn, *iterables)

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        pass


class _MemoryPatchLoader(importlib.abc.Loader):
    def __init__(self, code_obj: Any, filepath: str, is_pkg: bool, pkg_dir: str) -> None:
        self._code = code_obj
        self._filepath = filepath
        self._is_pkg = is_pkg
        self._pkg_dir = pkg_dir

    def create_module(self, spec: Any) -> Any:
        return None

    def exec_module(self, module: Any) -> None:
        module.__file__ = self._filepath
        if self._is_pkg:
            module.__path__ = [self._pkg_dir]
        exec(self._code, module.__dict__)


class _MemoryPatchFinder(importlib.abc.MetaPathFinder):
    def __init__(
        self,
        patched_modules: dict[str, tuple[Any, str, bool, str]],
        deleted_modules: set[str],
    ) -> None:
        self._patched = patched_modules
        self._deleted = deleted_modules

    def find_spec(self, fullname: str, path: Any, target: Any = None) -> Any:
        if fullname in self._deleted:
            raise ModuleNotFoundError(f"Module {fullname} deleted by patch")
        entry = self._patched.get(fullname)
        if entry is None:
            return None
        code_obj, filepath, is_pkg, pkg_dir = entry
        loader = _MemoryPatchLoader(code_obj, filepath, is_pkg, pkg_dir)
        return importlib.machinery.ModuleSpec(
            fullname,
            loader,
            origin=filepath,
            is_package=is_pkg,
        )


def _install_fixture_overlay(sqlglot_root: str, patched_files: dict[str, bytes | None]) -> None:
    """Redirect open()/io.open()/gzip.open() for patched non-Python files in memory."""
    if not patched_files:
        return
    abs_map: dict[str, bytes | None] = {}
    for rel, content in patched_files.items():
        abs_map[os.path.abspath(os.path.join(sqlglot_root, rel))] = content

    orig_open = builtins.open
    orig_io_open = io.open
    orig_gzip_open = gzip.open

    def _maybe_intercept(file_arg: Any, mode: str = "r", encoding: str | None = None, is_gz: bool = False) -> Any:
        if not isinstance(file_arg, (str, os.PathLike)):
            return None
        if any(m in mode for m in ("w", "a", "x", "+")):
            return None
        try:
            abs_p = os.path.abspath(os.fspath(file_arg))
        except Exception:
            return None
        if abs_p not in abs_map:
            return None
        raw = abs_map[abs_p]
        if raw is None:
            raise FileNotFoundError(abs_p)
        if is_gz:
            bio = io.BytesIO(raw)
            return gzip.GzipFile(fileobj=bio, mode="rb") if "b" in mode else io.TextIOWrapper(gzip.GzipFile(fileobj=bio, mode="rb"), encoding=encoding or "utf-8")
        if "b" in mode:
            return io.BytesIO(raw)
        return io.StringIO(raw.decode(encoding or "utf-8"))

    def patched_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        res = _maybe_intercept(file, mode, kwargs.get("encoding"), is_gz=False)
        return res if res is not None else orig_open(file, mode, *args, **kwargs)

    def patched_io_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        res = _maybe_intercept(file, mode, kwargs.get("encoding"), is_gz=False)
        return res if res is not None else orig_io_open(file, mode, *args, **kwargs)

    def patched_gzip_open(filename: Any, mode: str = "rb", *args: Any, **kwargs: Any) -> Any:
        res = _maybe_intercept(filename, mode, kwargs.get("encoding"), is_gz=True)
        return res if res is not None else orig_gzip_open(filename, mode, *args, **kwargs)

    builtins.open = patched_open  # type: ignore[assignment]
    io.open = patched_io_open  # type: ignore[assignment]
    gzip.open = patched_gzip_open  # type: ignore[assignment]


class _FastFailResult(unittest.TestResult):
    """TestResult that aborts immediately on the first failing assertion or subTest."""

    def __init__(self) -> None:
        super().__init__()
        self.failfast = True
        self.any_failed = False

    def addFailure(self, test: Any, err: Any) -> None:
        self.any_failed = True
        super().addFailure(test, err)

    def addError(self, test: Any, err: Any) -> None:
        self.any_failed = True
        super().addError(test, err)

    def addSubTest(self, test: Any, subtest: Any, err: Any) -> None:
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.any_failed = True
            self.shouldStop = True
            raise _SubTestFastFail()


def _proc_cpu_sec() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF)
    rc = resource.getrusage(resource.RUSAGE_CHILDREN)
    return r.ru_utime + r.ru_stime + rc.ru_utime + rc.ru_stime


def _rel_to_module(rel: str) -> tuple[str, bool]:
    no_ext = rel[:-3]
    is_pkg = no_ext.endswith("/__init__")
    if is_pkg:
        no_ext = no_ext[:-9]
    return no_ext.replace("/", "."), is_pkg


def _patch_executor_lazy_setup(cls: Any, full_setup: bool) -> None:
    """For TestExecutor, skip loading the 24 TPC-DS CSVs (~3.4 s) when running
    non-TPCDS tests and TPC-DS fixtures/helpers weren't modified."""
    if full_setup or getattr(cls, "_blast_lazy_patched", False):
        return
    cls._blast_lazy_patched = True

    @classmethod  # type: ignore[misc]
    def _fast_setup_class(c: Any) -> None:
        import ast as _ast
        import csv as _csv
        import duckdb as _duckdb
        from sqlglot.executor.table import Table as _Table
        from tests.helpers import TPCH_SCHEMA, load_sql_fixture_pairs
        from tests.test_executor import DIR_TPCH, open_file

        c.tpch_conn = _duckdb.connect()
        c.tpcds_conn = None
        c.tpch_tables = {}
        c.tpcds_tables = {}

        for table, columns in TPCH_SCHEMA.items():
            file_name = f"{DIR_TPCH}{table}.csv.gz"
            c.tpch_conn.execute(
                f"CREATE VIEW {table} AS SELECT * FROM READ_CSV('{file_name}', delim='|', header=True, columns={columns})"
            )
            reader = _csv.reader(open_file(file_name), delimiter="|")
            rows = []
            ctypes: list[Any] = []
            next(reader)
            for row in reader:
                if not ctypes:
                    for v in row:
                        try:
                            ctypes.append(type(_ast.literal_eval(v)))
                        except (ValueError, SyntaxError):
                            ctypes.append(str)
                rows.append(
                    tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row))
                )
            c.tpch_tables[table] = _Table(columns=columns, rows=rows)

        c.cache = {}
        c.tpch_sqls = list(load_sql_fixture_pairs("optimizer/tpc-h/tpc-h.sql"))
        c.tpcds_sqls = []

    cls.setUpClass = _fast_setup_class


def _child_verify(
    sqlglot_root: str,
    patched_py: dict[str, tuple[bytes | None, bytes | None]],  # rel -> (old_bytes, new_bytes)
    patched_nonpy: dict[str, bytes | None],
    candidates: list[tuple[int, str, float]],  # [(test_num, nodeid, dur_sec), ...] sorted fastest-first
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Execute inside forked child process. Returns JSON-serializable verification report."""
    # Ensure sqlglot_root is at the front of sys.path
    if sqlglot_root not in sys.path:
        sys.path.insert(0, sqlglot_root)
    os.environ["SKIP_INTEGRATION"] = "0"

    # Shadow expression tracking state
    shadow_calls = 0
    shadow_divergences = 0
    shadow_checked = False

    def _blast_shadow(old_fn: Any, new_fn: Any) -> Any:
        nonlocal shadow_calls, shadow_divergences
        shadow_calls += 1
        old_exc = None
        new_exc = None
        old_val = None
        new_val = None
        try:
            old_val = old_fn()
        except BaseException as e:
            old_exc = e
        try:
            new_val = new_fn()
        except BaseException as e:
            new_exc = e
        if old_exc is not None or new_exc is not None:
            if type(old_exc) is not type(new_exc):
                shadow_divergences += 1
            if new_exc is not None:
                raise new_exc
            return new_val
        try:
            if type(old_val) is not type(new_val) or old_val != new_val:
                shadow_divergences += 1
        except Exception:
            shadow_divergences += 1
        return new_val

    builtins.__blast_shadow__ = _blast_shadow  # type: ignore[attr-defined]

    # Compile patched Python modules & build MetaPathFinder
    patched_modules: dict[str, tuple[Any, str, bool, str]] = {}
    deleted_modules: set[str] = set()
    use_shadow = bool(cfg.get("enable_shadow_pruning", False)) and len(patched_py) == 1 and not patched_nonpy

    for rel, (old_b, new_b) in patched_py.items():
        mod_name, is_pkg = _rel_to_module(rel)
        abs_fp = os.path.abspath(os.path.join(sqlglot_root, rel))
        if new_b is None:
            deleted_modules.add(mod_name)
            continue
        if use_shadow and old_b is not None:
            code_obj, did_shadow = build_shadow_code(old_b, new_b, abs_fp)
            shadow_checked = did_shadow
        else:
            try:
                code_obj = compile(new_b, abs_fp, "exec")
            except SyntaxError:
                # Syntax error in patched file -> cannot pass tests
                return {
                    "passed": [],
                    "failed": [candidates[0][0]] if candidates else [],
                    "shadow_checked": False,
                    "shadow_calls": 0,
                    "shadow_divergences": 1,
                    "error": "syntax_error",
                }
        patched_modules[mod_name] = (code_obj, abs_fp, is_pkg, os.path.dirname(abs_fp))

    # Install MetaPathFinder, file-open overlay (BOTH .py and non-.py files so Path.read_text
    # in tests like test_typing.py also sees patched .py sources!), and SyncExecutor
    sys.meta_path.insert(0, _MemoryPatchFinder(patched_modules, deleted_modules))
    all_open_overlay: dict[str, bytes | None] = dict(patched_nonpy)
    for rel, (_, new_b) in patched_py.items():
        all_open_overlay[rel] = new_b
    _install_fixture_overlay(sqlglot_root, all_open_overlay)

    # Enable pickling inside SyncExecutor if serde or any expressions module was touched
    _SyncExecutor._do_pickle = any(
        rel == "sqlglot/serde.py" or rel.startswith("sqlglot/expressions/") for rel in patched_py
    )
    concurrent.futures.ProcessPoolExecutor = _SyncExecutor  # type: ignore[misc]

    full_executor_setup = any(
        rel.startswith(("tests/test_executor.py", "tests/helpers.py", "tests/fixtures/optimizer/tpc-"))
        for rel in (*patched_py.keys(), *patched_nonpy.keys())
    )

    def _alarm_handler(signum: int, frame: Any) -> None:
        raise _TestTimeout()

    signal.signal(signal.SIGALRM, _alarm_handler)

    max_single = float(cfg["max_single_test_sec"])
    last_test_max = float(cfg["last_test_max_sec"])
    max_cpu = float(cfg["max_commit_cpu_sec"])
    max_canaries = int(cfg["max_canary_tests"])
    per_test_timeout = int(cfg["per_test_timeout_sec"])
    sh_stop_passed = int(cfg["shadow_early_stop_passed"])
    sh_stop_calls = int(cfg["shadow_early_stop_calls"])

    # Compute how many tests in candidates are runnable within single-test cap
    passed: list[int] = []
    failed: list[int] = []
    setup_done_classes: set[type] = set()
    mod_cache: dict[str, Any] = {}

    t_start = _proc_cpu_sec()

    # Check if ALL candidates are cheap enough to finish completely (allow slightly
    # higher budget if total recorded runtime <= 2.2 s since proving ALL pass saves 2.0s!)
    total_rec = sum(d for _, _, d in candidates)
    effective_cpu_budget = max(max_cpu, 2.2) if total_rec <= 2.0 else max_cpu
    effective_single_cap = max(max_single, 1.9) if len(candidates) <= 3 and total_rec <= 2.0 else max_single

    n_cands = len(candidates)
    for idx_c, (tnum, nodeid, rec_sec) in enumerate(candidates):
        is_last_remaining = (idx_c == n_cands - 1) and (len(passed) == n_cands - 1)
        # Early stop when shadow equivalence is already strongly proven (0 divergences!)
        if (
            not is_last_remaining
            and shadow_checked
            and shadow_divergences == 0
            and len(passed) >= sh_stop_passed
            and shadow_calls >= sh_stop_calls
        ):
            break
        if is_last_remaining and rec_sec <= last_test_max:
            # Running this single final test can eliminate the entire 2.00 s worker startup!
            pass
        else:
            if rec_sec > effective_single_cap:
                break
            if len(passed) >= max_canaries and len(passed) < n_cands:
                break
            if (_proc_cpu_sec() - t_start) >= effective_cpu_budget:
                break

        # Never falsely pass subprocess tests (they spawn a fresh python outside our in-memory overlay)
        if nodeid.endswith("::test_lazy_load"):
            continue

        # Parse pytest nodeid: "tests/dialects/test_foo.py::TestFoo::test_bar"
        parts = nodeid.split("::")
        if len(parts) != 3:
            continue
        file_rel, cls_name, meth_name = parts
        mod_name = file_rel[:-3].replace("/", ".")

        # Import test module (catches import-time errors in patched sqlglot!)
        try:
            mod = mod_cache.get(mod_name)
            if mod is None:
                mod = importlib.import_module(mod_name)
                mod_cache[mod_name] = mod
            cls = getattr(mod, cls_name)
            if cls not in setup_done_classes:
                if cls_name == "TestExecutor" and "tpcds" not in meth_name:
                    _patch_executor_lazy_setup(cls, full_executor_setup)
                if hasattr(cls, "setUpClass"):
                    signal.alarm(per_test_timeout)
                    cls.setUpClass()
                    signal.alarm(0)
                setup_done_classes.add(cls)
        except BaseException:
            signal.alarm(0)
            failed.append(tnum)
            break

        # Run single test method with fail-fast on subTest & alarm timeout
        res = _FastFailResult()
        test_case = cls(meth_name)
        ok = False
        try:
            signal.alarm(per_test_timeout)
            test_case.run(res)
            signal.alarm(0)
            ok = (not res.any_failed) and res.wasSuccessful()
        except _SubTestFastFail:
            signal.alarm(0)
            ok = False
        except BaseException:
            signal.alarm(0)
            ok = False

        if ok:
            passed.append(tnum)
        else:
            failed.append(tnum)
            # First failure found -> stop immediately to minimize CPU!
            break

    return {
        "passed": passed,
        "failed": failed,
        "shadow_checked": shadow_checked,
        "shadow_calls": shadow_calls,
        "shadow_divergences": shadow_divergences,
        "error": None,
    }


class HybridVerifier:
    """Parent-side coordinator that forks isolated verification children."""

    def __init__(
        self,
        sqlglot_root: Path,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.sqlglot_root = str(sqlglot_root.resolve())
        self.cfg = dict(DEFAULT_VERIFIER_CONFIG)
        if config:
            self.cfg.update(config)
        self.available = os.path.isdir(os.path.join(self.sqlglot_root, "sqlglot")) and hasattr(os, "fork")
        self._preloaded_heavy = False
        self.batch_start_wall = time.monotonic()
        self.wall_cutoff_sec = float(self.cfg["wall_limit_sec"]) * float(self.cfg["wall_cutoff_frac"])
        self._deadline_tripped = False

    def _past_deadline(self) -> bool:
        if self._deadline_tripped:
            return True
        elapsed = time.monotonic() - self.batch_start_wall
        if elapsed >= self.wall_cutoff_sec:
            self._deadline_tripped = True
            print(
                f"[verifier] deadline guard triggered at {elapsed:.2f}s "
                f"(cutoff {self.wall_cutoff_sec:.2f}s = {self.cfg['wall_cutoff_frac']*100:.0f}% "
                f"of {self.cfg['wall_limit_sec']:.0f}s); emitting static selection for remaining commits",
                file=sys.stderr,
            )
            return True
        return False

    def _ensure_heavy_deps_preloaded(self, test_nodeids: list[str]) -> None:
        """Lazily pre-import duckdb/pandas/numpy ONCE in the parent process before fork()
        so forked children inherit them via Copy-on-Write in <1ms instead of paying ~1s each."""
        if self._preloaded_heavy:
            return
        needs_heavy = any(
            nid.startswith(("tests/test_optimizer.py::", "tests/test_executor.py::"))
            for nid in test_nodeids
        )
        if needs_heavy:
            self._preloaded_heavy = True
            for pkg in ("duckdb", "numpy", "pandas"):
                try:
                    importlib.import_module(pkg)
                except Exception:
                    pass

    def verify_candidates(
        self,
        candidates: list[tuple[int, str, float]],  # [(test_num, nodeid, dur_sec), ...]
        patched_py: dict[str, tuple[bytes | None, bytes | None]],
        patched_nonpy: dict[str, bytes | None],
        is_fixture_only: bool = False,
    ) -> list[int]:
        """Verify candidate tests in a forked child and return the pruned test numbers."""
        orig_nums = [n for n, _, _ in candidates]
        if not self.available or not candidates:
            return orig_nums
        if self._past_deadline():
            return orig_nums
        if is_fixture_only and self.cfg["skip_fixture_only"]:
            return orig_nums
        if len(candidates) > int(self.cfg["max_candidates_to_verify"]):
            return orig_nums

        # Economic gate + saving-per-CPU-second ordering
        can_shadow = _can_shadow_instrument(patched_py, patched_nonpy)
        should_verify, ordered = _economic_plan(candidates, can_shadow, self.cfg)
        if not should_verify or not ordered:
            return orig_nums

        # Pre-import duckdb/pandas/numpy once in parent if any optimizer/executor test is runnable
        single_cap = float(self.cfg["last_test_max_sec"])
        runnable_nids = [nid for _, nid, d in ordered if d <= single_cap]
        self._ensure_heavy_deps_preloaded(runnable_nids)

        # Fork isolated child process
        r_fd, w_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            # Child process: isolate fd 1 (stdout) to /dev/null so no test print()
            # can ever leak onto the parent's JSONL stdout stream!
            os.close(r_fd)
            try:
                devnull_fd = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull_fd, 1)
                os.close(devnull_fd)
                sys.stdout = open(os.devnull, "w")
            except Exception:
                pass
            try:
                report = _child_verify(
                    self.sqlglot_root, patched_py, patched_nonpy, ordered, self.cfg
                )
            except BaseException as exc:
                report = {
                    "passed": [],
                    "failed": [ordered[0][0]],
                    "shadow_checked": False,
                    "shadow_calls": 0,
                    "shadow_divergences": 1,
                    "error": repr(exc),
                }
            try:
                payload = json.dumps(report, separators=(",", ":")).encode("utf-8")
                with os.fdopen(w_fd, "wb") as wf:
                    wf.write(payload)
            except Exception:
                pass
            os._exit(0)

        # Parent process
        os.close(w_fd)
        chunks: list[bytes] = []
        with os.fdopen(r_fd, "rb") as rf:
            while True:
                buf = rf.read(65536)
                if not buf:
                    break
                chunks.append(buf)
        _, status = os.waitpid(pid, 0)
        if status != 0 or not chunks:
            return orig_nums

        try:
            report = json.loads(b"".join(chunks).decode("utf-8"))
        except Exception:
            return orig_nums

        passed_set = set(report.get("passed", ()))
        failed_list = report.get("failed", [])
        remaining = [(n, nid, d) for n, nid, d in ordered if n not in passed_set]

        # 1. If any test failed (or child errored): keep ALL remaining tests (drop only proven-passed)
        if failed_list or report.get("error"):
            return sorted(n for n, _, _ in remaining)

        # 2. If ALL candidate tests ran and passed -> empty workers [] (0% miss risk!)
        if not remaining:
            return []

        # 3. Some tests remain unrun, and all `m` run tests passed with 0 failures!
        m = len(passed_set)
        shadow_checked = bool(report.get("shadow_checked", False))
        shadow_calls = int(report.get("shadow_calls", 0))
        shadow_divs = int(report.get("shadow_divergences", 0))

        # 3a. Shadow divergence detected -> NEVER prune any unrun test!
        if shadow_checked and shadow_divs > 0:
            return sorted(n for n, _, _ in remaining)

        # 3b. Shadow-verified equivalence (disabled by default after differential test audit)
        if (
            bool(self.cfg.get("enable_shadow_pruning", False))
            and shadow_checked
            and shadow_divs == 0
            and m >= int(self.cfg["shadow_min_passed"])
            and shadow_calls >= int(self.cfg["shadow_min_calls"])
        ):
            # Include the 2.00 s worker startup cost: emitting [] avoids >= 2.0 s + remaining load
            p_any_fail = 1.0 / (m + shadow_calls + 50.0)
            est_remaining_cost = 2.0 + sum(d for _, _, d in remaining)
            if p_any_fail * 300.0 < est_remaining_cost:
                return []
            kept = [n for n, _, d in remaining if p_any_fail * 300.0 > d]
            return sorted(kept)

        # 3c. Conservative non-shadow Laplace posterior (disabled by default after audit)
        if bool(self.cfg.get("enable_laplace_pruning", False)) and m >= int(self.cfg["laplace_min_passed"]):
            alpha = float(self.cfg["laplace_alpha"])
            p_post = alpha / (m + alpha)
            kept = [n for n, _, d in remaining if p_post * 300.0 > d]
            return sorted(kept)

        return sorted(n for n, _, _ in remaining)
