# Final Miss-Hunting & Submission Readiness Report

**Date**: 2026-11-22 18:55 SGT (completed >1 hour before 20:00 SGT hard stop)  
**Target Repository**: `sqlglot` pinned at `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`  
**Submission Repository**: `https://github.com/Pleney/Buildathon-RWX`

---

## 1. Final Recommended Commit & `gate.sh` Summary

- **Verified Final Code Commit (`origin/main`, CHECKPOINT 7)**:  
  **`df9abfb5f71fc577a8d7099189d40a8656a1fb96`**  
  *(If `origin/main` HEAD has one follow-up metadata commit adding `FINAL_REPORT.md` and updating `LATEST_SAFE.txt`, `git diff df9abfb5f71fc577a8d7099189d40a8656a1fb96 HEAD -- selector.py patch_parser.py packer.py expr_profiler.py mapping_profiler.py verifier.py Dockerfile map.pkl.gz profiles.pkl.gz` is empty — the built container image and all runtime selection behavior are 100% byte-identical.)*
- **Previous Practice-Proven Candidate**: `2d67ebaf3f56790c2cd9438893d42920b0297e59` (`195.5 s` on hidden practice: `194.047 s` sim + `1.41 s` CPU, `0` misses in 3/3 runs).
- **Hard-rule compliance**:
  - `BLAST_VERIFIER_MODE=off` default (`verifier.py` not imported at runtime), shadow & Laplace pruning disabled (`False`).
  - Zero new pruning, zero new profile families, zero artifact rebuilds (`map.pkl.gz = 2.24 MB` and `profiles.pkl.gz = 0.71 MB` untouched).
  - Strictly monotonic across all **302 existing benchmarks** (`30` real samples + `126` original synthetic + `76` Phase-5 + `70` Checkpoint-6 unseen edge cases): **`0` regressions** (zero tests dropped vs `2d67eba`), **30 real samples `101.564 s` (`+0.000 s`, byte-identical)**, **70 unseen edge cases byte-identical**.
  - Zero sample commit IDs, patch contents, or answers in any tracked file (`0` exact-token matches across all tracked files; `sample/` and `output.jsonl` untracked).

### `gate.sh` Summary Line (6/6 PASS)
```text
PASS | sim=101.564s cpu=1.398s total=102.962s misses=0 diff_wrong=0 build=3.7s batch15x=2.7s art=2.95MB repo=10.99MB (gate_wall=1024.7s)
```
- `[gate 1/6]` No-cache Docker build: **`3.7 s`** (`<= 360 s`)
- `[gate 2/6]` Sample x3 under exact scorer flags: **`sim = 101.564 s`**, **`med_cpu = 1.398 s`** (`[1.398, 1.393, 1.409]`), **`misses = 0`**
- `[gate 3/6]` Robustness suite (`python -S -E`, **28/28 tests**): **PASS**
- `[gate 4/6]` Quick differential test (`diff_test.py --quick`, **65 edits**, **5,804 dropped tests** verified by real `pytest`): **`TOTAL WRONG DROPS: 0`**
- `[gate 5/6]` 1.5x batch (45 commits) x3 under `--cpus 2`: **`walls = [2.67, 2.40, 2.36] s`**, **3/3 byte-identical (`deterministic=True`)**
- `[gate 6/6]` Repo size & zero leaks: **`map 2.24 MB + profiles 0.71 MB = 2.95 MB`** (`<= 6 MB`), **`.git 7.44 MB + tracked 3.54 MB = 10.99 MB`** (`<= 15 MB`), **`0` leaks**

---

## 2. Task 2 — Unseen-Kind Miss Hunt (170 New Edits vs Real `pytest`)

Generated **170 brand-new edits** across **8 priority families** and verified EVERY unselected test (`all_1620 - selected`) against real `pytest` (`SKIP_INTEGRATION=0`, including both `test_max_nodes` and `test_execute_tpcds`, `0` timeouts, `0` skipped tests):

| Family | New Edits | Misses on `2d67eba` | Misses on `df9abfb` (Full 1,620 tests) |
| --- | ---: | ---: | ---: |
| `f1_init_expr_helper` (`__init__.py`, `dialects/__init__.py`, `expressions/*`, `helper.py`) | 28 | 4 | **0** |
| `f2_tables_enums` (non-dict class tables, sets/lists/tuples, module constants, Enums) | 26 | 8 | **0** |
| `f3_core_modules` (`executor/*`, `optimizer/*`, `planner`, `serde`, `jsonpath`, `schema`, `lineage`, `diff`, `transforms`) | 25 | 3 | **0** |
| `f4_oop_msgs_sigs` (error msgs, f-strings, defaults, decorators, sigs, overrides, class attrs, base swaps) | 24 | 3 | **0** |
| `f5_docs_ws_boundaries` (doctests, comments, line shifts + bugs, `else:` / first-line / last-line / multiline boundaries) | 17 | 2 | **0** |
| `f6_multi_helpers_fixtures` (4-way mixed commits, `tests/helpers.py`, `tests/fixtures/*`) | 14 | 0 | **0** |
| `f7_typing_future_all` (`sqlglot/typing/*`, annotations w/ & w/o `__future__`, `__future__` removal, `TYPE_CHECKING`, `__all__`) | 19 | 5 | **0** |
| `f8_unusual_formats` (`-U0` mod/del/pure-insert, rename+edit, no trailing NL, CRLF `-U0`, tabs, mode-only, binary, >5,500 lines) | 17 | 3 | **0** |
| **Total** | **170** | **28** | **0 / 170** |

### Root Causes, Minimal Reproducers & Strictly-Conservative Fixes (All 28 Misses -> 0)

Every one of the 28 misses reduced cleanly to **7 principled structural root causes**, all fixed purely in `selector.py` and `patch_parser.py` (with unit regression tests added in `tests/test_robustness.py`):

1. **`import_time_funcs` ignored outside `sqlglot/__init__.py` (12 misses)**:
   - *Reproducers*: `Expr.__init_subclass__` (`f1_expr_core_init_subclass`, `f1_expr_core_req_args`), `helper.seq_get` (`f1_helper_seq_get_body`), `jsonpath.parse` (`f3_jsonpath`, `f5_boundary_first_line_func`, `f8_tabs_jsonpath`, `f8_over_5000_lines_patch`), `simplify.catch` (`f3_opt_simplify`), `simplify.annotate_types_on_change` (`f4_dec_import_time_annotate`), `transforms.preprocess` (`f3_transforms`), `Dialect.get_or_raise` (`f4_dec_remove_classmethod`), `generator.unsupported_args` (`f5_boundary_else_generator`). All ran at module/collection import time (before per-test coverage began), causing collection errors on `tests/dialects/test_athena.py` or missed callers.
   - *Fix (`selector.py`)*: Honored `self.import_time_funcs.get(rel, ())` for ALL Python source files in Section 6 (not only `root_init`): when any changed function ran at import time, select `_importer_tests(rel)` with `p_direct` and disable expression-site dropping for that file.

2. **Runtime-evaluated annotations in modules without `from __future__ import annotations` + missing `node.returns` (3 misses)**:
   - *Reproducers*: Editing function return annotations to an undefined type in `sqlglot/executor/env.py` (`f7_annot_no_future_2_env`), `sqlglot/executor/python.py` (`f7_annot_no_future_3_python`), and `sqlglot/trie.py` (`f7_annot_no_future_4_trie`) crashed with `NameError` at import time while `2d67eba` selected `0` tests (`_func_scope_signature` omitted `node.returns` and ignored `__future__` absence).
   - *Fix (`selector.py`)*: Included `node.returns` and `type_params` in `_func_scope_signature` and added `_has_future_annotations(tree)`. Whenever a module lacks `from __future__ import annotations` and a function's signature/annotations change, select `_importer_tests(rel)` with `p_direct`.

3. **Zero-context (`-U0`) pure-insertion off-by-1 (`patch_parser.py`, 1 miss)**:
   - *Reproducer*: `f8_u0_pure_insert_jsonpath` (`@@ -45,0 +46 @@` pure insertion right after `def parse(...)`): `apply_hunks` used `target_idx = max(0, h.old_start - 1)` instead of `h.old_start` when `h.old_count == 0`, inserting the line inside the preceding class `JSONPathTokenizer` instead of `parse`.
   - *Fix (`patch_parser.py`)*: Set `target_idx = h.old_start if h.old_count == 0 else max(0, h.old_start - 1)` in `apply_hunks`, and anchored `_compute_hunk_changed_lines` at `[old_ln, old_ln + 1]` when `h.old_count == 0`.

4. **PEP 562 lazy `__getattr__` import edges missing from `module_importers` + conditional class-header importers (3 misses)**:
   - *Reproducers*: Syntax errors in `sqlglot/typing/bigquery.py` (`f7_typing_bigquery_ret`) and `sqlglot/typing/snowflake.py` (`f7_typing_snowflake_ret`) crashed `tests/dialects/test_athena.py` during collection because `tests/dialects/test_dialect.py` runs `from sqlglot.dialects import BigQuery, Hive, Snowflake, Spark2` via PEP 562 `__getattr__`, which static import tracing didn't expand; base-class swap `class DuckDB(MySQL):` (`f4_baseswap_duckdb`) hit `if not bits: bits = self._importer_tests(rel)` instead of unconditionally unioning importers.
   - *Fix (`selector.py`)*: Added lazy `_ensure_lazy_importers()` to expand `from sqlglot.dialects import ...` and `from sqlglot.optimizer import ...` edges transitively across `module_importers`; changed Section 5 (`changed_class_headers`) to always union `self._importer_tests(rel)`.

5. **Non-`Expr` class (`DType` Enum) and import-time-instantiated `Expr` (`Select`) in `sqlglot/expressions/` (2 misses)**:
   - *Reproducers*: Mutating `DType.BIGINT` in `sqlglot/expressions/datatypes.py` (`f2_enum_dtype_val`) selected only 29 tests because all classes under `sqlglot/expressions/` were treated as `Expr` subclasses; tightening `Select.arg_types["with_"] = True` (`f1_expr_argtypes_select`) crashed `tests/dialects/test_athena.py` at collection time because `sqlglot/generators/duckdb.py` calls `exp.maybe_parse("WITH ... SELECT ...")` at module import time.
   - *Fix (`selector.py`)*: Restricted the `Expr`-hierarchy path to actual `Expr` subclasses (`short_cls in self.expr_class_file or short_cls == "Expr"`), routing non-`Expr` classes (`DType`) to `_importer_tests(rel)`; for `Expr` subclasses referenced by any `import_time_func` (e.g., `Select`, `Where`, `Column`, `Window`), unioned `_importer_tests(rel)`.

6. **Metaclass-compiled class attributes (`__new__` / `__init_subclass__`), import-time base tables, and direct subclass test refs (7 misses)**:
   - *Reproducers*: `BigQuery.Tokenizer.QUOTES` (`f2_bq_tok_quotes`) and `MySQL.Tokenizer.QUOTES` (`f2_mysql_tok_quotes`) dropped dialect tests because `_TokenizerBase.__init_subclass__` compiles `QUOTES` into `_QUOTES` (making `dial_bits & ref_bits` unsound), AND `tests/test_tokens.py::TestTokens::test_jinja` directly instantiates `BigQuery.Tokenizer()` bypassing `Dialect.get_or_raise`; `Tokenizer.QUOTES`, `IDENTIFIERS`, `SINGLE_TOKENS`, `COMMANDS` (`f2_tok_*`) and `Parser.UNARY_PARSERS` (`f2_parser_unary`, which hit the lambda `direct_attr_lines` shortcut) broke collection-time `exp.maybe_parse(...)` in `duckdb.py`.
   - *Fix (`selector.py`)*: Added `_is_metaclass_compiled_attr` (disabling unsound `dial_bits & ref_bits` intersection on `__new__`/`__init_subclass__`-compiled attributes while preserving `expr_key_bits` and `mapping_profiles`), routed import-time-referenced core base tables (`sqlglot/{tokens,parser,generator,dialects/dialect}.py`) to `_importer_tests(rel)` ahead of the lambda shortcut, and added `_test_method_refs_for_name` to `_subclass_dialect_tests` to capture direct test-method subclass instantiations like `BigQuery.Tokenizer()`.

7. **Task 3 Bug Fix — Corrupt `profiles.pkl.gz` crash (`selector.py:load_map`)**:
   - *Reproducer*: Mounting a corrupt/truncated file at `/app/profiles.pkl.gz` raised an unhandled `zlib.error` in `load_map()`.
   - *Fix (`selector.py`)*: Wrapped `profiles.pkl.gz` read/decompress/unpickle in `try/except` with a stderr warning so the selector falls back cleanly to `map.pkl.gz`-only static selection.

---

## 3. Task 1 — Fresh-Clone Submission Test (Verified on Both `2d67eba` and `df9abfb`)

Cloned `https://github.com/Pleney/Buildathon-RWX` into an empty temporary directory, checked out exact `origin/main`, built with **`docker build --no-cache --network none --platform linux/amd64`**, and ran x3 under exact scorer flags (`--network none --cpus 2 --memory 4096m --memory-swap 4096m --pids-limit 256 --read-only --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL --security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp`):

- **`2d67ebaf3f56790c2cd9438893d42920b0297e59`**: Fresh clone `.git 7.28 MB + tracked 3.52 MB = 10.81 MB` (`< 15 MB`), `0` `sample`/`output` tracked, `submodules=False`, `lfs=False`; `--no-cache --network none` build OK in **`7.37 s`**; x3 exact scorer run `wall = [1.356, 1.163, 1.165] s`, valid JSONL, `0` stderr, **3/3 byte-identical**.
- **`df9abfb5f71fc577a8d7099189d40a8656a1fb96`**: Fresh clone `.git 7.30 MB + tracked 3.55 MB = 10.85 MB` (`< 15 MB`), `0` `sample`/`output` tracked, `submodules=False`, `lfs=False`; `--no-cache --network none` build OK in **`3.87 s`**; x3 exact scorer run `wall = [1.515, 1.431, 1.414] s`, valid JSONL, `0` stderr, **3/3 byte-identical**.

---

## 4. Task 3 — Failure-Mode Robustness & 90-Commit Batch

All 7 checks executed against the rebuilt `--no-cache` `blast-radius-entry` image under exact scorer flags (`--cpus 2 --read-only --tmpfs /tmp:rw,size=64m --network none --user nobody`):

| Check | Result | Details |
| --- | --- | --- |
| Mode 1: Garbled binary / truncated / nonexistent file / bad context | **PASS** | `rc=0` (`2.88 s`); all 4 broken commits fell back cleanly to all 1,620 tests validly packed |
| Mode 2: Mismatched `tests.json` (dropped map IDs, extra unmapped IDs, `0.0 s` durations, permuted numbers) | **PASS** | `rc=0` (`1.04 s`); only runtime numbers emitted, unmapped runtime tests conservatively included on non-docs commits, docs-only = `[]` |
| Mode 3a: Missing `profiles.pkl.gz` | **PASS** | `rc=0` (`0.99 s`); clean static fallback |
| Mode 3b: Corrupt / truncated `profiles.pkl.gz` | **PASS** | `rc=0` (`1.05 s`); caught & logged warning to stderr, clean static fallback |
| Mode 3c: Commit-mismatched `profiles.pkl.gz` | **PASS** | `rc=0` (`1.00 s`); profiles ignored cleanly |
| Mode 4: Injected exception in a single commit | **PASS** | Faulted commit fell back to all 1,620 tests; all neighboring commits byte-identical |
| Mode 5: **90-commit batch x3** under `--cpus 2` | **PASS** | **`wall = [4.136, 4.054, 3.894] s`** (`max 4.136 s << 180 s`), **3/3 byte-identical**, **`0` stderr bytes** |

*(Task 4 optional CPU trim was intentionally skipped: median CPU is already `1.398 s` on 30 commits and `~4.05 s` on 90 commits; avoiding unnecessary refactoring protects correctness and byte-identical determinism.)*

---

## 5. Known Residual Risks (Explicit Statement)

1. **Multi-step / inter-procedural value propagation through runtime-mutated global state**: Build-time profiles (`expr_profiles`, `reach_profiles`, `mapping_profiles`, `range_profiles`, `const_profiles`, `default_arg_profiles`) prove non-divergence using First-Divergence Induction under the pinned suite's single-process execution order. A pathological patch that mutates module-level state via an indirect dynamic mechanism (`setattr`, `globals()`, `exec`, `eval`, or `monkeypatch` inside a non-import-time function body whose own edited site was never executed during the suite) could evade static AST classification if it also modifies an unreached line. In practice, AST diffs detect `globals`/`eval`/`exec` traps on renames and check all imports/class attrs/module vars/signatures conservatively.
2. **Unprofiled test-order sensitivity**: `profiles.pkl.gz` was built twice in plain single-process `pytest` order (with any unstable site automatically dropped and `test_lazy_load` permanently marked unprofiled). If the hidden scorer executes tests inside isolated workers in a different order AND a hidden commit relies on a cross-test cached object created by an earlier test, rare order-dependent coverage differences could theoretically occur — though `test_dialect_entry_points` isolation and our import-time closure significantly mitigate this.
3. **Line-coverage granularity on dead/unreached branches**: `p_dead_stmt_func = 0.0` drops tests when neither a changed line nor its enclosing statement had any recorded `sys.monitoring` `LINE` hit in the pinned suite (while signature/decorators/locals/yield-ness are unchanged and the function is not in `import_time_funcs`). If a patch inside an unreached branch introduces a compile-time effect not captured by `_func_scope_signature`, or if a hidden test file not in `tests.json` at build time existed, `unmapped_runtime_tests` covers new tests whenever selection is non-empty, whereas a purely dead-branch edit yields `[]`.
4. **Practice score delta on unseen import-time edits**: Because `import_time_funcs` now triggers `_importer_tests(rel)` on all source files (fixing 12 real misses), any real hidden commit editing one of the 297 functions that ran during `import sqlglot` / `duckdb.py` collection will run all importers (`~30 s` extra) rather than risking a `+300 s` miss — a deliberate ~10:1 safety-first trade-off. All 30 real sample commits remain 100% byte-identical (`101.564 s`).
