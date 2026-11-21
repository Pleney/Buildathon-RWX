# Development Notes — Blast-Radius Test Selector for sqlglot

## Phase 1: Setup & Baseline Summary

### Environment
- Host: Windows with Python 3.13.14, Git 2.54.0, Docker Desktop 29.9.3 (`linux/amd64` engine via WSL2).
- Runtime image: `python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90` (pinned digest from starter `Dockerfile` — matches exact Python and SQLite used during test labeling).
- Map generation and `.pyc` precompilation run inside containers based on this exact image digest; map files use version-independent serialization (`pickle` protocol 4 + `zlib`, never `marshal`).
- `sample/` and `output.jsonl` are excluded via `.gitignore` and `.dockerignore` so no sample patches, IDs, or answers ever enter git or the runtime image.

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
  - Exact Python packages used when labeling commits (`pytest==9.2.0`, `duckdb==2.0.0`, `pandas==3.1.1`, etc.). `SKIP_INTEGRATION=0` must be set when running sqlglot tests so executor tests run.

### Dataset Characteristics (`tests.json`)
- **1,620 tests** across **58 test files** (plus 1 skipped test: `tests/dialects/test_sqlite.py::TestSQLite::test_variadic_iif_execution`).
- Total suite test runtime: **103.047 s**.
- Extremely skewed runtime distribution:
  - `#1490 test_max_nodes`: 28.817 s
  - `#1106 test_execute_tpcds`: 21.454 s
  - `#1224 test_generator_memory`: 6.686 s
  - `#1107 test_execute_tpch`: 3.863 s
  - `#1123 test_optimized_tpch`: 3.537 s
  - Top 5 tests = 64.36 s (62.5% of total); remaining 1,615 tests average only 24 ms each.

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
  1. **Synchronous `ProcessPoolExecutor` with IPC pickling**: Both `tests/test_optimizer.py` and `tests/test_executor.py` run core work in spawned child processes; replacing `ProcessPoolExecutor` with an in-process `_SyncExecutor` that round-trips inputs & outputs through `pickle.loads(pickle.dumps(...))` captures both in-process coverage AND `Expression.__reduce__` / `sqlglot.serde` serialization paths.
  2. **`Expr.__init__` (not `Expression.__init__`)**: Hooking `Expr.__init__` avoids shadowing `TimeUnit.__init__` in `_TimeUnit`'s MRO; all 1,620 tests + 25,440 subtests pass with 0 failures during tracing.
  3. **`setUpClass` hookwrapper**: Intercepting pytest's `_unittest_setUpClass_fixture_*` attributes class-level fixture opens (e.g., TPC-H / TPC-DS CSVs in `TestExecutor.setUpClass`) and setup lines to all tests in that class, not just the first test.
  4. **Dynamic `compile()` filter**: Ignoring non-file `co_filename` strings from `sqlglot/executor/python.py` keeps `line_to_tests` clean (237 real `.py` files).
  5. **Exact runtime MRO `class_subclasses`**: Walking actual Python `cls.__mro__` across all imported `sqlglot.*` modules prevents unrelated nested `Tokenizer` classes from colliding by short name.
- Stored in version-independent `pickle` protocol 4 + `zlib` level 9 (`map.pkl.gz`).

### Verification Results
- **Build time**: 57.4 s (4 parallel workers inside Docker)
- **Active tests indexed**: 1,620 (1 skipped) — exact 1-to-1 match with `tests.json` (`missing=0, extra=0`)
- **Test pass rate during tracing**: 1,620 passed, 0 failed, 25,440 subtests passed
- **Coverage stats**: 237 files (85,152 covered lines), 195 files (5,064 covered functions), 76 mapped fixtures, 122 dialect/parser/generator/typing files, 1,053 `Expr` classes
- **Map file size**: **2.24 MB** compressed (14.53 MB raw pickle4)
- **Map load time**: **48.6 ms** (18.6 ms decompress + 30.0 ms unpickle)

---

## Phase 3: Patch Parser, In-Memory Applier & File Classifier (`patch_parser.py`)

- Unified diff parser handling `diff --git` and plain `---`/`+++`, renames, new/deleted files, binary patches, quoted paths, `\ No newline at end of file`, multi-hunk files, empty patches, and malformed diffs (never crashes; flags `parse_error=True`).
- Exact old-file changed line tracking (separating modified/deleted lines and pure-insertion anchors from unchanged 3-line diff context).
- In-memory hunk application against compressed `base_sources` with CRLF/LF preservation.
- Principled file classifier: `fixture`, `config`, `rust_or_c`, `test`, `test_helper`, `root_init`, `pkg_init`, `source`, `docs`, `other`.
- Python AST semantic diff analyzer verifying PEP 263 encoding (`tokenize.detect_encoding`), `ast.parse`, exact AST equivalence (`ast_identical`), and structural diffs for functions, classes, class headers, class attributes (with exact changed dict keys), module variables, imports, `__all__`, and public names.
- Verified on all 30 sample patches: 30/30 parsed and applied cleanly.

---

## Phase 4 & 5: Selector (`selector.py`) & Worker Packer (`packer.py`)

- `selector.py`: Pure Python stdlib (`python -S -E` compatible), loads `map.pkl.gz` once, implements tiered probability rules + FR4 cost-aware inclusion (`P(fail) * 300 > runtime / k_div`) + strict output validation + conservative fallbacks on any error or unclassified file.
- `packer.py`: Exact integer-millisecond worker packer choosing `k` in `1..8` minimizing `max(worker_load_ms) + 2000 * k` using lower-bound pruning, LPT initialization, and move + 1-1 swap local search.
  - Verified on classic LPT-suboptimal `[5000, 4000, 3000, 3000, 3000]` -> optimal `[9000, 9000]` (k=2).
  - Full 1,620-test suite -> optimal `k=4`, `max_load=28.817s` (equals single longest test), cost `36.817s` (vs starter's `40.349s`), packed in **1.95 ms**.

---

## Phase 6: Evaluation, Principled Refinements & Cross-Validation (`eval.py`)

### Principled Program-Analysis Refinements
1. **Runtime readers vs class-body definitions**: `_tests_referencing_name` queries `func_to_tests` only (excluding class-body definitions) and skips plumbing constructors (`__init__`, `__new__`, `_init_core`) when non-constructor reader methods exist (also checking `attr.lower()` for `TokenizerCore` mirrors).
2. **Lambda-only direct line hits for class attributes**: Class attribute assignments only use `line_to_tests` when the changed line is inside an `ast.Lambda`, preventing accidental first-import hits from bypassing dialect-attribute reader intersection.
3. **Compile-time function scope + statement reachability**: When a function's signature, decorators, docstring, local variable bindings, and generator/yield flag are unchanged, tests that never reached the modified line or its enclosing statement cannot be affected.
4. **Method-level `Expr` static references**: Narrowed static `Expr` references in `tests/` from whole-file to the referencing test methods.

### Sample Evaluation & 3-Fold (20/10) Cross-Validation
| Metric | Value |
|---|---|
| Simulated time (30 samples) | **165.365 s** (7.3x reduction vs 1210.470 s baseline) |
| Oracle lower bound (30 samples) | **94.351 s** |
| Excess across all 18 failing commits | **+0.501 s total** (20/30 commits at exact `+0.000 s` oracle) |
| Excess on 8 covered 0-fail commits | **+70.513 s** (dominated by two covered `is`->`==` edits hitting 21.45 s `test_execute_tpcds`, plus 6 smaller covered 0-fail edits) |
| Misses | **0 / 30** |
| 3-fold CV aggregate (20 train / 10 val) | **165.365 s**, **0 misses**, **0.000 s CV gap** (all 3 folds select identical config) |

---

## Phase 7: Robustness Suite (`tests/test_robustness.py`) & CPU Profiling

- Added 18 synthetic robustness tests in `tests/test_robustness.py` (standalone against `map.pkl.gz`, zero dependency on `sample/`) covering: docs-only README, pure comment + conservative comment mode, comment inside docstring, PEP 263 coding cookie trap, new/removed public functions, constant edit, decorator change, import added, syntax-breaking edit, new/deleted/renamed files, test-file edit, known & unknown fixtures, `pyproject.toml`, `conftest.py`, Rust/tokenizer, `sqlglot/__init__.py`, unknown file extensions, binary/empty/malformed patches, and packer invariants.
- **Bug caught by robustness suite**: editing a function docstring (which emits no bytecode `LINE` events) was initially filtered by the unreachable-statement check; fixed by including `ast.get_docstring(node, clean=False)` in `_func_scope_signature`.
- **CPU optimization**: reused `old_tree`/`new_tree` parsed in `analyze_python_change` instead of re-parsing files in `selector.py`.
- All 18 tests pass under `python -S -E -m unittest` in **0.420 s**.

---

## Phase 8: Final Docker Verification & Report

### Scorer-Limit Container Verification (`--read-only --user 65534:65534 --network none --cpus 2 --memory 4096m --tmpfs /tmp:size=64m`)
| Metric | Baseline (Starter) | Final (Pure-Static FR7) |
| --- | --- | --- |
| Simulated time (30 samples) | 1210.470 s | **165.365 s** (-86.3%) |
| Misses (30 samples) | 0 | **0** |
| Median container CPU (`user + sys`, 3 runs) | 0.096 s | **0.737 s** (runs: 0.785s, 0.737s, 0.733s) |
| Total (Simulated + CPU, 30 samples) | 1210.566 s | **166.102 s** |
| Stdout size | ~194 KB | **3,097 bytes** (limit: 2 MB) |
| Docker build time | ~2 s | **7.64 s** (cold context) |
| `map.pkl.gz` size | — | **2.24 MB** |
| Tracked files / Total repo (`.git` + tracked) | — | **2.41 MB tracked / 7.02 MB total** (limit: 20 MB) |
| Precompiled `.pyc` readable by `nobody` | No | **Yes** (`/app/__pycache__/*.cpython-313.pyc`, mode `644`) |

### Open Risks & Path to Beat 142.9 s on Practice
1. **Pure-static lower bound vs 142.9 s practice target**:
   - Across all 18 failing sample commits, our selector is within **+0.501 s** of the mathematical oracle (with 0 misses and 20/30 commits at exact `+0.000 s` oracle).
   - However, **+70.513 s** of excess comes strictly from 8 covered-but-non-failing code edits (including two `is` -> `==` edits directly executed by 21.45 s `test_execute_tpcds`, plus 6 covered 0-fail edits costing 2–9 s each).
   - Consequently, ANY pure-static selector obeying FR7 (no test execution at decision time) faces a hard floor of **~160 s** on 30 samples (**~213 s** scaled to 40 practice commits), above the **142.9 s** top practice score (~107 s on 30-commit scale).
   - **Recommended next step (requires explicit approval to relax FR7)**: enable hybrid selective in-container execution (explicitly allowed by `README.md`: "Running sqlglot's tests while you decide is allowed, and you pay for the time") to execute fast candidate tests (< 1.5 s total) and fast canary subsets in-container, emitting `"workers": []` when all candidates pass and down-weighting 21.5 s `test_execute_tpcds` when dozens of fast tests hitting the exact same line all pass. Projected score: **~100–105 s** on 30 samples (**~133–140 s** on 40 practice commits, beating 142.9 s).
2. **AST-identical pure Python comment edits**: Currently `p_ast_identical = 0.0` (verified via PEP 263 encoding check + `ast.parse` + exact `ast.dump` equality, after verifying no test in `eb11aec` inspects comments or `inspect.getsource`). If an unseen environment ever added a comment-sensitive test, toggling `p_ast_identical` to `0.001` in `DEFAULT_CONFIG` adds conservative cheap-test coverage (+~6 s on 30 samples).
3. **Unseen dynamic dispatch / `eval` / monkeypatch edits**: Handled conservatively by syntax/import/public-name/class-header/config/unknown-file fallbacks, though any runtime reflection that skips direct line coverage and static identifier references could remain a residual risk.

---

## Phase 9: Hybrid In-Container Verification + AST Shadow Expression Checking (`verifier.py`)

### Practice Board Feedback (Pure-Static v1)
- Pure-static v1 scored **206.7 s** on the 40-commit hidden practice set (`205.456 s` simulated + `1.19 s` median CPU, **0 commits missed** across all 3 runs).

### General Optimizations Added
1. **Dict-values-only AST dataflow filter (`patch_parser.py` + `selector.py`)**: When a class dict literal edit modifies only values while keeping the same non-empty key set, reader functions that only check truthiness (`bool(d)` / `if d:`) or key membership (`k in d`) cannot change behavior and are excluded; functions reading dict values (`.get(...)`, `[...]`, etc.) remain included.
2. **Forked in-container fast-test verifier (`verifier.py`)**:
   - Uses Linux `os.fork()` with an in-memory `sys.meta_path` loader and `open()` overlay (covering both `.py` and fixture files) before `sqlglot` is ever imported in the child — zero stale references, zero disk writes on read-only FS, child `fd 1` (`stdout`) redirected to `/dev/null` so test prints can never corrupt JSONL stdout.
   - Replaces `ProcessPoolExecutor` with `_SyncExecutor` (with IPC pickling when `serde.py` or `sqlglot/expressions/*` is patched), uses fail-fast `subTest` abort, lazy TPC-DS loading in `TestExecutor`, per-test `SIGALRM` timeout, and skips `test_lazy_load` (subprocess test).
   - Drops tests proven to pass in-container (0% miss risk); stops immediately on first failure so unrun candidates are kept; rescues single final remaining tests `<= 2.20 s` to save the 2.00 s worker startup cost.
3. **AST Shadow Expression Instrumentation (`verifier.py`)**:
   - For single pure-expression edits inside a function body, instruments the edited expression with `__blast_shadow__(lambda: old_expr, lambda: new_expr)` inside the forked child to count calls and behavioral divergences.
   - If even 1 divergence occurs, refuses to prune any unrun test. If `>= 12` passing tests and `>= 50` calls observe 0 divergences, applies FR4 including the 2.00 s worker startup cost.

### Hybrid Verification Results (30 Samples, Exact Scorer Docker Limits as `nobody`)
| Metric | Pure-Static v1 | Hybrid v2 | Oracle |
| --- | --- | --- | --- |
| Simulated time (30 samples) | 165.365 s | **94.581 s** (+0.230 s over oracle!) | 94.351 s |
| Misses (30 samples) | 0 | **0** | 0 |
| Median container CPU (3 runs) | 0.737 s | **12.513 s** | — |
| Total (`simulated + CPU`, 30 samples) | 166.102 s | **107.094 s** | 94.351 s |
| Projected 40-commit practice score | 206.7 s (actual) | **~125–133 s** (target < 142.9 s) | — |
| Robustness suite (local & in-Docker) | 18/18 passed | **18/18 passed** | — |

---

## Branch `hybrid-v3-econ`: Measurement, Economic Gate, Deadline Guard & Differential Audit

### Step 1: Baseline Economic Measurement (`econ_report.py`, no behaviour change)
Measured inside the exact scorer container (`--cpus 2 --read-only --user 65534:65534 --network none`) on commit `9a38b9b`:
- **Total static cost**: `162.412 s`
- **Total final simulated cost**: `94.581 s` (`0` misses)
- **Total verifier CPU**: `10.904 s` (Grand CPU: `11.973 s`; 3-run median baseline total: **`107.094 s`**)
- **Total saving**: `+67.831 s` | **Total net gain**: `+56.927 s`
- **Profitable commits (8)**: `+60.175 s` net gain
- **Money-losing verified commits (8)**: `-3.248 s` net (`3.519 s` CPU burned)
  - Main loss patterns identified (purely structural, zero sample-specific logic):
    1. Commits where unrunnable slow tests (`> max_single_test_sec`) remain and the runnable tests' marginal saving (`pack(S) - pack(S \ runnable)`) is smaller than their estimated CPU cost (fork + import + test execution).
    2. `TestOptimizer` fixture tests run single-threaded under `_SyncExecutor` (so actual CPU is ~2x their multi-core recorded `tests.json` duration), making marginal pruning unprofitable unless eliminating the 2.00 s worker startup cost or probing shadow equivalence.
    3. Commits where a `fixture` file is also modified alongside code (high probability of failure, low marginal saving).

### Step 2: Economic Gate & Saving-Per-CPU Ordering (`verifier.py`)
- Added `_economic_plan` before forking:
  - Computes exact packed cost (`max_load + 2k`) via `packer.pack_tests` and per-test estimated single-threaded CPU cost (`_est_test_cpu_sec`, accounting for `_SyncExecutor` single-threaded overhead on `test_optimizer.py` and one-time module/class setup amortization).
  - Evaluates best-case achievable saving (`max_possible_saving`) across all-runnable, shadow-canary, Laplace-canary, and partial-runnable regimes; skips forking unless `max_possible_saving > est_cpu * min_roi_ratio` (`1.0`).
  - Greedily orders runnable candidates by descending saving per CPU second (with module-load amortization), placing last-test-rescue candidates (`> max_single_test_sec`) at the tail and applying a per-candidate `direct_saving > est_cpu` gate when unrunnable tests will remain.
- Tuned `min_static_cost_to_verify` across `{0.0, 2.02, 2.10, 2.25}`: any threshold `> 2.0` worsens total by `+1.65 s` to `+5.20 s` because verifying a tiny test risks `~0.18 s` CPU to save `>= 2.00 s` worker startup (~11:1 reward-to-risk). Kept `min_static_cost_to_verify = 0.0` and let the saving-vs-CPU gate decide.
- **Scorer-limit Docker benchmark (`--cpus 2 --read-only --user 65534:65534`, 3 runs)**:
  - Simulated: **`94.581 s`** | Misses: **`0 / 30`**
  - Median container CPU: **`10.069 s`** (runs: `9.999 s`, `10.122 s`, `10.069 s`, down from `12.513 s`)
  - **Total (`simulated + CPU`)**: **`104.650 s`** (**`-2.444 s`** vs `107.094 s` baseline -> **KEPT**)
  - Money-losing CPU dropped from `3.519 s` to `2.103 s` (`-40%`).

### Step 3: Batch Wall-Time Deadline Guard (`verifier.py`)
- Tracks monotonic elapsed wall time across the batch (`wall_limit_sec = 180.0`, `wall_cutoff_frac = 0.60` -> `108.0 s` cutoff, overridable via `BLAST_WALL_LIMIT_SEC` / `BLAST_WALL_CUTOFF_FRAC`).
- Past 60% of the limit, stops forking the verifier and emits the 100%-recall static selection for all remaining commits.
- **Scorer-limit Docker benchmark (`--cpus 2 --read-only --user 65534:65534`, 3 runs)**:
  - 30-sample: Simulated **`94.581 s`**, Misses **`0 / 30`**, Median CPU **`10.211 s`** (runs: `10.271 s`, `10.211 s`, `10.167 s`), Total **`104.792 s`** (**KEPT**, zero overhead vs Step 2).
  - 1.5x batch (45 commits, default 180 s limit under `--cpus 2`): wall **`17.89 s`** (9.9% of 180 s limit), Simulated `160.917 s`, Misses **`0 / 45`**.
  - 1.5x batch (45 commits, forced `BLAST_WALL_LIMIT_SEC=5.0` -> `3.0 s` cutoff): guard fired cleanly mid-batch (`4.67 s`), wall **`6.24 s`**, Misses **`0 / 45`**.

### Step 4 & Step 5: Differential Test (`diff_test.py`) & Shadow-Expression Pruning Audit
- Built `diff_test.py`: generates 19 synthetic edits against pinned commit `eb11aec` covering all 9 categories (`constant`, `dict_value`, `condition`, `condition_rare`, `condition_equiv`, `return_value`, `decorator`, `signature`, `import`, `class_body`, `fixture`) plus verifier safety stress tests (`verifier_timeout` infinite loop, `verifier_error` syntax error). Compares every dropped test (`static_set - verified_set`) against real `pytest` (`SKIP_INTEGRATION=0`, catching `FAILED`, `ERROR`, timeouts, and pytest 9 `SUBFAILED`).
- **Critical finding (shadow-expression pruning enabled)**:
  - `diff_test.py` caught **2 wrong drops** on `cond_rare_source_none_trap` (`sqlglot/optimizer/qualify_columns.py`, `if source is None:` -> `if source is None and False:`): because `source` was never `None` during the first 16 cheap canary tests, shadow checking saw `0` divergences across 150+ calls and dropped all 76 tests — wrongly dropping `TestExecutor::test_execute_tpcds` and `TestOptimizer::test_qualify_columns`, both of which FAILED in real pytest (which would incur a +300 s miss penalty!).
  - All other verifier paths (`safety_timeout_infinite_loop`, `safety_syntax_error`, and direct run-and-drop) had **0 wrong drops**.
- **Action taken (per Step 5 requirement)**:
  - Disabled speculative unrun-test pruning (`enable_shadow_pruning = False`, `enable_laplace_pruning = False`) in `verifier.py` so **only tests that actually execute and pass** inside the isolated forked child are ever dropped.
  - Re-ran `diff_test.py` across all 19 synthetic edits -> **`TOTAL WRONG DROPS: 0`**.
- **Measured savings of shadow-expression pruning & final Step 5 score (`--cpus 2 --read-only --user 65534:65534`, 3 runs)**:
  - With shadow pruning (Step 3): Simulated `94.581 s` + Median CPU `10.211 s` = **`104.792 s`** (0 sample misses, but **2 wrong drops** on differential test).
  - Without shadow pruning (Step 5, zero-wrong-drop verified): Simulated **`144.822 s`** + Median CPU **`9.402 s`** (runs: `9.402 s`, `9.360 s`, `9.561 s`) = **`154.224 s`** (**0 sample misses**, **0/19 differential wrong drops**).
  - **Shadow-expression pruning was saving `50.241 s` simulated / `49.433 s` net** on the 30 samples (from the two covered `is`->`==` edits hitting `test_execute_tpcds`), at the risk of +300 s misses on rare-True branch conditions.
  - Even with shadow pruning disabled, zero-wrong-drop Hybrid v3 (`154.224 s`) beats Pure-Static v1 (`166.102 s`) by **`-11.878 s`** on 30 samples with 0 speculative miss risk.

### Branch `hybrid-v3-econ` Summary Table (30 Samples, Exact Scorer Docker Limits)
| Step | Description | Simulated | Median CPU | Total | Sample Misses | Diff-Test Wrong Drops |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Pure-Static v1 (`90f2d8f`) | Static coverage + AST + packer | 165.365 s | 0.737 s | 166.102 s | 0 | — |
| Step 1 (`9a38b9b` + `econ_report.py`) | Hybrid v2 baseline (shadow ON) | 94.581 s | 12.513 s | 107.094 s | 0 | 2 (shadow) |
| Step 2 (`98588d5`) | + Economic gate & saving/CPU order (shadow ON) | 94.581 s | 10.069 s | 104.650 s | 0 | 2 (shadow) |
| Step 3 (`e490f4d`) | + 60% wall-time deadline guard (shadow ON) | 94.581 s | 10.211 s | 104.792 s | 0 | 2 (shadow) |
| **Step 4–5 (HEAD)** | **Shadow & Laplace unrun-pruning DISABLED** | **144.822 s** | **9.402 s** | **154.224 s** | **0** | **0 / 19** |





