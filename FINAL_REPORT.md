# Final Miss-Hunting, Import-Time Refinement & Submission Readiness Report

**Date**: 2026-11-22 20:25 SGT  
**Target Repository**: `sqlglot` pinned at `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`  
**Submission Repository**: `https://github.com/Pleney/Buildathon-RWX`

---

## 1. Candidate Commits (`origin/main` vs `LATEST_SAFE.txt`) & `gate.sh` Summary

Two verified hashes are provided so you can choose based on the hidden practice result:

1. **Final Code Commit (`origin/main`, Import-Time Refinement)**:  
   **`53704c9b04a12942111fce42c2633d28bef32104`**  
   - Keeps all **28 unseen-kind miss fixes** from CHECKPOINT 7 (`df9abfb5f71fc577a8d7099189d40a8656a1fb96`, which scored `258.265 s` sim / `3.6–4.4 s` CPU / `0` misses on hidden practice) while replacing coarse function/class/table-level import-time escalation with sound **line/key/Expr First-Divergence escalation** and baking PEP 562 lazy importer edges into `map.pkl.gz` (`+0.01 MB`, `profiles.pkl.gz` untouched).
   - *(The follow-up metadata commit on `origin/main` updates ONLY `FINAL_REPORT.md` and `LATEST_SAFE.txt`; `git diff 53704c9b04a12942111fce42c2633d28bef32104 HEAD -- selector.py patch_parser.py packer.py expr_profiler.py mapping_profiler.py verifier.py Dockerfile map.pkl.gz profiles.pkl.gz` is completely empty, so the container image and all runtime selection behavior are 100% byte-identical to `53704c9b04a12942111fce42c2633d28bef32104`.)*
2. **Practice-Proven Safe Fallback (`LATEST_SAFE.txt`, CHECKPOINT 6)**:  
   **`2d67ebaf3f56790c2cd9438893d42920b0297e59`**  
   - Scored **`195.5 s`** on hidden practice (`194.047 s` sim + `1.41 s` CPU, **`0` misses** in 3/3 runs).
   - `LATEST_SAFE.txt` remains pinned to `2d67ebaf3f56790c2cd9438893d42920b0297e59` until `53704c9b04a12942111fce42c2633d28bef32104`'s hidden practice result is confirmed.

### Hard-Rule Compliance
- `BLAST_VERIFIER_MODE=off` default (`verifier.py` not imported at runtime), shadow & Laplace pruning disabled (`False`).
- Zero new pruning families; `profiles.pkl.gz` (`0.71 MB`) untouched; `map.pkl.gz` updated via 2x stability-verified import/collection-time probe only (`2.24 MB -> 2.25 MB`, 100% stable across 2 fresh runs).
- Monotonic vs `2d67eba` across all **302 existing benchmarks** (`30` real samples + `126` original synthetic + `76` Phase-5 + `70` Checkpoint-6 unseen edge cases): **`0` regressions** (zero tests dropped vs `2d67eba`), **30 real samples `101.564 s` (`+0.000 s`, byte-identical)**, **70 unseen edge cases byte-identical**, **`0` misses**.
- Zero sample commit IDs, patch contents, or answers in any tracked file (`0` exact-token matches across all tracked files; `sample/` and `output.jsonl` untracked).

### `gate.sh` Summary Line on `53704c9` (6/6 PASS)
```text
PASS | sim=101.564s cpu=1.564s total=103.128s misses=0 diff_wrong=0 build=4.1s batch15x=2.7s art=2.96MB repo=13.45MB (gate_wall=1146.7s)
```
- `[gate 1/6]` No-cache Docker build: **`4.1 s`** (`<= 360 s`)
- `[gate 2/6]` Sample x3 under exact scorer flags: **`sim = 101.564 s`**, **`med_cpu = 1.564 s`** (`[1.585, 1.564, 1.559]`), **`misses = 0`**
- `[gate 3/6]` Robustness suite (`python -S -E`, **29/29 tests**): **PASS**
- `[gate 4/6]` Quick differential test (`diff_test.py --quick`, **65 edits**, **5,833 dropped tests** verified by real `pytest`): **`TOTAL WRONG DROPS: 0`** (`1119.8 s`)
- `[gate 5/6]` 1.5x batch (45 commits) x3 under `--cpus 2`: **`walls = [2.74, 2.54, 2.56] s`**, **3/3 byte-identical (`deterministic=True`)**
- `[gate 6/6]` Repo size & zero leaks: **`map 2.25 MB + profiles 0.71 MB = 2.96 MB`** (`<= 6 MB`), **`.git 9.84 MB + tracked 3.61 MB = 13.45 MB`** (`<= 15 MB`), **`0` leaks**

---

## 2. Import-Time Refinement (`df9abfb` -> `53704c9`)

After CHECKPOINT 7 (`df9abfb`, `0` misses on hidden practice) showed `+64.2 s` simulated time (`258.265 s` vs `2d67eba`'s `194.047 s`) and `+3.0 s` CPU (`3.6–4.4 s` vs `1.41 s`), root-cause profiling isolated two causes:
1. Coarse function/table/Expr escalation: editing ANY line inside one of the 297 `import_time_funcs` (or any key in core base tables / any Expr referenced by an import-time function) escalated to all `_importer_tests(rel)` even when the edited line/key never executed during import/collection.
2. Uncached AST scan in `_ensure_lazy_importers()`: scanning 241 transitive modules in `module_importers["sqlglot.dialects"]` cost `1,529.7 ms` on host (`~3 s` under `--cpus 2`).

### Sound First-Divergence Fixes (`53704c9`)
- **2x Stability-Verified Import/Collection-Time Probe (`build_map.py --update-import-data`, `31.9 s`, 100% stable: `fl=True, expr=True, dk=True`)**:
  - `import_time_func_lines`: exact body lines executed during `import sqlglot` + pytest collection (`32` files, `297` functions, `2,386` body lines).
  - `import_time_exprs`: exact `Expr` subclasses actually instantiated or checked during import/collection (`92` classes, plus MRO propagation).
  - `import_time_dict_keys`: exact keys accessed on core base tables during import/collection (`27` tables, with wholesale-iterated tables conservatively flagged).
  - `lazy_importers_baked=True`: pre-baked PEP 562 `__getattr__` lazy importer edges directly into `module_importers` (`map.pkl.gz`: `2.24 MB -> 2.25 MB`; `profiles.pkl.gz` unchanged).
- **Selector Refinement (`selector.py`)**:
  - Escalate a changed `import_time_func` to `_importer_tests(rel)` ONLY if `scope_changed` (signature/decorators/locals/annotations) OR an edited line / enclosing statement / deleted span overlaps `import_time_func_lines[rel][qn]`.
  - Escalate core base tables by exact changed keys vs `import_time_dict_keys` (wholesale tables always escalate).
  - Restrict `Expr` import-time escalation to `Expr` subclasses whose MRO intersects `import_time_exprs`.
  - Cut `_ensure_lazy_importers()` from `1,529.7 ms` -> **`0.002 ms`** (with raw-bytes pre-filter fallback if unbaked) and memoized `_importer_tests(rel)` (`0.001 ms` cached).

### Verification Results (`53704c9`)
- **Task 2 Unseen-Kind Suite (170 edits, full 1,620-test real `pytest`)**: **`0 / 170 misses`**. Only `5 / 170` edits changed relative to `df9abfb`: `1` grew (`f1_expr_core_slots` `1543 -> 1620`), `4` shrunk cleanly back to `2d67eba` (`f2_parser_no_paren_fn` `1620 -> 26`, `f2_parser_range` `1620 -> 5`, `f4_errmsg_parse_maxnodes` `1620 -> 3`, `f4_errmsg_jsonpath` `1620 -> 3`), and all 4 passed full 1,620-test real `pytest` (including `test_max_nodes` & `test_execute_tpcds`) with `0` misses.
- **302 Older Benchmarks (`0` regressions vs `2d67eba`, `0` misses)**:
  - `sample30`: **`101.564 s`** (`+0.000 s` vs `2d67eba`, byte-identical)
  - `synth126`: **`421.602 s`** (recovered **`-334.060 s`** vs `df9abfb`'s `755.662 s`; `+0.005 s` vs `2d67eba`)
  - `phase5_76`: **`385.928 s`** (`0` regressions vs `2d67eba`)
  - `unseen70`: **`1264.493 s`** (byte-identical to `2d67eba` & `df9abfb`)
- **45 Realistic Single-Token Mutations Inside `import_time_funcs`**: total tests selected `2d67eba = 11,189`, `df9abfb = 62,794`, `53704c9 = 27,650`; all **27 / 45** edits that dropped tests vs `df9abfb` (`35,144` tests dropped) were verified against full 1,620-test real `pytest` -> **`0 / 27` wrong drops**.
- **40-Commit Mixed CPU Benchmark** (30 samples + 10 commits invoking `_importer_tests`, x3 under exact `--cpus 2` scorer flags): **`df9abfb = 3.576 s` median -> `53704c9 = 2.172 s` median (`-1.404 s` wall)**, 3/3 byte-identical, `0` stderr.
- **Robustness Unit Suite (`tests/test_robustness.py`)**: **29 / 29 PASS** (`3.28 s`).

---

## 3. Fresh-Clone Submission Check from GitHub (`origin/main`)

Cloned `https://github.com/Pleney/Buildathon-RWX` into an empty temporary directory, checked out exact `origin/main`, built with **`docker build --no-cache --network none --platform linux/amd64`**, and ran x3 under exact scorer flags (`--network none --cpus 2 --memory 4096m --memory-swap 4096m --pids-limit 256 --read-only --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL --security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp`):

- **`53704c9b04a12942111fce42c2633d28bef32104` (Pushed `origin/main`, all 30 sample commits scored with `score.py`)**:
  - **Repo hygiene**: `.git 7.33 MB + tracked 3.44 MB (30 files) = 10.77 MB` (`< 15 MB`), `git ls-files | grep -iE "sample|output"` = **empty**, `submodules = False`, `LFS = False`.
  - **Docker build (`--no-cache --network none --platform linux/amd64`)**: **OK in `6.97 s`**.
  - **3x exact scorer run (all 30 sample commits, `/data:ro` without `answers.json`)**: `wall = [2.162, 2.059, 2.013] s`, valid JSONL (`30` lines, `1393 B`), **`0` stderr bytes**, **3/3 byte-identical** (`sha256 = fe014fd42e41a417`).
  - **Scorer (`score.py`)**: **`Simulated: 101.564 s`**, **`Misses: 0`**.
- **Earlier Fresh-Clone Baselines**:
  - `2d67ebaf3f56790c2cd9438893d42920b0297e59`: `.git 7.28 MB + tracked 3.52 MB = 10.81 MB`, build `7.37 s`, x3 `wall = [1.356, 1.163, 1.165] s`, 3/3 byte-identical, `0` stderr.
  - `df9abfb5f71fc577a8d7099189d40a8656a1fb96`: `.git 7.30 MB + tracked 3.55 MB = 10.85 MB`, build `3.87 s`, x3 `wall = [1.515, 1.431, 1.414] s`, 3/3 byte-identical, `0` stderr.

---

## 4. Task 2 — Unseen-Kind Miss Hunt Summary (170 New Edits vs Real `pytest`)

Generated **170 brand-new edits** across **8 priority families** and verified EVERY unselected test (`all_1620 - selected`) against real `pytest` (`SKIP_INTEGRATION=0`, including both `test_max_nodes` and `test_execute_tpcds`, `0` timeouts, `0` skipped tests):

| Family | New Edits | Misses on `2d67eba` | Misses on `df9abfb` | Misses on `53704c9` (Full 1,620 tests) |
| --- | ---: | ---: | ---: | ---: |
| `f1_init_expr_helper` (`__init__.py`, `dialects/__init__.py`, `expressions/*`, `helper.py`) | 28 | 4 | 0 | **0** |
| `f2_tables_enums` (non-dict class tables, sets/lists/tuples, module constants, Enums) | 26 | 8 | 0 | **0** |
| `f3_core_modules` (`executor/*`, `optimizer/*`, `planner`, `serde`, `jsonpath`, `schema`, `lineage`, `diff`, `transforms`) | 25 | 3 | 0 | **0** |
| `f4_oop_msgs_sigs` (error msgs, f-strings, defaults, decorators, sigs, overrides, class attrs, base swaps) | 24 | 3 | 0 | **0** |
| `f5_docs_ws_boundaries` (doctests, comments, line shifts + bugs, `else:` / first-line / last-line / multiline boundaries) | 17 | 2 | 0 | **0** |
| `f6_multi_helpers_fixtures` (4-way mixed commits, `tests/helpers.py`, `tests/fixtures/*`) | 14 | 0 | 0 | **0** |
| `f7_typing_future_all` (`sqlglot/typing/*`, annotations w/ & w/o `__future__`, `__future__` removal, `TYPE_CHECKING`, `__all__`) | 19 | 5 | 0 | **0** |
| `f8_unusual_formats` (`-U0` mod/del/pure-insert, rename+edit, no trailing NL, CRLF `-U0`, tabs, mode-only, binary, >5,500 lines) | 17 | 3 | 0 | **0** |
| **Total** | **170** | **28** | **0 / 170** | **0 / 170** |

### 7 Structural Root Causes Fixed (All 28 Misses -> 0)
1. **Import-time function execution outside `sqlglot/__init__.py` (12 misses)**: Honored `import_time_funcs` across all Python source files, refined in `53704c9` to exact `import_time_func_lines` + `scope_changed` First-Divergence checks.
2. **Runtime-evaluated annotations without `from __future__ import annotations` + `node.returns` (3 misses)**: Included `node.returns` and `type_params` in `_func_scope_signature`; modules lacking `from __future__ import annotations` escalate signature/annotation edits to `_importer_tests(rel)`.
3. **Zero-context (`-U0`) pure-insertion off-by-1 (`patch_parser.py`, 1 miss)**: Fixed `apply_hunks` `target_idx = h.old_start` when `h.old_count == 0` and anchored `_compute_hunk_changed_lines` at `[old_ln, old_ln + 1]`.
4. **PEP 562 lazy `__getattr__` import edges + unconditional class-header importers (3 misses)**: Baked PEP 562 lazy importer edges into `map.pkl.gz` (with runtime fallback) and always unioned `_importer_tests(rel)` on `changed_class_headers`.
5. **Non-`Expr` Enum (`DType`) & import-time `Expr` subclasses in `sqlglot/expressions/` (2 misses)**: Routed non-`Expr` classes to `_importer_tests(rel)`; escalated `Expr` subclasses whose MRO intersects `import_time_exprs`.
6. **Metaclass-compiled class attrs (`__new__`/`__init_subclass__`), import-time base tables & direct subclass test refs (7 misses)**: Added `_is_metaclass_compiled_attr`, key-level `import_time_dict_keys` escalation on core base tables ahead of the lambda shortcut, and `_test_method_refs_for_name` in `_subclass_dialect_tests`.
7. **Corrupt `profiles.pkl.gz` crash (`selector.py:load_map`)**: Caught read/decompress/unpickle errors cleanly with fallback to `map.pkl.gz`-only static selection.

---

## 5. Failure-Mode Robustness & 90-Commit Batch (Verified under Exact Scorer Flags)

| Check | Result | Details |
| --- | --- | --- |
| Mode 1: Garbled binary / truncated / nonexistent file / bad context | **PASS** | `rc=0` (`2.88 s`); all 4 broken commits fell back cleanly to all 1,620 tests validly packed |
| Mode 2: Mismatched `tests.json` (dropped map IDs, extra unmapped IDs, `0.0 s` durations, permuted numbers) | **PASS** | `rc=0` (`1.04 s`); only runtime numbers emitted, unmapped runtime tests conservatively included on non-docs commits, docs-only = `[]` |
| Mode 3a: Missing `profiles.pkl.gz` | **PASS** | `rc=0` (`0.99 s`); clean static fallback |
| Mode 3b: Corrupt / truncated `profiles.pkl.gz` | **PASS** | `rc=0` (`1.05 s`); caught & logged warning to stderr, clean static fallback |
| Mode 3c: Commit-mismatched `profiles.pkl.gz` | **PASS** | `rc=0` (`1.00 s`); profiles ignored cleanly |
| Mode 4: Injected exception in a single commit | **PASS** | Faulted commit fell back to all 1,620 tests; all neighboring commits byte-identical |
| Mode 5: **90-commit batch x3** under `--cpus 2` | **PASS** | **`wall = [4.136, 4.054, 3.894] s`** (`max 4.136 s << 180 s`), **3/3 byte-identical**, **`0` stderr bytes** |

---

## 6. Known Residual Risks (Explicit Statement)

1. **Multi-step / inter-procedural value propagation through runtime-mutated global state**: Build-time profiles (`expr_profiles`, `reach_profiles`, `mapping_profiles`, `range_profiles`, `const_profiles`, `default_arg_profiles`) prove non-divergence using First-Divergence Induction under the pinned suite's single-process execution order. A pathological patch that mutates module-level state via an indirect dynamic mechanism (`setattr`, `globals()`, `exec`, `eval`, or `monkeypatch` inside a non-import-time function body whose own edited site was never executed during the suite) could evade static AST classification if it also modifies an unreached line. AST diffs detect `globals`/`eval`/`exec` traps on renames and check all imports/class attrs/module vars/signatures conservatively.
2. **Unprofiled test-order sensitivity**: `profiles.pkl.gz` was built twice in plain single-process `pytest` order (with any unstable site automatically dropped and `test_lazy_load` permanently marked unprofiled). If the hidden scorer executes tests inside isolated workers in a different order AND a hidden commit relies on a cross-test cached object created by an earlier test, rare order-dependent coverage differences could theoretically occur.
3. **Line-coverage granularity on dead/unreached branches**: `p_dead_stmt_func = 0.0` drops tests when neither a changed line nor its enclosing statement had any recorded `sys.monitoring` `LINE` hit in the pinned suite (while signature/decorators/locals/yield-ness are unchanged and no edited line ran at import/collection time). If a hidden test file not in `tests.json` at build time existed, `unmapped_runtime_tests` covers new tests whenever selection is non-empty, whereas a purely dead-branch edit yields `[]`.
4. **Practice score trade-off on import-time-executed lines**: Under `53704c9`, edits strictly to non-import-time lines/keys/Exprs avoid full importer escalation, while edits that directly touch an import/collection-executed line, key, or `Expr` MRO still escalate to `_importer_tests(rel)` to prevent `+300 s` collection misses. `LATEST_SAFE.txt` remains pinned to `2d67ebaf3f56790c2cd9438893d42920b0297e59` until `53704c9`'s hidden practice score is confirmed.
