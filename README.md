# Blast-Radius Test Selector for sqlglot

A containerised blast-radius test selector and worker packer for [sqlglot](https://github.com/tobymao/sqlglot) at commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`.

Given `/data/commits/<id>.patch` and `/data/tests.json`, the container prints one JSON line per commit on stdout:

```json
{"commit":"k7q2","workers":[[12,845,901],[3,77]]}
```

selecting the tests that could be affected by each patch and packing them onto 1–8 workers to minimize `max(worker_load) + 2s * workers_used + CPU_time` while avoiding the 300 s miss penalty.

## Rules Compliance

- **No patch-to-test lookup tables**: No sample patches, commit IDs, hashes, answers, or tables keyed by patch or edit kind exist anywhere in this repository or Docker image (`sample/` and `output.jsonl` are excluded via `.gitignore` and `.dockerignore`).
- **Map derived strictly from the pinned commit**: `map.pkl.gz` is generated exclusively from sqlglot at commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa` by running its own test suite under Python 3.13 `sys.monitoring` and static AST/import inspection (`build_map.py`). Test entries are keyed by pytest node ID and mapped dynamically to `/data/tests.json` numbers at runtime.
- **No mutation testing**: No mutation-derived maps are used.
- **Allowed sources only**: The runtime Docker build downloads only from Docker Hub (pinned `python:3.13-slim` digest), PyPI (`requirements.lock`), and sqlglot's official GitHub archive at the pinned commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`.

## Architecture

| File | Role |
| --- | --- |
| `build_map.py` | Offline/build-stage map generator. Runs sqlglot's full suite (`SKIP_INTEGRATION=0`) inside the pinned `python:3.13-slim` environment with `sys.monitoring` (`LINE` + `PY_START`), `open()` fixture hooks, `Dialect`/`Expr` runtime hooks, `setUpClass` attribution, IPC-pickling `_SyncExecutor` for `ProcessPoolExecutor` coverage, and `expr_profiler.py` per-test expression-site profiling. Emits `map.pkl.gz` (`pickle` protocol 4 + `zlib`). |
| `map.pkl.gz` | Compact (~2.43 MB) commit-derived artifact storing per-test line/function/class/fixture/dialect/expression bitsets, per-test expression-site profiles (7,427 sites across 117 files; import-time sites excluded), static AST & import graphs, runtime MRO subclass hierarchy, and individually compressed base Python sources. |
| `expr_profiler.py` | Build-time AST expression-site profiler and runtime soundness prover (`prove_unchanged_test_bits`). At build time (pinned commit `eb11aec` only), records each test's observed branch outcomes and operand comparison safety bits at single-op `Compare` and `If`/`IfExp`/`While` sites. At runtime, for single-file single-expression edits, drops test `T` only when `T`'s own recorded profile mathematically proves identical evaluation (`is`→`==`, identity-safe `==`→`is`, pure primitive `<`↔`<=`/`>`↔`>=`, short-circuit `E and X`/`E or X`, and strictly-pure branch rewrites); any missing profile, unseen test, or uncertainty keeps the test. |
| `patch_parser.py` | Pure-stdlib unified diff parser, in-memory patch applier, file classifier, and Python AST/encoding diff analyzer (including dict-values-only change detection). |
| `selector.py` | Runtime selector combining tiered static+dynamic blast-radius rules, dict-value-only reader dataflow filtering, build-time test-specific profile proving, cost-aware FR4 inclusion (`P(fail) * 300 > runtime / k`), strict output validation, and conservative fallbacks. `BLAST_VERIFIER_MODE` defaults to `off` (so `verifier.py` is not imported unless explicitly enabled via `single_cheap` or `full`). |
| `verifier.py` | Optional forked in-container verifier (disabled by default via `BLAST_VERIFIER_MODE=off`; opt-in via `BLAST_VERIFIER_MODE=single_cheap` or `full`). Runs with in-memory `sys.meta_path` & `open()` patch overlays, `stdout` redirected to `/dev/null`, economic gating, and a 60% batch wall-time deadline guard. Speculative unrun-test pruning (shadow-expression and Laplace pruning) is disabled; only tests that actually execute and pass may be dropped. |
| `packer.py` | Exact integer-millisecond worker packer choosing `k` in `1..8` via lower-bound pruning, LPT initialization, and move/1-1 swap local search. |
| `diff_test.py` | 133-edit synthetic differential test suite verifying every dropped test against real `pytest` (`SKIP_INTEGRATION=0`, pytest 9 subtests), requiring 0 wrong drops. |
| `eval.py` | Local evaluation and 3-fold (20/10) leave-some-out cross-validation script using `score.py`. |
| `tests/test_robustness.py` | Standalone synthetic patch robustness suite (18 tests covering all file classes and edge cases). |
| `Dockerfile` | Runtime image on the pinned `python:3.13-slim` digest with `requirements.lock`, sqlglot at `eb11aec`, and precompiled `.pyc` readable by `nobody`. |

## Usage

### Build and run under exact scorer constraints
```sh
make sample   # build image, run on sample/ as nobody on read-only FS with --network none, and score
make check    # format validation only
```

### Local evaluation & cross-validation
```sh
python eval.py --cv
```

### Robustness suite
```sh
python -S -E -m unittest discover -s tests -v
```

### Regenerating `map.pkl.gz` from scratch
Inside a container built from `python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90` with `pip install -r requirements.lock`, `SKIP_INTEGRATION=0`, and sqlglot checked out at `eb11aec4be8cb2122afe713a4816662c3f0fb2fa` at `/sqlglot`:

```sh
python build_map.py --sqlglot-dir /sqlglot --output /out/map.pkl.gz --workers 4
```
