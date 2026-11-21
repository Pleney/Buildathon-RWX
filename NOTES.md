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
