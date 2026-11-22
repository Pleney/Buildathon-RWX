# Blast-Radius Test Selector for sqlglot

A containerised, stdlib-only blast-radius test selector and worker packer for [sqlglot](https://github.com/tobymao/sqlglot) at commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`.

Given `/data/commits/<id>.patch` and `/data/tests.json`, the container prints one JSON line per commit on stdout:

```json
{"commit":"k7q2","workers":[[12,845,901],[3,77]]}
```

selecting the tests that could be affected by each patch and packing them onto 1–8 workers to minimize `max(worker_load) + 2s * workers_used + CPU_time` while avoiding the 300 s miss penalty.

## Rules Compliance

- **No patch-to-test lookup tables**: No sample patches, commit IDs, hashes, answers, or tables keyed by patch or edit kind exist anywhere in this repository or Docker image (`sample/` and `output.jsonl` are excluded via `.gitignore` and `.dockerignore`).
- **Artifacts derived strictly from the pinned commit**: `map.pkl.gz` (frozen static + coverage map) and `profiles.pkl.gz` (2x-stability-checked runtime behavior profiles) are generated exclusively from sqlglot at commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa` by running its own test suite (`SKIP_INTEGRATION=0`) in single-process plain-pytest order. Test entries are keyed by pytest node ID and mapped dynamically to `/data/tests.json` numbers at runtime.
- **No mutation testing**: No mutation-derived maps or precomputed mutation failure tables are used.
- **Zero-network runtime Docker build**: The runtime `Dockerfile` uses only the pinned `python:3.13-slim` base image digest and Python's standard library — zero PyPI or external downloads at image build time (~1.4 s no-cache build).

## Architecture

| File | Role |
| --- | --- |
| `build_map.py` | Offline/build-stage static+coverage map generator. Runs sqlglot's full suite (`SKIP_INTEGRATION=0`) inside the pinned `python:3.13-slim` environment with `sys.monitoring` (`LINE` + `PY_START`), `open()` fixture hooks, `Dialect`/`Expr` runtime hooks, `setUpClass` attribution, and IPC-pickling `_SyncExecutor` for `ProcessPoolExecutor` coverage. Emits `map.pkl.gz`. |
| `build_profiles.py` | Offline/build-stage 2x-stability profile generator. Runs the full suite twice in plain-pytest single-process order, diffs every site/table across both runs, drops any unstable or import-time site automatically, and emits `profiles.pkl.gz`. |
| `map.pkl.gz` | Frozen (~2.24 MB) static+coverage artifact storing per-test line/function/class/fixture/dialect/expression bitsets, static AST & import graphs, runtime MRO subclass hierarchy, and individually compressed base Python sources. |
| `profiles.pkl.gz` | Stability-verified (~0.71 MB; `map + profiles = 2.95 MB`) per-test behavioral profile artifact containing: expression-site profiles (`7,786` sites), sub-expression reachability (`6,042` sites), `sqlglot/__init__.py` import-time functions, class-level mapping-key lookup profiles (`461` tables), ordered comparison integer delta ranges (`235` sites), bounded constant-equality value sets (`643` sites), and default-argument omission masks (`599` functions). |
| `expr_profiler.py` | Build-time AST instrumentation and runtime soundness prover (`prove_unchanged_test_bits`). Proves a test unchanged across single- and multi-site commits via First-Divergence Induction: sub-expression reachability, operand comparison safety (P1–P6), ordered comparison delta ranges (P7), bounded constant-equality value sets (P8), and default-argument omission profiles (P9). Any missing profile, unstable site, unseen test, or uncertainty keeps the test. |
| `mapping_profiler.py` | Per-test mapping-key lookup profiler and runtime soundness prover for `Dialect`, `Tokenizer`, `Parser`, and `Generator` class-level dict tables (plus `Generator._dispatch` -> `TRANSFORMS`). Separates value lookups, membership checks, `len`/`bool`, and wholesale reads; handles key-derived tries/sets, `TIME_MAPPING`/`INVERSE_TIME_MAPPING` aliases, pure closure-factory values, and import-time cross-key subscripts. |
| `patch_parser.py` | Pure-stdlib unified diff parser, in-memory patch applier, file classifier, and Python AST/encoding diff analyzer (including dict-values-only detection and Phase 3 commit-level equivalence checks: alpha-equivalent local variable renames, docstring-only narrowing, and unreferenced import-time-pure added public functions). |
| `selector.py` | Runtime stdlib-only selector combining tiered static+dynamic blast-radius rules, per-file soundness-proven profile pruning (Phases 1–4), cost-aware FR4 inclusion (`P(fail) * 300 > runtime / k`), strict output validation, and conservative fallbacks. `BLAST_VERIFIER_MODE` defaults to `off` so `verifier.py` is never imported unless explicitly enabled. |
| `verifier.py` | Optional forked in-container verifier (**disabled by default** via `BLAST_VERIFIER_MODE=off`; opt-in via `single_cheap` or `full` only when `/sqlglot` is mounted). Speculative unrun-test pruning (`enable_shadow_pruning`, `enable_laplace_pruning`) is permanently disabled (`False`). |
| `packer.py` | Exact integer-millisecond worker packer choosing `k` in `1..8` via lower-bound pruning, LPT initialization, and move/1-1 swap local search. |
| `gate.sh` / `gate_runner.py` | Automated 6-check release gate: no-cache Docker build (< 6 min), exact scorer sample run x3 (0 misses, CPU < 2 s, deterministic, verifier off), robustness suite, `diff_test.py --quick` (0 wrong drops on real pytest), 1.5x batch determinism (< 60 s), and repo size / zero-leak audit. |
| `diff_test.py` | Synthetic differential test suite (65-edit `--quick` gate + full suite + deterministic 20% `--split holdout\|dev`) verifying every dropped test against real `pytest` (`SKIP_INTEGRATION=0`, pytest 9 subtests), requiring 0 wrong drops. |
| `tests/test_robustness.py` | Standalone synthetic patch robustness suite (24 tests under `python -S -E` covering all file classes, Phase 3 equivalence rules, cross-category renames, hunk-boundary/CRLF/tab/mode/EOF edge cases, `sqlglotc/` whitelist, dotfiles, and adversarial traps). |
| `Dockerfile` | Slim runtime image on the pinned `python:3.13-slim` digest with only `/app` stdlib Python modules, `map.pkl.gz`, `profiles.pkl.gz`, and precompiled `.pyc` readable by `nobody` (`CMD ["python", "-S", "-E", "/app/selector.py"]`). |

## Runtime Feature Toggles (Zero-Revert Ablation)

Every phase can be toggled independently via environment variables (all default to `1` except verifier which defaults to `off`):

- `BLAST_VERIFIER_MODE=off` (default `off`; `single_cheap` / `full` opt-in only)
- `BLAST_DISABLE_PROFILES=0` (set `1` to disable all build-time profiles)
- `BLAST_P1_REACH=1` (Phase 1 sub-expression reachability)
- `BLAST_P1_MULTISITE=1` (Phase 1 per-file multi-site conjunction)
- `BLAST_P1_ROOT_INIT=1` (Phase 1 `sqlglot/__init__.py` non-import-time function scoping)
- `BLAST_P2_MAPPINGS=1` (Phase 2 per-test mapping-key lookup profiles)
- `BLAST_P3_EQUIV=1` (Phase 3 commit-level equivalence: alpha-equiv local renames, docstring narrowing, unreferenced pure added functions)
- `BLAST_P4_RANGES=1` (Phase 4 numeric delta ranges P7, constant-equality sets P8, default-argument omission profiles P9)

## Usage

### Build and run under exact scorer constraints
```sh
make sample   # build image, run on sample/ as nobody on read-only FS with --network none, and score
make check    # format validation only
```

### Full release gate
```sh
bash gate.sh
```

### Robustness suite
```sh
python -S -E tests/test_robustness.py
```

### Regenerating `map.pkl.gz` and `profiles.pkl.gz` from scratch
Inside a build container (`python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90` with `pip install -r requirements.lock`, `SKIP_INTEGRATION=0`, and sqlglot checked out at `eb11aec4be8cb2122afe713a4816662c3f0fb2fa` at `/sqlglot`):

```sh
python build_map.py --sqlglot-dir /sqlglot --output /out/map.pkl.gz --workers 4
python build_profiles.py --sqlglot-dir /sqlglot --map /out/map.pkl.gz --output /out/profiles.pkl.gz --stability-runs 2
```
