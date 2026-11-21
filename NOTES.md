# Development Notes — Blast-Radius Test Selector for sqlglot

## Phase 1: Setup & Baseline Summary

### Environment
- Host: Windows with Python 3.13.14, Git 2.54.0, Docker Desktop 29.9.3 (`linux/amd64` engine via WSL2).
- Runtime image: `python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90` (pinned digest from starter `Dockerfile` — matches exact Python and SQLite used during test labeling).
- Map generation and `.pyc` precompilation will run inside containers based on this exact image digest, and map files use version-independent serialization (`pickle` protocol 4 or JSON, never `marshal`).
- `sample/` and `output.jsonl` are excluded via `.gitignore` and `.dockerignore` so no sample patches, IDs, or answers ever enter git or the image.

### Starter Kit Summary
- **`SCORING.md` & `score.py`**:
  - Inputs: `/data/tests.json` (`{"tests": [{"number", "id", "file", "seconds"}], "skipped": [...]}`), `/data/commits/<id>.patch`.
  - Output: one JSON line per commit on stdout: `{"commit": "<id>", "workers": [[...], ...]}`. Logs to stderr only, UTF-8 without BOM, max 2 MB stdout, no duplicate tests, valid test numbers only, max 8 non-empty workers.
  - Arithmetic in integer milliseconds: `duration_ms = floor(seconds * 1000 + 0.5)`.
  - Per-commit cost: `0` if empty workers; else `max(worker_load_ms) + 2000 * num_nonempty_workers`.
  - Miss penalty: `+300,000 ms` (300 s) per commit if ANY failing test is omitted.
  - Total = `simulated + CPU time (user + sys microseconds)`. Score = median of 3 runs, rounded to 0.1 s.
- **`Makefile`**:
  - Builds with `--platform linux/amd64`.
  - Runs under strict scorer limits: `--network none --cpus 2 --memory 4096m --memory-swap 4096m --pids-limit 256 --read-only --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL --security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp`.
- **Starter `selector.py`**:
  - Reads `/data/tests.json`, runs LPT across 3 fixed workers over all 1,620 tests, emits the same 3-worker schedule for every patch.
- **`requirements.lock`**:
  - Exact Python packages used when labeling commits (including `pytest==9.2.0`, `duckdb==2.0.0`, `pandas==3.1.1`, etc.). Note from `README.md`: `SKIP_INTEGRATION=0` must be set when running sqlglot tests so executor tests run, and sqlglot checkout is placed on `PYTHONPATH` with `requirements.lock` installed.

### Dataset Characteristics (`tests.json`)
- **1,620 tests** across **58 test files** (plus 1 skipped test: `tests/dialects/test_sqlite.py::TestSQLite::test_variadic_iif_execution`).
- Total suite test runtime: **103.047 s**.
- Extremely skewed runtime distribution:
  - `#1490 test_max_nodes`: 28.817 s
  - `#1106 test_execute_tpcds`: 21.454 s
  - `#1224 test_generator_memory`: 6.686 s
  - `#1107 test_execute_tpch`: 3.863 s
  - `#1123 test_optimized_tpch`: 3.537 s
  - Top 5 tests = 64.36 s (62.5% of total); remaining 1,615 tests average only 24 ms each!
- Implication: pruning expensive low-probability tests (especially `test_max_nodes`, `test_execute_tpcds`, `test_generator_memory`, `test_execute_tpch`, `test_optimized_tpch`) when untouched gives huge savings, while cheap related tests (~1–50 ms) should be included generously because a miss costs 300 s.
- Also note: running even 1 test costs at least 2.0 s (worker overhead), whereas `"workers": []` costs 0.0 s.

### Phase 1 Baseline Measurements (30 Sample Commits)
| Metric | Value |
|---|---|
| Simulated time | **1210.470 s** (40.349 s/commit; scales to ~1614.0 s on 40 practice commits) |
| Misses | **0** |
| Container CPU time (`user + sys`) | **0.096 s** (`0.084s user + 0.012s sys`) |
| Oracle lower bound (30 samples) | **94.351 s** |
| Practice target | **< 142.9 s** (~107 s equivalent on 30-commit scale) |

---

## Phase 2: Map Build & Verification (`build_map.py` -> `map.pkl.gz`)

### Design Decisions
- Built purely from the pinned sqlglot commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa` inside a container based on the exact pinned `python:3.13-slim@sha256:e48f59...` image with `requirements.lock` and `SKIP_INTEGRATION=0`.
- Does **not** embed `tests.json` numbers: indexes tests by sorted pytest node ID (`test_ids`), mapped dynamically to `/data/tests.json` numbers at runtime.
- Uses Python 3.13 `sys.monitoring` (`LINE` + `PY_START` with per-event `DISABLE` + `restart_events()` per test) across 4 balanced parallel workers.
- Key engineering fixes discovered during verification:
  1. **Synchronous `ProcessPoolExecutor`**: Both `tests/test_optimizer.py` and `tests/test_executor.py` run core work in spawned child processes; replacing `ProcessPoolExecutor` with an in-process `_SyncExecutor` during tracing captures full optimizer & executor coverage.
  2. **`Expr.__init__` (not `Expression.__init__`)**: Hooking `Expr.__init__` avoids shadowing `TimeUnit.__init__` in `_TimeUnit`'s MRO; all 1,620 tests + 25,440 subtests pass with 0 failures during tracing.
  3. **`setUpClass` hookwrapper**: Intercepting pytest's `_unittest_setUpClass_fixture_*` attributes class-level fixture opens (e.g., TPC-H / TPC-DS CSVs in `TestExecutor.setUpClass`) and setup lines to all tests in that class, not just the first test.
  4. **Dynamic `compile()` filter**: Ignoring non-file `co_filename` strings from `sqlglot/executor/python.py` keeps `line_to_tests` clean (237 real `.py` files).
- Stored in version-independent `pickle` protocol 4 + `zlib` level 9 (`map.pkl.gz`).

### Verification Results
- **Build time**: 53.8 s (4 parallel workers inside Docker)
- **Active tests indexed**: 1,620 (1 skipped) — exact 1-to-1 match with `tests.json` (`missing=0, extra=0`)
- **Test pass rate during tracing**: 1,620 passed, 0 failed, 25,440 subtests passed
- **Coverage stats**: 237 files (85,152 covered lines), 195 files (5,064 covered functions), 76 mapped fixtures, 122 dialect/parser/generator/typing files, 1,053 `Expr` classes
- **Map file size**: **2.24 MB** compressed (14.54 MB raw pickle4)
- **Map load time**: **48.6 ms** (18.6 ms decompress + 30.0 ms unpickle)

---

## Phase 3: Patch Parser, In-Memory Applier & File Classifier (`patch_parser.py`)

### Capabilities Implemented
- Unified diff parser handling `diff --git` and plain `---`/`+++`, renames, new/deleted files, binary patches, quoted paths, `\ No newline at end of file`, multi-hunk files, empty patches, and malformed diffs (never crashes; flags `parse_error=True`).
- Exact old-file changed line tracking (separating modified/deleted lines and pure-insertion anchors from unchanged 3-line diff context).
- In-memory hunk application against compressed `base_sources` with CRLF/LF preservation.
- Principled file classifier: `fixture`, `config`, `rust_or_c`, `test`, `test_helper`, `root_init`, `pkg_init`, `source`, `docs`, `other`.
- Python AST semantic diff analyzer:
  - Verifies PEP 263 encoding (`tokenize.detect_encoding`) and `ast.parse` on patched bytes (flags `syntax_error=True` on any parse/encoding failure).
  - Detects exact AST equivalence (`ast_identical=True`) — safely distinguishes genuine comments/formatting from comments inside multiline strings, docstring edits, indentation changes, coding cookies, or line-continuation syntax errors.
  - Extracts changed functions/methods, class headers (bases/decorators), class attributes (with exact changed dict keys when a class dict literal is modified), module-level variables (with dict keys), imports, `__all__`, and added/removed public names.
- Verified on all 30 sample patches: 30/30 parsed and applied cleanly with zero errors.

---

## Phase 4: Selector (`selector.py`) & Map Refinements

### Map Refinements Discovered During Phase 4 Testing
1. **IPC pickling inside `_SyncExecutor`**: Initially `sqlglot/serde.py` missed `ProcessPoolExecutor` tests (`test_optimizer.py` / `test_executor.py`) because `_SyncExecutor` bypassed pickling (which calls `Expression.__reduce__` -> `serde.dump`/`load`). Adding `pickle.loads(pickle.dumps(...))` round-tripping in `_SyncExecutor.submit` restored exact `ProcessPoolExecutor` coverage: `3utz` (`serde.py`) went from a miss to 37/37 exact precision & recall.
2. **Runtime MRO `class_subclasses`**: Replaced short-name AST class matching (which merged unrelated nested `Tokenizer` classes across dialects) with exact runtime `cls.__mro__` walking across all `sqlglot.*` modules.

### Selection Architecture (`selector.py`)
- Pure Python stdlib (`python -S -E` compatible), loads `map.pkl.gz` once.
- Tiered probability model (`DEFAULT_CONFIG`) combining:
  - Direct changed lines (`line_to_tests`) & enclosing multi-line statements (`p_direct`, `p_enclosing_stmt`).
  - Enclosing functions (`func_to_tests`, promoted when changed line has zero direct coverage).
  - Class attributes & declarative tables: direct line hits (for lambdas), `Expr` hierarchy closure (`expr_to_tests`), dialect/MRO intersection with attribute readers (`dialect_to_tests & name_ref_tests`) plus cost-bounded dialect fallback.
  - Direct fixture reads (`fixture_to_tests`) with directory/all-fixture fallback for unmapped fixtures.
  - Surgical test-file & test-helper selection when only specific test methods/helpers change; full file + importers otherwise.
  - Conservative widening on syntax/encoding errors, `__all__`, public name add/remove, imports, class headers, config, Rust/C, root `__init__.py`, malformed/unknown files, and any internal exception.
  - Cost-aware inclusion (FR4): include test `t` when `P(fail) * 300 > runtime / k_div`.

### Phase 4 Sample Evaluation (Default Un-Tuned Thresholds)
| Metric | Value |
|---|---|
| Simulated time (30 samples) | **188.958 s** (down from 1210.470 s baseline, **6.4x reduction**) |
| Misses | **0 / 30** |
| Local wall time | **0.643 s** |

---

## Phase 5: Optimal Worker Packer (`packer.py`)

- Exact integer millisecond objective: `max(worker_load_ms) + 2000 * k` for `k` in `1..8`.
- Theoretical lower-bound pruning skips impossible `k` values (`max(max_single, ceil(total/k)) + 2000*k >= best_cost`).
- LPT initialization + bounded move and 1-1 swap local search between busiest and lighter workers.
- Verified:
  - Classic LPT-suboptimal `[5000, 4000, 3000, 3000, 3000]` -> optimal `[9000, 9000]` (k=2).
  - Full 1,620-test suite -> optimal `k=4`, `max_load=28.817s` (equals single longest test), cost `36.817s` (vs starter's `40.349s`), packed in **1.95 ms**.




