#!/usr/bin/env python3
"""Build a compact coverage, import, fixture, dialect, expression, and AST map
purely from the pinned sqlglot commit (eb11aec4be8cb2122afe713a4816662c3f0fb2fa).

Must be run inside the same Python/environment as the runtime image (e.g.
python:3.13-slim@sha256:e48f59... with requirements.lock installed and
SKIP_INTEGRATION=0).

No sample patches, commit IDs, or answers are ever read or stored.
"""

from __future__ import annotations

import argparse
import ast
import builtins
import concurrent.futures
import gzip
import inspect
import io
import json
import os
import pickle
import re
import subprocess
import sys
import tempfile
import time
import zlib
from collections import defaultdict
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import expr_profiler as ep  # noqa: E402

PINNED_COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


# ---------------------------------------------------------------------------
# Synchronous ProcessPoolExecutor replacement for coverage tracing
# ---------------------------------------------------------------------------
class _SyncExecutor:
    """Drop-in replacement for ProcessPoolExecutor so worker work executes
    in-process and is captured by sys.monitoring and hooks."""

    def __init__(
        self,
        max_workers: int | None = None,
        mp_context: Any = None,
        initializer: Any = None,
        initargs: tuple = (),
        **kwargs: Any,
    ) -> None:
        if initializer is not None:
            # Mirror ProcessPoolExecutor pickling of initargs across spawn boundary
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
            # Mirror ProcessPoolExecutor IPC serialization so __reduce__/serde paths are traced
            fn_p, args_p, kwargs_p = pickle.loads(pickle.dumps((fn, args, kwargs)))
            res = fn_p(*args_p, **kwargs_p)
            res = pickle.loads(pickle.dumps(res))
            fut.set_result(res)
        except BaseException as exc:
            fut.set_exception(exc)
        return fut

    def map(self, fn: Any, *iterables: Any, **kwargs: Any) -> Any:
        return map(fn, *iterables)

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        pass


# ---------------------------------------------------------------------------
# Pytest plugin for per-test sys.monitoring + fixture/dialect/expr tracing
# ---------------------------------------------------------------------------
class _CoverageTracerPlugin:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root.resolve()
        self.repo_prefix = str(self.repo_root) + os.sep
        self.tool_id = sys.monitoring.COVERAGE_ID

        # Active recording target: either nodeid string or ("class", class_key)
        self.active_key: Any = None
        self.enabled = False

        # Per-target accumulators
        self.lines: dict[Any, set[tuple[str, int]]] = defaultdict(set)
        self.funcs: dict[Any, set[tuple[str, int]]] = defaultdict(set)
        self.fixtures: dict[Any, set[str]] = defaultdict(set)
        self.dialects: dict[Any, set[str]] = defaultdict(set)
        self.exprs: dict[Any, set[str]] = defaultdict(set)

        # Track test outcomes and class membership
        self.test_nodeids: list[str] = []
        self.test_files: dict[str, str] = {}
        self.test_classes: dict[str, str] = {}  # nodeid -> class_key
        self.skipped_nodeids: set[str] = set()
        self.failed_nodeids: list[str] = []

        # Path normalization cache
        self._path_cache: dict[tuple[str, bool], str | None] = {}
        self._dialect_files_cache: dict[type, tuple[str, ...]] = {}
        self._wrapped_classes: set[type] = set()

        # Per-test expression-site profiler
        self.recorder = ep.BuildTimeProfileRecorder()

        # Originals for hooks
        self._orig_open = builtins.open
        self._orig_io_open = io.open
        self._orig_gzip_open = gzip.open
        self._orig_dialect_get: Any = None
        self._orig_dialect_init: Any = None
        self._orig_expr_init: Any = None

    def _rel_path(self, filename: str, *, py_only: bool = False) -> str | None:
        cache_key = (filename, py_only)
        cached = self._path_cache.get(cache_key)
        if cached is not None or cache_key in self._path_cache:
            return cached
        if not filename or filename.startswith("<"):
            self._path_cache[cache_key] = None
            return None
        if py_only and (not os.path.isabs(filename) or not filename.endswith(".py")):
            self._path_cache[cache_key] = None
            return None
        try:
            abs_p = os.path.abspath(filename)
        except Exception:
            self._path_cache[cache_key] = None
            return None
        if abs_p.startswith(self.repo_prefix) and os.path.isfile(abs_p):
            rel = abs_p[len(self.repo_prefix) :].replace("\\", "/")
            self._path_cache[cache_key] = rel
            return rel
        self._path_cache[cache_key] = None
        return None

    def _record_open(self, file_arg: Any) -> None:
        key = self.active_key
        if key is None:
            return
        if isinstance(file_arg, (str, os.PathLike)):
            try:
                path_str = os.fspath(file_arg)
            except Exception:
                return
            if isinstance(path_str, bytes):
                try:
                    path_str = path_str.decode("utf-8", errors="ignore")
                except Exception:
                    return
            rel = self._rel_path(path_str)
            if rel is not None:
                self.fixtures[key].add(rel)

    def _dialect_mro_files(self, cls: type) -> tuple[str, ...]:
        cached = self._dialect_files_cache.get(cls)
        if cached is not None:
            return cached
        files: set[str] = set()
        classes_to_walk: list[type] = [cls]
        for attr in ("tokenizer_class", "parser_class", "generator_class", "Tokenizer", "Parser", "Generator"):
            sub = getattr(cls, attr, None)
            if isinstance(sub, type):
                classes_to_walk.append(sub)
        for c in classes_to_walk:
            for base in getattr(c, "__mro__", ()):
                if base is object:
                    continue
                mod = sys.modules.get(base.__module__)
                fname = getattr(mod, "__file__", None) if mod is not None else None
                if fname:
                    rel = self._rel_path(fname)
                    if rel is not None:
                        files.add(rel)
        res = tuple(files)
        self._dialect_files_cache[cls] = res
        return res

    def _record_dialect_cls(self, cls: type) -> None:
        key = self.active_key
        if key is None:
            return
        dset = self.dialects[key]
        for rel in self._dialect_mro_files(cls):
            dset.add(rel)

    def _on_line(self, code: Any, line_number: int) -> Any:
        key = self.active_key
        if key is None:
            return None
        fname = code.co_filename
        rel = self._rel_path(fname, py_only=True)
        if rel is None:
            return sys.monitoring.DISABLE
        self.lines[key].add((rel, line_number))
        return sys.monitoring.DISABLE

    def _on_py_start(self, code: Any, instruction_offset: int) -> Any:
        key = self.active_key
        if key is None:
            return None
        fname = code.co_filename
        rel = self._rel_path(fname, py_only=True)
        if rel is None:
            return sys.monitoring.DISABLE
        self.funcs[key].add((rel, code.co_firstlineno))
        return sys.monitoring.DISABLE

    def _install_hooks(self) -> None:
        # 1. Replace ProcessPoolExecutor before tests import it
        concurrent.futures.ProcessPoolExecutor = _SyncExecutor  # type: ignore[misc]

        # 2. Wrap open / io.open / gzip.open to track fixture reads
        plugin = self
        orig_open = self._orig_open
        orig_io_open = self._orig_io_open
        orig_gzip_open = self._orig_gzip_open

        def tracked_open(file: Any, *args: Any, **kwargs: Any) -> Any:
            plugin._record_open(file)
            return orig_open(file, *args, **kwargs)

        def tracked_io_open(file: Any, *args: Any, **kwargs: Any) -> Any:
            plugin._record_open(file)
            return orig_io_open(file, *args, **kwargs)

        def tracked_gzip_open(filename: Any, *args: Any, **kwargs: Any) -> Any:
            plugin._record_open(filename)
            return orig_gzip_open(filename, *args, **kwargs)

        builtins.open = tracked_open  # type: ignore[assignment]
        io.open = tracked_io_open  # type: ignore[assignment]
        gzip.open = tracked_gzip_open  # type: ignore[assignment]

        # 2b. Install AST expression-site profiler BEFORE importing sqlglot
        builtins.__blast_bp__ = self.recorder.branch_probe  # type: ignore[attr-defined]
        builtins.__blast_cp__ = self.recorder.compare_probe  # type: ignore[attr-defined]
        sys.meta_path.insert(0, ep.ProfilingMetaPathFinder(self.repo_root, self.recorder))

        # 3. Hook Dialect & Expression in sqlglot
        import sqlglot.dialects.dialect as dialect_mod
        import sqlglot.expressions.core as expr_core

        orig_get_or_raise = dialect_mod.Dialect.get_or_raise
        orig_dialect_init = dialect_mod.Dialect.__init__
        orig_expr_init = expr_core.Expr.__init__

        @classmethod  # type: ignore[misc]
        def tracked_get_or_raise(cls: Any, dialect: Any = None) -> Any:
            inst = orig_get_or_raise.__func__(cls, dialect)
            plugin._record_dialect_cls(type(inst))
            return inst

        def tracked_dialect_init(self_d: Any, *args: Any, **kwargs: Any) -> Any:
            plugin._record_dialect_cls(type(self_d))
            return orig_dialect_init(self_d, *args, **kwargs)

        def tracked_expr_init(self_e: Any, **args: Any) -> Any:
            key = plugin.active_key
            if key is not None:
                plugin.exprs[key].add(self_e.__class__.__name__)
            return orig_expr_init(self_e, **args)

        dialect_mod.Dialect.get_or_raise = tracked_get_or_raise  # type: ignore[assignment]
        dialect_mod.Dialect.__init__ = tracked_dialect_init  # type: ignore[assignment]
        expr_core.Expr.__init__ = tracked_expr_init  # type: ignore[assignment]

        # 4. Install sys.monitoring callbacks
        sys.monitoring.use_tool_id(self.tool_id, "blast_radius_mapgen")
        sys.monitoring.register_callback(
            self.tool_id, sys.monitoring.events.LINE, self._on_line
        )
        sys.monitoring.register_callback(
            self.tool_id, sys.monitoring.events.PY_START, self._on_py_start
        )

    def _start_tracing(self, key: Any) -> None:
        self.active_key = key
        self.recorder.switch_target(key)
        sys.monitoring.restart_events()
        sys.monitoring.set_events(
            self.tool_id,
            sys.monitoring.events.LINE | sys.monitoring.events.PY_START,
        )
        self.enabled = True

    def _stop_tracing(self) -> None:
        self.recorder.stop()
        if self.enabled:
            sys.monitoring.set_events(self.tool_id, sys.monitoring.events.NO_EVENTS)
            self.enabled = False
        self.active_key = None

    def pytest_configure(self, config: Any) -> None:
        self._install_hooks()

    def pytest_collection_modifyitems(self, session: Any, config: Any, items: list[Any]) -> None:
        for item in items:
            nodeid = item.nodeid
            self.test_nodeids.append(nodeid)
            self.test_files[nodeid] = nodeid.split("::", 1)[0]
            cls = getattr(item, "cls", None)
            if cls is not None:
                self.test_classes[nodeid] = f"{cls.__module__}.{cls.__qualname__}"

    def _make_hookwrapper_fixture_setup(self) -> None:
        pass

    def pytest_fixture_setup(self, fixturedef: Any, request: Any) -> Any:
        # Hookimpl decorated dynamically in run_worker via hookwrapper=True
        if fixturedef.argname.startswith("_unittest_setUpClass_fixture_") and request.cls is not None:
            cls = request.cls
            ckey = f"{cls.__module__}.{cls.__qualname__}"
            prev_key = self.active_key
            prev_enabled = self.enabled
            self._start_tracing(("class", ckey))
            try:
                yield
            finally:
                if prev_enabled and prev_key is not None:
                    self._start_tracing(prev_key)
                else:
                    self._stop_tracing()
        else:
            yield

    def pytest_runtest_setup(self, item: Any) -> None:
        self._start_tracing(item.nodeid)

    def pytest_runtest_call(self, item: Any) -> None:
        if self.active_key != item.nodeid or not self.enabled:
            self._start_tracing(item.nodeid)

    def pytest_runtest_teardown(self, item: Any, nextitem: Any) -> None:
        self._stop_tracing()

    def pytest_runtest_makereport(self, item: Any, call: Any) -> None:
        if call.excinfo is not None:
            if call.excinfo.typename == "Skipped":
                self.skipped_nodeids.add(item.nodeid)
            elif call.when in ("setup", "call"):
                self.failed_nodeids.append(f"{item.nodeid} ({call.when}: {call.excinfo.typename})")

    def pytest_sessionfinish(self, session: Any, exitstatus: int) -> None:
        self._stop_tracing()
        try:
            sys.monitoring.free_tool_id(self.tool_id)
        except Exception:
            pass
        builtins.open = self._orig_open
        io.open = self._orig_io_open
        gzip.open = self._orig_gzip_open


def run_worker(sqlglot_dir: Path, shard_files: list[str], shard_out: Path) -> None:
    import pytest

    os.environ["SKIP_INTEGRATION"] = "0"
    sys.path.insert(0, str(sqlglot_dir.resolve()))
    os.chdir(sqlglot_dir)

    plugin = _CoverageTracerPlugin(sqlglot_dir)
    _CoverageTracerPlugin.pytest_fixture_setup = pytest.hookimpl(hookwrapper=True)(
        _CoverageTracerPlugin.pytest_fixture_setup
    )
    args = ["-q", "-p", "no:cacheprovider", "--no-header", "--tb=short", "-o", "addopts=", *shard_files]
    rc = pytest.main(args, plugins=[plugin])
    if plugin.failed_nodeids:
        print(
            f"[worker] WARNING: {len(plugin.failed_nodeids)} failures (rc={rc}): "
            f"{plugin.failed_nodeids[:5]}",
            file=sys.stderr,
        )

    # Merge ("class", cls_key) traces into every test belonging to that class
    for nodeid, cls_key in plugin.test_classes.items():
        ck = ("class", cls_key)
        if ck in plugin.lines:
            plugin.lines[nodeid].update(plugin.lines[ck])
        if ck in plugin.funcs:
            plugin.funcs[nodeid].update(plugin.funcs[ck])
        if ck in plugin.fixtures:
            plugin.fixtures[nodeid].update(plugin.fixtures[ck])
        if ck in plugin.dialects:
            plugin.dialects[nodeid].update(plugin.dialects[ck])
        if ck in plugin.exprs:
            plugin.exprs[nodeid].update(plugin.exprs[ck])
        if ck in plugin.recorder.records:
            dst = plugin.recorder.records.setdefault(nodeid, {})
            for sid, fl in plugin.recorder.records[ck].items():
                dst[sid] = dst.get(sid, 0) | fl

    # Keep only per-test (string) keys
    out = {
        "test_nodeids": plugin.test_nodeids,
        "test_files": plugin.test_files,
        "skipped_nodeids": sorted(plugin.skipped_nodeids),
        "failed_nodeids": plugin.failed_nodeids,
        "lines": {k: sorted(v) for k, v in plugin.lines.items() if isinstance(k, str)},
        "funcs": {k: sorted(v) for k, v in plugin.funcs.items() if isinstance(k, str)},
        "fixtures": {k: sorted(v) for k, v in plugin.fixtures.items() if isinstance(k, str)},
        "dialects": {k: sorted(v) for k, v in plugin.dialects.items() if isinstance(k, str)},
        "exprs": {k: sorted(v) for k, v in plugin.exprs.items() if isinstance(k, str)},
        "prof_sites": plugin.recorder.sites,
        "prof_records": {k: v for k, v in plugin.recorder.records.items() if isinstance(k, str)},
        "prof_inactive_sids": sorted(plugin.recorder.inactive_sids),
    }
    shard_out.write_bytes(pickle.dumps(out, protocol=4))


# ---------------------------------------------------------------------------
# Static AST, import graph, class hierarchy, and base source indexing
# ---------------------------------------------------------------------------
def _rel_to_module(rel: str) -> str:
    assert rel.endswith(".py")
    no_ext = rel[:-3]
    if no_ext.endswith("/__init__"):
        no_ext = no_ext[:-9]
    return no_ext.replace("/", ".")


def _resolve_relative_import(cur_mod: str, is_pkg: bool, level: int, mod: str | None) -> str:
    parts = cur_mod.split(".") if cur_mod else []
    if not is_pkg:
        parts = parts[:-1]
    if level > 1:
        parts = parts[: -(level - 1)]
    if mod:
        parts.extend(mod.split("."))
    return ".".join(p for p in parts if p)


def _extract_ast_info(rel: str, src: bytes, module_name: str, is_pkg: bool) -> dict[str, Any]:
    tree = ast.parse(src, filename=rel)
    funcs: list[tuple[int, int, str]] = []
    classes: list[tuple[int, int, str, list[str]]] = []
    names_referenced_by: dict[str, set[str]] = defaultdict(set)
    module_defs: dict[str, tuple[int, int, str]] = {}
    imports: set[str] = set()
    exports: list[str] | None = None
    fixture_refs: set[str] = set()

    # Walk scopes
    scope_stack: list[str] = []

    def _base_name(node: ast.expr) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return f"{_base_name(node.value)}.{node.attr}"
        return ""

    def visit_body(stmts: list[ast.stmt], is_module: bool = False) -> None:
        nonlocal exports
        for stmt in stmts:
            start = stmt.lineno
            end = stmt.end_lineno or start
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Include decorators in start line
                if stmt.decorator_list:
                    start = min(start, *(d.lineno for d in stmt.decorator_list))
                qname = ".".join((*scope_stack, stmt.name))
                funcs.append((start, end, qname))
                if is_module:
                    module_defs[stmt.name] = (start, end, "func")
                # Collect all identifiers referenced inside this function
                for sub in ast.walk(stmt):
                    if isinstance(sub, ast.Name):
                        names_referenced_by[sub.id].add(qname)
                    elif isinstance(sub, ast.Attribute):
                        names_referenced_by[sub.attr].add(qname)
                    elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        _check_fixture_str(sub.value, fixture_refs)
                scope_stack.append(stmt.name)
                visit_body(stmt.body, is_module=False)
                scope_stack.pop()
            elif isinstance(stmt, ast.ClassDef):
                if stmt.decorator_list:
                    start = min(start, *(d.lineno for d in stmt.decorator_list))
                qname = ".".join((*scope_stack, stmt.name))
                base_names = [_base_name(b) for b in stmt.bases if _base_name(b)]
                classes.append((start, end, qname, base_names))
                if is_module:
                    module_defs[stmt.name] = (start, end, "class")
                scope_stack.append(stmt.name)
                visit_body(stmt.body, is_module=False)
                scope_stack.pop()
            else:
                if is_module:
                    if isinstance(stmt, ast.Assign):
                        for t in stmt.targets:
                            if isinstance(t, ast.Name):
                                module_defs[t.id] = (start, end, "var")
                                if t.id == "__all__" and isinstance(stmt.value, (ast.List, ast.Tuple, ast.Set)):
                                    exports = [
                                        elt.value
                                        for elt in stmt.value.elts
                                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                                    ]
                    elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                        module_defs[stmt.target.id] = (start, end, "var")
                # Collect imports and fixture string refs anywhere in statement
                for sub in ast.walk(stmt):
                    if isinstance(sub, ast.Import):
                        for alias in sub.names:
                            imports.add(alias.name)
                    elif isinstance(sub, ast.ImportFrom):
                        base = (
                            _resolve_relative_import(module_name, is_pkg, sub.level, sub.module)
                            if sub.level > 0
                            else (sub.module or "")
                        )
                        if base:
                            imports.add(base)
                            for alias in sub.names:
                                if alias.name != "*":
                                    imports.add(f"{base}.{alias.name}")
                    elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        _check_fixture_str(sub.value, fixture_refs)
                    elif not is_module and scope_stack:
                        # Class body reference: attribute to class
                        cls_q = ".".join(scope_stack)
                        if isinstance(sub, ast.Name):
                            names_referenced_by[sub.id].add(cls_q)
                        elif isinstance(sub, ast.Attribute):
                            names_referenced_by[sub.attr].add(cls_q)

    visit_body(tree.body, is_module=True)

    # Also capture any imports inside function bodies (lazy imports)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = (
                _resolve_relative_import(module_name, is_pkg, node.level, node.module)
                if node.level > 0
                else (node.module or "")
            )
            if base:
                imports.add(base)
                for alias in node.names:
                    if alias.name != "*":
                        imports.add(f"{base}.{alias.name}")

    return {
        "funcs": funcs,
        "classes": classes,
        "names_referenced_by": {k: sorted(v) for k, v in names_referenced_by.items()},
        "module_defs": module_defs,
        "imports": sorted(imports),
        "exports": exports,
        "fixture_refs": sorted(fixture_refs),
    }


def _check_fixture_str(val: str, out: set[str]) -> None:
    if len(val) > 120 or "\n" in val:
        return
    if any(ext in val for ext in (".sql", ".json", ".csv", ".gz")):
        out.add(val)


def build_static_maps(sqlglot_dir: Path) -> dict[str, Any]:
    file_to_module: dict[str, str] = {}
    module_to_file: dict[str, str] = {}
    ast_index: dict[str, dict[str, Any]] = {}
    base_sources: dict[str, bytes] = {}
    direct_imports: dict[str, set[str]] = defaultdict(set)

    py_files: list[Path] = []
    for sub in ("sqlglot", "tests"):
        d = sqlglot_dir / sub
        if d.exists():
            py_files.extend(sorted(d.rglob("*.py")))
    # Also include root setup.py / sqlglotc/setup.py if present
    for extra in ("setup.py", "sqlglotc/setup.py"):
        p = sqlglot_dir / extra
        if p.exists():
            py_files.append(p)

    for p in py_files:
        rel = p.relative_to(sqlglot_dir).as_posix()
        mod = _rel_to_module(rel)
        file_to_module[rel] = mod
        module_to_file[mod] = rel

    all_modules = set(module_to_file.keys())

    # First pass: parse ASTs and store compressed base sources
    for p in py_files:
        rel = p.relative_to(sqlglot_dir).as_posix()
        raw = p.read_bytes()
        base_sources[rel] = zlib.compress(raw, level=9)
        mod = file_to_module[rel]
        is_pkg = rel.endswith("/__init__.py")
        info = _extract_ast_info(rel, raw, mod, is_pkg)
        ast_index[rel] = {
            "funcs": info["funcs"],
            "classes": info["classes"],
            "names_referenced_by": info["names_referenced_by"],
            "module_defs": info["module_defs"],
            "exports": info["exports"],
            "fixture_refs": info["fixture_refs"],
        }
        # Resolve imported module names to known repo modules
        for imp in info["imports"]:
            if imp in all_modules:
                direct_imports[mod].add(imp)
            else:
                # Try parent package (e.g. `from sqlglot.foo import Bar` -> `sqlglot.foo`)
                parent = imp.rsplit(".", 1)[0] if "." in imp else ""
                if parent in all_modules:
                    direct_imports[mod].add(parent)
            # Importing `sqlglot.a.b` also executes `sqlglot` and `sqlglot.a`
            parts = imp.split(".")
            for i in range(1, len(parts)):
                anc = ".".join(parts[:i])
                if anc in all_modules:
                    direct_imports[mod].add(anc)

    # Also add implicit lazy dialect edges from sqlglot.dialects -> each dialect module
    # (note: we do NOT force sqlglot.dialects to depend on all dialects statically because
    # dialects are lazy-loaded; instead each dialect depends on its parent dialect, parser, generator, typing)

    # Reverse import graph: module -> direct importers
    rev_imports: dict[str, set[str]] = defaultdict(set)
    for importer, imported_set in direct_imports.items():
        for target in imported_set:
            if target != importer:
                rev_imports[target].add(importer)

    # Transitive closure of module_importers
    module_importers: dict[str, list[str]] = {}
    for mod in all_modules:
        visited: set[str] = set()
        stack = list(rev_imports.get(mod, ()))
        while stack:
            cur = stack.pop()
            if cur not in visited:
                visited.add(cur)
                stack.extend(rev_imports.get(cur, ()))
        module_importers[mod] = sorted(visited)

    # Transitive deps for each test file
    test_file_deps: dict[str, list[str]] = {}
    for rel, mod in file_to_module.items():
        if not rel.startswith("tests/"):
            continue
        visited = set()
        stack = list(direct_imports.get(mod, ()))
        while stack:
            cur = stack.pop()
            if cur not in visited:
                visited.add(cur)
                stack.extend(direct_imports.get(cur, ()))
        test_file_deps[rel] = sorted(m for m in visited if m.startswith("sqlglot"))

    # Exact runtime class hierarchy across sqlglot/ via real Python MRO
    import importlib
    import pkgutil

    sys.path.insert(0, str(sqlglot_dir.resolve()))
    import sqlglot as _sg

    repo_prefix = str(sqlglot_dir.resolve()) + os.sep
    all_classes: set[type] = set()

    def _collect_classes_from_obj(obj: Any) -> None:
        if isinstance(obj, type):
            if obj in all_classes:
                return
            all_classes.add(obj)
            for v in obj.__dict__.values():
                if isinstance(v, type):
                    _collect_classes_from_obj(v)

    # Walk all modules under sqlglot package
    for mod_info in pkgutil.walk_packages(_sg.__path__, prefix="sqlglot."):
        if mod_info.name == "sqlglot.__main__":
            continue
        try:
            m = importlib.import_module(mod_info.name)
        except Exception:
            continue
        for v in vars(m).values():
            if isinstance(v, type):
                _collect_classes_from_obj(v)
    for v in vars(_sg).values():
        if isinstance(v, type):
            _collect_classes_from_obj(v)

    def _cls_id(c: type) -> str | None:
        mod = sys.modules.get(c.__module__)
        fname = getattr(mod, "__file__", None) if mod is not None else None
        if not fname:
            return None
        abs_p = os.path.abspath(fname)
        if not abs_p.startswith(repo_prefix):
            return None
        rel = abs_p[len(repo_prefix) :].replace("\\", "/")
        return f"{rel}:{c.__qualname__}"

    class_subclasses_sets: dict[str, set[str]] = defaultdict(set)
    expr_class_file: dict[str, str] = {}
    for rel, info in ast_index.items():
        if rel.startswith("sqlglot/expressions/"):
            for _, _, qname, _ in info["classes"]:
                if "." not in qname:
                    expr_class_file[qname] = rel

    for c in all_classes:
        cid = _cls_id(c)
        if cid is None:
            continue
        for base in c.__mro__[1:]:
            if base is object:
                continue
            bid = _cls_id(base)
            if bid is not None and bid != cid:
                class_subclasses_sets[bid].add(cid)

    class_subclasses: dict[str, list[str]] = {
        k: sorted(v) for k, v in class_subclasses_sets.items()
    }

    # Static fixture references: match all actual files under tests/fixtures/
    all_fixture_files: list[str] = []
    fixtures_root = sqlglot_dir / "tests" / "fixtures"
    if fixtures_root.exists():
        all_fixture_files = sorted(
            p.relative_to(sqlglot_dir).as_posix()
            for p in fixtures_root.rglob("*")
            if p.is_file()
        )

    # Map static references to Expr names in tests/ (catches tests that inspect exp.Foo without instantiating)
    test_file_expr_refs: dict[str, set[str]] = defaultdict(set)
    expr_names = set(expr_class_file.keys())
    for rel, info in ast_index.items():
        if not rel.startswith("tests/"):
            continue
        for name in info["names_referenced_by"]:
            if name in expr_names:
                test_file_expr_refs[name].add(rel)

    return {
        "file_to_module": file_to_module,
        "module_to_file": module_to_file,
        "module_importers": module_importers,
        "test_file_deps": test_file_deps,
        "ast_index": ast_index,
        "class_subclasses": class_subclasses,
        "expr_class_file": expr_class_file,
        "all_fixture_files": all_fixture_files,
        "test_file_expr_refs": {k: sorted(v) for k, v in test_file_expr_refs.items()},
        "base_sources": base_sources,
    }


def _find_enclosing_func(funcs: list[tuple[int, int, str]], lineno: int) -> str | None:
    best: tuple[int, str] | None = None
    for start, end, qname in funcs:
        if start <= lineno <= end:
            span = end - start
            if best is None or span < best[0]:
                best = (span, qname)
    return best[1] if best is not None else None


def build_map(
    sqlglot_dir: Path,
    output_path: Path,
    workers: int = 4,
    verify_tests_json: Path | None = None,
) -> None:
    t0 = time.perf_counter()
    sqlglot_dir = sqlglot_dir.resolve()
    tests_dir = sqlglot_dir / "tests"

    # Discover all pytest test files
    test_files = sorted(
        p.relative_to(sqlglot_dir).as_posix()
        for p in tests_dir.rglob("test_*.py")
        if p.is_file()
    )
    print(f"[build_map] Found {len(test_files)} test files in {sqlglot_dir}", file=sys.stderr)

    # Balance shards: put the 3 heaviest files in dedicated shards when workers >= 4
    heavy = ["tests/test_executor.py", "tests/test_parser.py", "tests/test_optimizer.py"]
    shards: list[list[str]] = [[] for _ in range(max(1, workers))]
    remaining: list[str] = []
    h_idx = 0
    for tf in test_files:
        if tf in heavy and workers >= 4:
            shards[h_idx].append(tf)
            h_idx += 1
        else:
            remaining.append(tf)
    # Distribute remaining files across non-heavy shards (or all shards if workers < 4)
    start_shard = h_idx if (workers >= 4 and h_idx < workers) else 0
    for i, tf in enumerate(remaining):
        shards[start_shard + (i % (workers - start_shard))].append(tf)
    shards = [s for s in shards if s]

    print(
        f"[build_map] Launching {len(shards)} parallel coverage worker(s): "
        + ", ".join(str(len(s)) for s in shards)
        + " files per shard",
        file=sys.stderr,
    )

    with tempfile.TemporaryDirectory(prefix="blast_map_") as tmpdir:
        tmp = Path(tmpdir)
        procs: list[tuple[subprocess.Popen, Path, int]] = []
        for idx, sfiles in enumerate(shards):
            shard_out = tmp / f"shard_{idx}.pkl"
            cmd = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--worker",
                "--sqlglot-dir",
                str(sqlglot_dir),
                "--shard-out",
                str(shard_out),
                "--shard-files",
                *sfiles,
            ]
            p = subprocess.Popen(cmd)
            procs.append((p, shard_out, idx))

        shard_results: list[dict[str, Any]] = []
        for p, shard_out, idx in procs:
            rc = p.wait()
            if rc != 0:
                raise RuntimeError(f"Worker shard {idx} failed with exit code {rc}")
            shard_results.append(pickle.loads(shard_out.read_bytes()))

    t_cov = time.perf_counter() - t0
    print(f"[build_map] Dynamic coverage finished in {t_cov:.1f}s", file=sys.stderr)

    # Collect all non-skipped test nodeids in deterministic sorted order
    all_skipped: set[str] = set()
    all_nodeids_raw: list[str] = []
    nodeid_to_file: dict[str, str] = {}
    all_failed: list[str] = []
    for sr in shard_results:
        all_skipped.update(sr["skipped_nodeids"])
        all_nodeids_raw.extend(sr["test_nodeids"])
        nodeid_to_file.update(sr["test_files"])
        all_failed.extend(sr["failed_nodeids"])

    if all_failed:
        print(
            f"[build_map] WARNING: {len(all_failed)} test failures during tracing: {all_failed[:10]}",
            file=sys.stderr,
        )

    test_ids = sorted(nid for nid in set(all_nodeids_raw) if nid not in all_skipped)
    test_files_list = [nodeid_to_file[nid] for nid in test_ids]
    id_to_idx = {nid: i for i, nid in enumerate(test_ids)}
    print(
        f"[build_map] Collected {len(test_ids)} active tests, {len(all_skipped)} skipped",
        file=sys.stderr,
    )

    if verify_tests_json is not None:
        tj = json.loads(verify_tests_json.read_text("utf-8"))
        tj_ids = {t["id"] for t in tj["tests"]}
        missing = tj_ids - set(test_ids)
        extra = set(test_ids) - tj_ids
        print(
            f"[build_map] Verification vs tests.json: missing={len(missing)}, extra={len(extra)}",
            file=sys.stderr,
        )
        if missing:
            print(f"  MISSING: {sorted(missing)[:10]}", file=sys.stderr)
        if extra:
            print(f"  EXTRA: {sorted(extra)[:10]}", file=sys.stderr)

    # Build static AST / import / class / source maps
    print("[build_map] Building static AST, import graph, and compressed base sources...", file=sys.stderr)
    static = build_static_maps(sqlglot_dir)
    ast_index = static["ast_index"]

    # Build bitset maps from shard dynamic data
    line_to_tests: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    func_to_tests: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    class_to_tests: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    module_exec_tests: dict[str, int] = defaultdict(int)
    fixture_to_tests: dict[str, int] = defaultdict(int)
    dialect_to_tests: dict[str, int] = defaultdict(int)
    expr_to_tests: dict[str, int] = defaultdict(int)
    test_file_to_tests: dict[str, int] = defaultdict(int)

    # Build-time per-test expression-site profiles:
    # site_acc[(rel, lineno, col)] = [is_compare, seen, false, true, eq, is2eq_unsafe, eq2is_unsafe, order_unsafe]
    prof_acc: dict[tuple[str, int, int], list[Any]] = {}
    prof_inactive_sites: set[tuple[str, int, int]] = set()

    for idx, tfile in enumerate(test_files_list):
        test_file_to_tests[tfile] |= 1 << idx

    # Pre-index firstlineno -> qualname per file for fast PY_START resolution
    firstline_to_qname: dict[str, dict[int, str]] = {}
    for rel, info in ast_index.items():
        fl: dict[int, str] = {}
        for start, end, qname in info["funcs"]:
            fl[start] = qname
        firstline_to_qname[rel] = fl

    for sr in shard_results:
        for nodeid, pairs in sr["lines"].items():
            idx = id_to_idx.get(nodeid)
            if idx is None:
                continue
            bit = 1 << idx
            for rel, lineno in pairs:
                line_to_tests[rel][lineno] |= bit
                module_exec_tests[rel] |= bit

        for nodeid, pairs in sr["funcs"].items():
            idx = id_to_idx.get(nodeid)
            if idx is None:
                continue
            bit = 1 << idx
            for rel, fline in pairs:
                info = ast_index.get(rel)
                if info is None:
                    continue
                qname = firstline_to_qname[rel].get(fline) or _find_enclosing_func(info["funcs"], fline)
                if qname:
                    func_to_tests[rel][qname] |= bit
                    # Attribute to enclosing class(es)
                    parts = qname.split(".")
                    if len(parts) >= 2:
                        cls_q = ".".join(parts[:-1])
                        class_to_tests[rel][cls_q] |= bit

        for nodeid, flist in sr["fixtures"].items():
            idx = id_to_idx.get(nodeid)
            if idx is None:
                continue
            bit = 1 << idx
            for frel in flist:
                fixture_to_tests[frel] |= bit

        for nodeid, dlist in sr["dialects"].items():
            idx = id_to_idx.get(nodeid)
            if idx is None:
                continue
            bit = 1 << idx
            for drel in dlist:
                dialect_to_tests[drel] |= bit

        for nodeid, elist in sr["exprs"].items():
            idx = id_to_idx.get(nodeid)
            if idx is None:
                continue
            bit = 1 << idx
            for ename in elist:
                expr_to_tests[ename] |= bit

        # Merge expression-site profiles from this shard
        prof_sites = sr.get("prof_sites", ())
        for sid in sr.get("prof_inactive_sids", ()):
            if 0 <= sid < len(prof_sites):
                rel, ln, col, _is_cmp = prof_sites[sid]
                prof_inactive_sites.add((rel, ln, col))
        for nodeid, sid_flags in sr.get("prof_records", {}).items():
            idx = id_to_idx.get(nodeid)
            if idx is None:
                continue
            bit = 1 << idx
            for sid, fl in sid_flags.items():
                rel, ln, col, is_cmp = prof_sites[sid]
                key = (rel, ln, col)
                acc = prof_acc.get(key)
                if acc is None:
                    acc = [is_cmp, 0, 0, 0, 0, 0, 0, 0]
                    prof_acc[key] = acc
                elif is_cmp:
                    acc[0] = True
                acc[1] |= bit
                if fl & ep.BIT_FALSE:
                    acc[2] |= bit
                if fl & ep.BIT_TRUE:
                    acc[3] |= bit
                if fl & ep.BIT_EQ:
                    acc[4] |= bit
                if fl & ep.BIT_IS_TO_EQ_UNSAFE:
                    acc[5] |= bit
                if fl & ep.BIT_EQ_TO_IS_UNSAFE:
                    acc[6] |= bit
                if fl & ep.BIT_ORDER_UNSAFE:
                    acc[7] |= bit

    # Also roll line_to_tests into func_to_tests and class_to_tests so every
    # function/class covers all tests that hit any line inside its span
    for rel, lmap in line_to_tests.items():
        info = ast_index.get(rel)
        if not info:
            continue
        funcs = info["funcs"]
        classes = info["classes"]
        for lineno, bits in lmap.items():
            for start, end, qname in funcs:
                if start <= lineno <= end:
                    func_to_tests[rel][qname] |= bits
            for start, end, cqname, _ in classes:
                if start <= lineno <= end:
                    class_to_tests[rel][cqname] |= bits

    # Link sqlglot/typing/<d>.py to dialect_to_tests of sqlglot/dialects/<d>.py + test_typing.py
    typing_test_bits = test_file_to_tests.get("tests/test_typing.py", 0)
    for rel in list(static["file_to_module"].keys()):
        if rel.startswith("sqlglot/typing/") and rel.endswith(".py"):
            dialect_to_tests[rel] |= typing_test_bits
            stem = Path(rel).stem
            if stem != "__init__":
                d_rel = f"sqlglot/dialects/{stem}.py"
                if d_rel in dialect_to_tests:
                    dialect_to_tests[rel] |= dialect_to_tests[d_rel]

    # Also ensure test_lazy_load is linked to sqlglot/__init__.py & sqlglot/dialects/dialect.py
    lazy_idx = id_to_idx.get("tests/dialects/test_dialect.py::TestDialect::test_lazy_load")
    if lazy_idx is not None:
        lazy_bit = 1 << lazy_idx
        for core_rel in ("sqlglot/__init__.py", "sqlglot/dialects/__init__.py", "sqlglot/dialects/dialect.py"):
            module_exec_tests[core_rel] |= lazy_bit

    # Strip fixture_refs from ast_index (not needed at runtime)
    clean_ast_index: dict[str, dict[str, Any]] = {}
    for rel, info in ast_index.items():
        clean_ast_index[rel] = {
            "funcs": info["funcs"],
            "classes": info["classes"],
            "names_referenced_by": info["names_referenced_by"],
            "module_defs": info["module_defs"],
            "exports": info["exports"],
        }

    # Pack expression-site profiles (excluding any site hit during import/collection time)
    expr_profiles: dict[str, dict[tuple[int, int], tuple[int, ...]]] = defaultdict(dict)
    skipped_inactive = 0
    for key, acc in prof_acc.items():
        if key in prof_inactive_sites:
            skipped_inactive += 1
            continue
        rel, ln, col = key
        if acc[0]:
            expr_profiles[rel][(ln, col)] = (
                acc[1], acc[2], acc[3], acc[4], acc[5], acc[6], acc[7]
            )
        else:
            expr_profiles[rel][(ln, col)] = (acc[1], acc[2], acc[3])

    # Build final compact map dict
    final_map = {
        "version": 2,
        "commit": PINNED_COMMIT,
        "test_ids": test_ids,
        "test_files": test_files_list,
        "skipped_ids": sorted(all_skipped),
        "line_to_tests": {k: dict(v) for k, v in line_to_tests.items()},
        "func_to_tests": {k: dict(v) for k, v in func_to_tests.items()},
        "class_to_tests": {k: dict(v) for k, v in class_to_tests.items()},
        "module_exec_tests": dict(module_exec_tests),
        "fixture_to_tests": dict(fixture_to_tests),
        "dialect_to_tests": dict(dialect_to_tests),
        "expr_to_tests": dict(expr_to_tests),
        "test_file_to_tests": dict(test_file_to_tests),
        "file_to_module": static["file_to_module"],
        "module_to_file": static["module_to_file"],
        "module_importers": static["module_importers"],
        "test_file_deps": static["test_file_deps"],
        "ast_index": clean_ast_index,
        "class_subclasses": static["class_subclasses"],
        "expr_class_file": static["expr_class_file"],
        "test_file_expr_refs": static["test_file_expr_refs"],
        "base_sources": static["base_sources"],
        "expr_profiles": {k: dict(v) for k, v in expr_profiles.items()},
    }

    raw_pkl = pickle.dumps(final_map, protocol=4)
    comp = zlib.compress(raw_pkl, level=9)
    output_path.write_bytes(comp)
    os.chmod(output_path, 0o644)

    total_time = time.perf_counter() - t0
    covered_lines = sum(len(v) for v in line_to_tests.values())
    covered_funcs = sum(len(v) for v in func_to_tests.values())
    profiled_sites = sum(len(v) for v in expr_profiles.values())
    print(f"[build_map] Done in {total_time:.1f}s!", file=sys.stderr)
    print(f"  Tests indexed:      {len(test_ids)}", file=sys.stderr)
    print(f"  Files with lines:   {len(line_to_tests)} ({covered_lines} covered lines)", file=sys.stderr)
    print(f"  Files with funcs:   {len(func_to_tests)} ({covered_funcs} covered funcs)", file=sys.stderr)
    print(f"  Fixtures mapped:    {len(fixture_to_tests)}", file=sys.stderr)
    print(f"  Dialects mapped:    {len(dialect_to_tests)}", file=sys.stderr)
    print(f"  Exprs mapped:       {len(expr_to_tests)}", file=sys.stderr)
    print(
        f"  Expr profile sites: {profiled_sites} across {len(expr_profiles)} files "
        f"(excluded {skipped_inactive} import-time sites)",
        file=sys.stderr,
    )
    print(
        f"  Map size:           {len(comp)/1e6:.2f} MB compressed "
        f"({len(raw_pkl)/1e6:.2f} MB raw pickle4)",
        file=sys.stderr,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Blast-Radius map from pinned sqlglot commit.")
    parser.add_argument("--sqlglot-dir", type=Path, default=Path("/sqlglot"))
    parser.add_argument("--output", type=Path, default=Path("/out/map.pkl.gz"))
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--verify-tests-json", type=Path, default=None)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--shard-out", type=Path, default=None)
    parser.add_argument("--shard-files", nargs="*", default=[])
    args = parser.parse_args()

    if args.worker:
        assert args.shard_out is not None
        run_worker(args.sqlglot_dir, args.shard_files, args.shard_out)
    else:
        build_map(args.sqlglot_dir, args.output, args.workers, args.verify_tests_json)


if __name__ == "__main__":
    main()
