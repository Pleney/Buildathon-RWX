#!/usr/bin/env python3
"""Single-process plain-pytest-order build-time profile builder with 2x stability diff.

Builds `profiles.pkl.gz` strictly from pinned sqlglot commit eb11aec4be8cb2122afe713a4816662c3f0fb2fa.
- Runs the FULL sqlglot test suite in a SINGLE process in exact default pytest
  collection order (matching a plain `pytest` run so isolation or ordering effects
  cannot hide any path from a test).
- Runs TWICE in independent fresh processes (`--single-run`), then diffs Run 1 vs
  Run 2 (`--merge-stable`): any site whose recorded profile differs by even 1 bit
  between runs is discarded (treated as unprofiled).
- Excludes all import/collection-time sites and marks subprocess tests
  (`test_lazy_load`) as unprofiled.
- Never modifies `map.pkl.gz`.
"""

from __future__ import annotations

import argparse
import builtins
import concurrent.futures
import os
import pickle
import subprocess
import sys
import tempfile
import time
import zlib
from collections import defaultdict
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import build_map as bm  # noqa: E402
import expr_profiler as ep  # noqa: E402


class _ProfilePlugin:
    """Pytest plugin recording per-test expression & reachability profiles in plain pytest order."""

    def __init__(self, repo_root: Path, recorder: ep.BuildTimeProfileRecorder) -> None:
        self.repo_root = repo_root.resolve()
        self.recorder = recorder
        self.active_key: Any = None
        self.test_nodeids: list[str] = []
        self.test_classes: dict[str, str] = {}
        self.skipped_nodeids: set[str] = set()
        self.failed_nodeids: list[str] = []
        self.import_time_code_lines: set[tuple[str, int]] = set()
        self._tool_id = 3
        self._mon_active = False

    def _on_import_py_start(self, code: Any, offset: int) -> Any:
        if self.active_key is None:
            fn = code.co_filename
            if fn and fn.endswith(".py") and os.path.isabs(fn):
                try:
                    rel = Path(fn).resolve().relative_to(self.repo_root).as_posix()
                    if rel.startswith("sqlglot/"):
                        self.import_time_code_lines.add((rel, code.co_firstlineno))
                except Exception:
                    pass
        return sys.monitoring.DISABLE

    def pytest_configure(self, config: Any) -> None:
        concurrent.futures.ProcessPoolExecutor = bm._SyncExecutor  # type: ignore[misc]
        builtins.__blast_bp__ = self.recorder.branch_probe  # type: ignore[attr-defined]
        builtins.__blast_cp__ = self.recorder.compare_probe  # type: ignore[attr-defined]
        builtins.__blast_rm__ = self.recorder.reach_mark  # type: ignore[attr-defined]
        sys.meta_path.insert(0, ep.ProfilingMetaPathFinder(self.repo_root, self.recorder, enable_reach=True))
        # Capture functions executed during module import / collection (turned off once tests start!)
        try:
            sys.monitoring.use_tool_id(self._tool_id, "blast_prof_import")
            sys.monitoring.register_callback(
                self._tool_id, sys.monitoring.events.PY_START, self._on_import_py_start
            )
            sys.monitoring.set_events(self._tool_id, sys.monitoring.events.PY_START)
            self._mon_active = True
        except Exception:
            self._mon_active = False

    def pytest_collection_modifyitems(self, session: Any, config: Any, items: list[Any]) -> None:
        if self._mon_active:
            sys.monitoring.set_events(self._tool_id, sys.monitoring.events.NO_EVENTS)
            try:
                sys.monitoring.free_tool_id(self._tool_id)
            except Exception:
                pass
            self._mon_active = False
        for item in items:
            nid = item.nodeid
            self.test_nodeids.append(nid)
            cls = getattr(item, "cls", None)
            if cls is not None:
                self.test_classes[nid] = f"{cls.__module__}.{cls.__qualname__}"

    def pytest_fixture_setup(self, fixturedef: Any, request: Any) -> Any:
        if fixturedef.argname.startswith("_unittest_setUpClass_fixture_") and request.cls is not None:
            cls = request.cls
            ckey = f"{cls.__module__}.{cls.__qualname__}"
            prev_key = self.active_key
            self.active_key = ("class", ckey)
            self.recorder.switch_target(self.active_key)
            try:
                yield
            finally:
                self.active_key = prev_key
                self.recorder.switch_target(prev_key)
        else:
            yield

    def pytest_runtest_setup(self, item: Any) -> None:
        self.active_key = item.nodeid
        self.recorder.switch_target(item.nodeid)

    def pytest_runtest_call(self, item: Any) -> None:
        if self.active_key != item.nodeid:
            self.active_key = item.nodeid
            self.recorder.switch_target(item.nodeid)

    def pytest_runtest_teardown(self, item: Any, nextitem: Any) -> None:
        self.recorder.stop()
        self.active_key = None

    def pytest_runtest_makereport(self, item: Any, call: Any) -> None:
        if call.excinfo is not None:
            if call.excinfo.typename == "Skipped":
                self.skipped_nodeids.add(item.nodeid)
            elif call.when in ("setup", "call"):
                self.failed_nodeids.append(f"{item.nodeid} ({call.when}: {call.excinfo.typename})")

    def pytest_sessionfinish(self, session: Any, exitstatus: int) -> None:
        self.recorder.stop()
        self.active_key = None


def run_single_pass(sqlglot_dir: Path, map_path: Path, run_out: Path) -> None:
    import pytest

    t0 = time.monotonic()
    map_data = pickle.loads(zlib.decompress(map_path.read_bytes()))
    test_ids: list[str] = map_data["test_ids"]
    id_to_idx = {nid: i for i, nid in enumerate(test_ids)}

    os.environ["SKIP_INTEGRATION"] = "0"
    sys.path.insert(0, str(sqlglot_dir.resolve()))
    os.chdir(sqlglot_dir)

    recorder = ep.BuildTimeProfileRecorder()
    plugin = _ProfilePlugin(sqlglot_dir, recorder)
    _ProfilePlugin.pytest_fixture_setup = pytest.hookimpl(hookwrapper=True)(
        _ProfilePlugin.pytest_fixture_setup
    )

    # Exact plain pytest run order (default collection of `tests`)
    args = ["-q", "-p", "no:cacheprovider", "--no-header", "--tb=short", "-o", "addopts=", "tests"]
    rc = pytest.main(args, plugins=[plugin])
    elapsed = time.monotonic() - t0
    print(
        f"[build_profiles] single pass done in {elapsed:.1f}s: rc={rc} "
        f"collected={len(plugin.test_nodeids)} skipped={len(plugin.skipped_nodeids)} "
        f"failed={len(plugin.failed_nodeids)}",
        file=sys.stderr,
        flush=True,
    )
    if plugin.failed_nodeids:
        print(f"[build_profiles] FAILURES: {plugin.failed_nodeids[:10]}", file=sys.stderr, flush=True)
    assert rc == 0 and not plugin.failed_nodeids, f"Profile run failed (rc={rc}, fails={plugin.failed_nodeids[:5]})"

    # Merge ("class", cls_key) traces into every test belonging to that class
    for nid, cls_key in plugin.test_classes.items():
        ck = ("class", cls_key)
        if ck in recorder.records:
            dst = recorder.records.setdefault(nid, {})
            for sid, fl in recorder.records[ck].items():
                dst[sid] = dst.get(sid, 0) | fl
        if ck in recorder.reach_records:
            rdst = recorder.reach_records.setdefault(nid, set())
            rdst.update(recorder.reach_records[ck])

    # Pack into bitsets keyed by full 4-tuple span (rel, ln, col, eln, ecol)
    inactive_expr = {
        (recorder.sites[s][0], recorder.sites[s][1], recorder.sites[s][2], recorder.sites[s][3], recorder.sites[s][4])
        for s in recorder.inactive_sids
    }
    inactive_reach = {recorder.reach_sites[s] for s in recorder.inactive_rsids}

    # Resolve all functions executed at import / collection time
    ast_index = map_data["ast_index"]
    import_time_funcs: set[tuple[str, str]] = set()
    for rel, ln in plugin.import_time_code_lines:
        info = ast_index.get(rel)
        if info:
            for s, e, qn in info["funcs"]:
                if s <= ln <= e:
                    import_time_funcs.add((rel, qn))
    for rel, ln, _c, _el, _ec in inactive_expr | inactive_reach:
        info = ast_index.get(rel)
        if info:
            for s, e, qn in info["funcs"]:
                if s <= ln <= e:
                    import_time_funcs.add((rel, qn))

    prof_acc: dict[tuple[str, int, int, int, int], list[Any]] = {}
    for nid, sid_flags in recorder.records.items():
        if not isinstance(nid, str):
            continue
        idx = id_to_idx.get(nid)
        if idx is None:
            continue
        bit = 1 << idx
        for sid, fl in sid_flags.items():
            rel, ln, col, eln, ecol, is_cmp = recorder.sites[sid]
            key = (rel, ln, col, eln, ecol)
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

    expr_profiles: dict[tuple[str, int, int, int, int], tuple[int, ...]] = {}
    for key, acc in prof_acc.items():
        if key in inactive_expr:
            continue
        if acc[0]:
            expr_profiles[key] = (acc[1], acc[2], acc[3], acc[4], acc[5], acc[6], acc[7])
        else:
            expr_profiles[key] = (acc[1], acc[2], acc[3])

    reach_profiles: dict[tuple[str, int, int, int, int], int] = {}
    for nid, rset in recorder.reach_records.items():
        if not isinstance(nid, str):
            continue
        idx = id_to_idx.get(nid)
        if idx is None:
            continue
        bit = 1 << idx
        for rsid in rset:
            key = recorder.reach_sites[rsid]
            if key in inactive_reach:
                continue
            reach_profiles[key] = reach_profiles.get(key, 0) | bit

    # Also register instrumented sites with 0 active hits (dead / unreached for ALL tests!)
    for rel, ln, col, eln, ecol, is_cmp in recorder.sites:
        key = (rel, ln, col, eln, ecol)
        if key not in inactive_expr and key not in expr_profiles:
            expr_profiles[key] = (0, 0, 0, 0, 0, 0, 0) if is_cmp else (0, 0, 0)
    for key in recorder.reach_sites:
        if key not in inactive_reach and key not in reach_profiles:
            reach_profiles[key] = 0

    active_collected = [n for n in plugin.test_nodeids if n not in plugin.skipped_nodeids]
    missing_from_run = sorted(set(test_ids) - set(active_collected))

    out = {
        "expr_profiles": expr_profiles,
        "reach_profiles": reach_profiles,
        "inactive_expr": inactive_expr,
        "inactive_reach": inactive_reach,
        "import_time_funcs": import_time_funcs,
        "missing_from_run": missing_from_run,
        "elapsed": elapsed,
    }
    run_out.write_bytes(pickle.dumps(out, protocol=4))
    print(
        f"[build_profiles] wrote {run_out}: expr_sites={len(expr_profiles)} "
        f"(inactive={len(inactive_expr)}), reach_sites={len(reach_profiles)} "
        f"(inactive={len(inactive_reach)}), missing_tests={len(missing_from_run)}",
        file=sys.stderr,
        flush=True,
    )


def merge_and_check_stability(map_path: Path, run1_path: Path, run2_path: Path, out_path: Path) -> None:
    map_data = pickle.loads(zlib.decompress(map_path.read_bytes()))
    r1 = pickle.loads(run1_path.read_bytes())
    r2 = pickle.loads(run2_path.read_bytes())

    inact_expr = r1["inactive_expr"] | r2["inactive_expr"]
    inact_reach = r1["inactive_reach"] | r2["inactive_reach"]

    e1, e2 = r1["expr_profiles"], r2["expr_profiles"]
    stable_expr: dict[str, dict[tuple[int, int, int, int], tuple[int, ...]]] = defaultdict(dict)
    unstable_expr: list[tuple[str, int, int, int, int]] = []
    for key in sorted(set(e1.keys()) | set(e2.keys())):
        if key in inact_expr:
            continue
        if key in e1 and key in e2 and e1[key] == e2[key]:
            rel, ln, col, eln, ecol = key
            stable_expr[rel][(ln, col, eln, ecol)] = e1[key]
        else:
            unstable_expr.append(key)

    rc1, rc2 = r1["reach_profiles"], r2["reach_profiles"]
    stable_reach: dict[str, dict[tuple[int, int, int, int], int]] = defaultdict(dict)
    unstable_reach: list[tuple[str, int, int, int, int]] = []
    for key in sorted(set(rc1.keys()) | set(rc2.keys())):
        if key in inact_reach:
            continue
        if key in rc1 and key in rc2 and rc1[key] == rc2[key]:
            rel, ln, col, eln, ecol = key
            stable_reach[rel][(ln, col, eln, ecol)] = rc1[key]
        else:
            unstable_reach.append(key)

    import_funcs_by_file: dict[str, list[str]] = defaultdict(list)
    for rel, qn in sorted(r1.get("import_time_funcs", set()) | r2.get("import_time_funcs", set())):
        import_funcs_by_file[rel].append(qn)

    unprofiled = sorted(
        set(r1["missing_from_run"])
        | set(r2["missing_from_run"])
        | {"tests/dialects/test_dialect.py::TestDialect::test_lazy_load"}
    )

    payload = {
        "version": 3,
        "commit": map_data["commit"],
        "test_ids": map_data["test_ids"],
        "expr_profiles": {k: dict(v) for k, v in stable_expr.items()},
        "reach_profiles": {k: dict(v) for k, v in stable_reach.items()},
        "import_time_funcs": dict(import_funcs_by_file),
        "unprofiled_test_ids": unprofiled,
    }
    raw = pickle.dumps(payload, protocol=4)
    comp = zlib.compress(raw, level=9)
    out_path.write_bytes(comp)
    os.chmod(out_path, 0o644)

    n_expr = sum(len(v) for v in stable_expr.values())
    n_reach = sum(len(v) for v in stable_reach.values())
    print(
        f"[build_profiles] STABILITY DIFF COMPLETE:\n"
        f"  Run 1: {r1['elapsed']:.1f}s | Run 2: {r2['elapsed']:.1f}s\n"
        f"  Expr sites:  {n_expr} stable, {len(unstable_expr)} unstable (dropped), {len(inact_expr)} import-time (excluded)\n"
        f"  Reach sites: {n_reach} stable, {len(unstable_reach)} unstable (dropped), {len(inact_reach)} import-time (excluded)\n"
        f"  Unprofiled tests: {unprofiled}\n"
        f"  profiles.pkl.gz: {len(comp)/1e6:.2f} MB compressed ({len(raw)/1e6:.2f} MB raw)",
        file=sys.stderr,
        flush=True,
    )
    if unstable_expr:
        print(f"  Sample unstable expr sites: {unstable_expr[:10]}", file=sys.stderr)
    if unstable_reach:
        print(f"  Sample unstable reach sites: {unstable_reach[:10]}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build stability-verified profiles.pkl.gz in plain pytest order.")
    parser.add_argument("--sqlglot-dir", type=Path, default=Path("/sqlglot"))
    parser.add_argument("--map", type=Path, default=Path("/work/map.pkl.gz"))
    parser.add_argument("--output", type=Path, default=Path("/out/profiles.pkl.gz"))
    parser.add_argument("--single-run", type=Path, default=None)
    parser.add_argument("--merge-runs", nargs=2, type=Path, default=None)
    args = parser.parse_args()

    if args.single_run is not None:
        run_single_pass(args.sqlglot_dir, args.map, args.single_run)
    elif args.merge_runs is not None:
        merge_and_check_stability(args.map, args.merge_runs[0], args.merge_runs[1], args.output)
    else:
        with tempfile.TemporaryDirectory(prefix="prof_stab_") as td:
            r1 = Path(td) / "run1.pkl"
            r2 = Path(td) / "run2.pkl"
            for label, rpath in [("Run 1/2", r1), ("Run 2/2", r2)]:
                print(f"[build_profiles] Starting {label} (plain single-process pytest order)...", file=sys.stderr, flush=True)
                p = subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--sqlglot-dir", str(args.sqlglot_dir),
                        "--map", str(args.map),
                        "--single-run", str(rpath),
                    ],
                    timeout=1500,
                )
                assert p.returncode == 0, f"{label} failed with rc={p.returncode}"
            merge_and_check_stability(args.map, r1, r2, args.output)


if __name__ == "__main__":
    main()
