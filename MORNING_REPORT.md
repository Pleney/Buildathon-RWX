# Morning Report — Overnight Blast-Radius Selector Run

**Completed**: `2026-11-22 08:05 SGT` (2h 25m ahead of the 10:30 SGT feature freeze)  
**Branch / Remote**: Developed on `work`, every checkpoint gated by `gate.sh` (6/6 PASS), merged `--no-ff` to `main`, and pushed to `origin/main` (zero force-pushes, zero rebases, zero deleted commits).  
**Hard-Rule Compliance**: Zero per-sample special cases, zero patch-to-test or edit-kind lookups, zero precomputed mutation tables, zero sample commit IDs in any tracked file (`0` leaks verified by `gate.sh`), `BLAST_VERIFIER_MODE=off` default (`verifier.py` not imported), shadow & Laplace unrun-test pruning permanently disabled (`False`).

---

## 1. Candidate Submissions (Recommended Submission Order)

Sorted safest-strong-first. Every phase is also independently toggleable via environment variables (`BLAST_P1_REACH`, `BLAST_P1_MULTISITE`, `BLAST_P1_ROOT_INIT`, `BLAST_P2_MAPPINGS`, `BLAST_P3_EQUIV`, `BLAST_P4_RANGES`, `BLAST_DISABLE_PROFILES`) so you can ablate without reverting.

| Rank | `origin/main` Commit | What's Included | 30 Real Samples (`sim / med_cpu / miss`, Oracle `94.351 s`) | 41 Unseen Hold-Out (`Oracle 35.10 s`) | 76 Brand-New P5 (`Oracle 103.74 s`) | Docker Build (no-cache) | Risk |
| ---: | --- | --- | --- | --- | --- | ---: | --- |
| **1 (Recommended)** | **`HEAD`** *(Phase 6 slim Dockerfile; run `git log origin/main -1 --format=%H`)* | **Phases 1–4 + slim stdlib-only `Dockerfile` (`python -S -E`)** | **`101.564 s`** / `1.327 s` / **`0`** | **`143.22 s`** (`-84.21 s`, **`0` miss**) | **`377.75 s`** (`-259.69 s`, **`0` miss**) | **`1.4 s`** (zero network) | Low |
| **2** | `401eab5f90ae7fce33c9a5881a098bc91aef952a` | **Phases 1–4** (identical selector & profiles, full Dockerfile w/ `/sqlglot` baked in) | **`101.564 s`** / `1.213 s` / **`0`** | **`143.22 s`** (`-84.21 s`, **`0` miss**) | **`377.75 s`** (`-259.69 s`, **`0` miss**) | `42.8 s` | Low |
| **3** | `cbb9984e0ba1206f4454759cc8a1e71b53c23159` | **Phases 1–3** (drops Phase 4 P7/P8/P9 runtime instrumentation) | `101.564 s` / `1.196 s` / `0` | `151.33 s` (`-76.10 s`, `0` miss) | `420.79 s` (`-216.65 s`, `0` miss) | `39.8 s` | Very Low |
| **4** | `aa9dec44e5ff720495b836909e615931fcc5d2e7` | **Phases 1–2** (mapping-key profiles + Phase 1; no Phase 3 equivalence) | `101.564 s` / `1.208 s` / `0` | `203.11 s` (`-24.32 s`, `0` miss) | `569.22 s` (`-68.22 s`, `0` miss) | `41.8 s` | Very Low |
| **5** | `5a84dfc3537f6674bd5ecc55c85c4a93a27348eb` | **Phase 1 only** (reachability, multi-site, `__init__` scoping, 4-tuple spans, 2x stability) | `109.635 s` / `1.165 s` / `0` | `218.34 s` (`-9.09 s`, `0` miss) | `632.44 s` (`-5.00 s`, `0` miss) | `41.2 s` | Very Low |
| **6 (Baseline)** | `2a3d3cbec3ca15d7df268b30ff6ed7b509c1a50b` | Pre-overnight baseline (`200.8 s`, `0` misses on hidden practice) | `109.635 s` / `1.241 s` / `0` | `227.43 s` (`0` miss) | `637.44 s` (`0` miss) | `45.7 s` | Baseline |

**Why #1 (`HEAD`) is the recommended first submission**: Identical blast-radius selection to `401eab5f90ae7fce33c9a5881a098bc91aef952a`, validated with **0 misses across all 232 evaluated edits** (30 real samples + 126 original synthetic + 76 brand-new Phase 5 edits) and **0 wrong drops across 4,864 pytest-verified dropped tests** in `diff_test.py --quick`, while cutting the no-cache Docker build from `42.8 s` to **`1.4 s`** with zero PyPI/GitHub network calls during judge builds. If you prefer keeping `/sqlglot` baked into the image, submit #2 (`401eab5f90ae7fce33c9a5881a098bc91aef952a`); if you want an even more conservative fallback without Phase 4 ranges/const/default-arg profiles, submit #3 (`cbb9984e0ba1206f4454759cc8a1e71b53c23159`).

---

## 2. What Each Phase Gained (Real Samples vs Hold-Out & Broader Sets)

All numbers measured with `BLAST_VERIFIER_MODE=off` using exact `packer.py` cost (`max_load + 2k`) and real `pytest` ground truth (`SKIP_INTEGRATION=0`). **Misses = `0` in every row and every benchmark.**

### Cumulative & Incremental Phase Ablation

| Config | 30 Real Samples (Oracle `94.351 s`) | Orig 126 Synth (Oracle `258.60 s`) | Orig 20 Hold-Out (Oracle `19.55 s`) | New 76 Phase-5 (Oracle `103.74 s`) | New 21 Hold-Out (Oracle `15.55 s`) | Combined 41 Hold-Out (Oracle `35.10 s`) | Misses |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `0_Static` (profiles OFF) | 162.412 s | 695.20 s | 67.18 s | 637.44 s | 176.76 s | 243.94 s | 0 |
| `1_Base` (`2a3d3cb`, P1–P6) | 109.635 s | 533.73 s | 50.67 s | 637.44 s | 176.76 s | 227.43 s | 0 |
| `2_+Phase1` (`5a84dfc`) | 109.635 s (`+0.000 s`) | 427.67 s (`-106.07 s`) | 46.57 s (`-4.09 s`) | 632.44 s (`-5.00 s`) | 171.76 s (`-5.00 s`) | 218.34 s (`-9.09 s`) | 0 |
| `3_+Phase2` (`aa9dec4`) | **101.564 s (`-8.071 s`)** | 421.75 s (`-5.91 s`) | 46.57 s (`0.00 s`) | 569.22 s (`-63.22 s`) | 156.53 s (`-15.23 s`) | 203.11 s (`-15.23 s`) | 0 |
| `4_+Phase3` (`cbb9984`) | 101.564 s (`+0.000 s`) | 421.75 s (`0.00 s`) | 46.57 s (`0.00 s`) | 420.79 s (`-148.43 s`) | 104.75 s (`-51.78 s`) | 151.33 s (`-51.78 s`) | 0 |
| `5_+Phase4` (`401eab5` / `HEAD`) | **101.564 s (`+0.000 s`)** | **421.60 s (`-0.15 s`)** | **46.57 s (`0.00 s`)** | **377.75 s (`-43.04 s`)** | **96.64 s (`-8.11 s`)** | **143.22 s (`-8.11 s`)** | **0** |
| **Total Gain vs `2a3d3cb`** | **`-8.071 s`** (`-52.8%` of gap) | **`-112.13 s`** (`-40.8%` of gap) | **`-4.09 s`** | **`-259.69 s`** (`-48.7%` of gap) | **`-80.12 s`** (`-49.7%` of gap) | **`-84.21 s`** (`-43.8%` of gap) | **0** |

### Phase 5 Brand-New 76-Edit Evaluation by Edit Family (Zero Edits Seen During Development)

| Family | N | `0_Static` / `1_Base` | `+P1` | `+P2` | `+P3` | `+P4` (Final) | Oracle | Saved vs Base | Misses |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `added_function` *(incl. decorator traps)* | 8 | 140.96 s | 140.96 s | 140.96 s | 36.88 s | **36.88 s** | 22.23 s | `-104.08 s` | 0 |
| `mapping_entry` *(val/add/del)* | 10 | 76.89 s | 76.89 s | 34.35 s | 34.35 s | **34.35 s** | 12.25 s | `-42.54 s` | 0 |
| `local_rename` *(incl. param traps)* | 8 | 46.18 s | 46.18 s | 46.18 s | 6.58 s | **6.58 s** | 4.31 s | `-39.60 s` | 0 |
| `default_arg` *(pure defaults + traps)* | 8 | 88.97 s | 88.97 s | 88.97 s | 88.97 s | **54.33 s** | 4.02 s | `-34.64 s` | 0 |
| `multi_file` *(2-file mixed commits)* | 8 | 94.16 s | 94.16 s | 73.48 s | 68.73 s | **66.68 s** | 24.99 s | `-27.48 s` | 0 |
| `const_change` *(eq swaps + thresholds)* | 10 | 39.29 s | 34.29 s | 34.29 s | 34.29 s | **27.94 s** | 18.04 s | `-11.35 s` | 0 |
| `multi_hunk` *(2-hunk same-file)* | 8 | 55.49 s | 55.49 s | 55.49 s | 55.49 s | **55.49 s** | 8.83 s | `0.00 s` | 0 |
| `refactor` *(multi-stmt restructuring)* | 8 | 45.96 s | 45.96 s | 45.96 s | 45.96 s | **45.96 s** | 0.00 s | `0.00 s` | 0 |
| `return_value` *(logic mutations)* | 8 | 49.54 s | 49.54 s | 49.54 s | 49.54 s | **49.54 s** | 9.06 s | `0.00 s` | 0 |
| **Total (76 edits; 21 hold-out)** | **76** | **637.44 s** | **632.44 s** | **569.22 s** | **420.79 s** | **377.75 s** | **103.74 s** | **`-259.69 s`** | **0** |

**Key takeaway**: Every phase contributed substantial, non-overlapping savings on realistic edit families unseen during development (`-259.69 s` on the 76 new edits; `-84.21 s` on the 41 unseen hold-out edits), while conservatively holding `0` misses on harder families (`multi_hunk`, `refactor`, `return_value`) and adversarial traps (broken decorators, param renames, rare-True `source is None`, non-singleton `==` -> `is`, `Presto`/`MySQL` aliased `TIME_MAPPING`).

---

## 3. What Is Disabled and Why

- **`BLAST_VERIFIER_MODE=off` (default)**: Execution-based verification was break-even on hidden practice (`207.4 s` vs `206.7 s`) because heavy tests remain unrunnable; `verifier.py` is never imported at runtime unless explicitly opted in.
- **`enable_shadow_pruning = False` & `enable_laplace_pruning = False`**: Speculative unrun-test pruning caused real misses on rare-True branch conditions (`if source is None:`) and remains permanently off.
- **Import-time & unstable sites excluded**: Every profile build runs the full suite twice in plain single-process `pytest` order and drops any site or table whose recorded behavior differs across runs or executes during module import/collection (`746` import-time expr sites, `import_time_funcs` in `sqlglot/__init__.py` such as `parse_one`, and `1/644` unstable `const_profiles` site auto-dropped).
- **`sqlglot/typing/*` excluded from instrumentation & Phase 3 equivalence**: `test_typing.py` inspects `co_varnames` and bytecode/AST details directly.
- **Value-derived & mutable mapping tables excluded from Phase 2**: Only verified lookup tables across `Dialect`/`Tokenizer`/`Parser`/`Generator` are profiled; key-derived tries/sets (`KEYWORDS`, `NUMERIC_ESCAPES`, `SINGLE_TOKENS`, etc.) require `dict_values_only=True`, and `TIME_MAPPING` additionally requires zero `INVERSE_TIME_MAPPING`/`INVERSE_TIME_TRIE` access across the class, subclasses, and aliases (`Presto.TIME_MAPPING = MySQL.TIME_MAPPING`).
- **Missing/unprofiled/overflow fallback**: Any missing profile, unseen test, overflowed constant value set (>8 vals/test or >32 vals/site), cross-type coercion (`int`/`bool`/`float`/`IntEnum`), `@overload` duplicate, or patch/AST parse uncertainty conservatively keeps the test.

---

## 4. Current Repository Size

Well within the **15 MB** hard budget (and **20 MB** submission limit) and the **6 MB** artifact ceiling:

- **Artifacts**: `map.pkl.gz` = **`2.24 MB`** + `profiles.pkl.gz` = **`0.71 MB`** -> **`2.95 MB`** total (ceiling `6.00 MB`)
- **`.git` (`du -sh .git`)**: **`~7.23 MB`** (losslessly packed via `git gc --prune=never`; `146` objects in 1 pack, `0` pruned, `0` garbage)
- **Tracked working tree**: **`~3.47 MB`**
- **Total (`.git` + tracked)**: **`~10.71 MB`** (ceiling `15.00 MB`, submission limit `20.00 MB`)
- **Slim Docker image**: **`185 MB`**, no-cache build **`1.4 s`**, zero external network fetches

---

## 5. Unusual Things Discovered Overnight

1. **Observer effect & 37x slowdown in original `compare_probe` (Phase 1)**: Evaluating `lhs == rhs` inside the profiler hook re-entered instrumented `Expr.__eq__`/`__hash__` and `_Dialect.__eq__`, causing 7 spurious site mismatches and stretching `test_execute_tpcds` to `36 min` (`2,154 s`). Adding an `in_probe` re-entrancy guard + fast paths for `None`/reflexive/mismatched-`Expr` achieved **0 site mismatches** (bit-identical across 3,008 sites) and dropped `test_execute_tpcds` to **`58.4 s`** (**37x speedup**).
2. **`IfExp` vs `IfExp.body` 2-tuple AST span collision (Phase 1)**: `IfExp` and its `.body` share identical `(lineno, col_offset)` in Python's AST (`(106, 19)` in `sqlglot/tokens.py`), causing `anc_sites` to read the wrong reach bitset and miss `TestAthena::test_tokenizer_errors` in `gate.sh`. Switching all expr/reach/range/const keys to full 4-tuple spans `(lineno, col, end_lineno, end_col)` eliminated the entire class of collisions permanently.
3. **`sqlglot/__init__.py:parse_one()` executes at import time (Phase 1)**: Several dialect modules call `parse_one()` during module import; breaking it crashes collection for tests that never call `parse_one()` in their own body. Recording `import_time_funcs` during the stability build automatically protects all import-time functions in `sqlglot/__init__.py`.
4. **CPython `{**Base.TABLE}` unpacking + `TeradataParser` cross-key import read (Phase 2)**: `{**Base.TABLE}` and `dict(cls.TRANSFORMS)` invoke `keys()` + `__getitem__` on every key during `in_import > 0`. Separating import-time dict unpacking from test-time lookups and adding a static AST scanner automatically caught the single genuine cross-key import read (`sqlglot/parsers/teradata.py:197` reading `Parser.FUNCTION_PARSERS['TRY_CAST']`).
5. **Python 3.13 `inspect.getfullargspec` does not follow `__wrapped__` (Phase 4)**: Wrapping functions with `@functools.wraps` broke `sqlglot/optimizer/optimizer.py:111` (`test_optimize` & `test_execute_tpcds`). Attaching `_blast_orig` and transparently unwrapping inside `build_profiles.py` fixed it cleanly with `1,620/1,620` tests passing.
6. **Multi-bit omission mask bug caught pre-merge (Phase 4)**: `if add and not (cur & add):` silently skipped recording a newly omitted default parameter whenever a different default parameter's bit was already set in `cur`. Fixing to `if add & ~cur:` immediately eliminated all 62 misses on `signature_generator_pretty`, after which real `pytest` verified **0/780** wrong drops.
7. **Pytest output-parser pitfalls in ground-truth harnesses (Phases 1, 4, 5)**: Three subtle pytest 9 quirks surfaced during differential verification: (a) SQL literal `"ERROR ON ERROR"` inside `SUBPASSED` lines falsely triggered `" ERROR"` matches (explaining 9 stale labels in an earlier `synthetic_133_gt.json` cache); (b) substring matching matched `test_mysql` inside `test_mysql_time` and `test_canonicalize` inside `test_canonicalize_internal_names`; (c) `pytest` `RC=4` (`found no collectors`) aborts an entire multi-file chunk even under `--continue-on-collection-errors`. Fixing all three yielded exact ground truth with **0 misses across all 232 evaluated edits**.
