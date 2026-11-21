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
- **Allowed sources only**: The runtime Docker build uses only the pinned `python:3.13-slim` base image from Docker Hub and files committed in this repository. Regenerating `map.pkl.gz` downloads only from PyPI (`requirements.lock`) and sqlglot at commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`.

## Architecture

| File | Role |
| --- | --- |
| `build_map.py` | Offline/build-stage map generator. Runs sqlglot's full suite (`SKIP_INTEGRATION=0`) inside the pinned `python:3.13-slim` environment with `sys.monitoring` (`LINE` + `PY_START`), `open()` fixture hooks, `Dialect`/`Expr` runtime hooks, `setUpClass` attribution, and IPC-pickling `_SyncExecutor` for `ProcessPoolExecutor` coverage. Emits `map.pkl.gz` (`pickle` protocol 4 + `zlib`). |
| `map.pkl.gz` | Compact (~2.24 MB) commit-derived artifact storing per-test line/function/class/fixture/dialect/expression bitsets, static AST & import graphs, runtime MRO subclass hierarchy, and individually compressed base Python sources. |
| `patch_parser.py` | Pure-stdlib unified diff parser, in-memory patch applier, file classifier, and Python AST/encoding diff analyzer. |
| `selector.py` | Pure-stdlib runtime selector (`python -S -E`). Applies tiered blast-radius rules + cost-aware FR4 inclusion (`P(fail) * 300 > runtime / k`) with conservative fallbacks on syntax errors, config/Rust/`__init__.py`/unknown files, malformed patches, or internal errors. |
| `packer.py` | Exact integer-millisecond worker packer choosing `k` in `1..8` via lower-bound pruning, LPT initialization, and move/1-1 swap local search. |
| `eval.py` | Local evaluation and 3-fold (20/10) leave-some-out cross-validation script using `score.py`. |
| `tests/test_robustness.py` | Standalone synthetic patch robustness suite (18 tests covering all file classes and edge cases). |
| `Dockerfile` | Minimal runtime image on the pinned `python:3.13-slim` digest with precompiled `.pyc` readable by `nobody`. |

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
