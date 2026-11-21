# Overnight Progress Log (Asia/Singapore Time)

- **2026-11-22 03:28 SGT** — Started unattended overnight run (feature freeze: **10:30 SGT**). Created branch `work` from `main` (`2a3d3cbec3ca15d7df268b30ff6ed7b509c1a50b`).
- **2026-11-22 03:31 SGT** — Safety infrastructure built:
  - Split `map.pkl.gz` (`2.24 MB`, frozen static+coverage) and `profiles.pkl.gz` (`0.21 MB`, mutable profile tables) so only `profiles.pkl.gz` changes across phases.
  - Added per-phase toggle env vars (`BLAST_P1_REACH`, `BLAST_P1_MULTISITE`, `BLAST_P1_ROOT_INIT`, `BLAST_P2_MAPPINGS`, `BLAST_P3_EQUIV`, `BLAST_P4_RANGES`, `BLAST_VERIFIER_MODE=off` default).
  - Updated `.gitignore` and `.dockerignore` for caches/logs/JSON reports/gate scratch.
  - Added `--quick` (~33 edits) and deterministic 20% `--split holdout|dev` to `diff_test.py` with hard 300 s process-group timeout.
  - Created `gate.sh` + `gate_runner.py`, `CANDIDATES.md`, `LATEST_SAFE.txt`, `PROGRESS.md`.
- **Phase 0 (no tests)** — Already inspected all 8 covered-but-passing sample commits (`sample_01`, `03`, `06`, `08`, `09`, `22`, `23`, `26`):
  - **Finding**: All 8 are single-line generated mutations (`is->==`, `<->=`, `16->17`, `False->True`, `==->!=`).
  - **3 of the 5 remaining passing sample commits (`sample_06` `NUMERIC_ESCAPES`, `sample_23` `SHOW_PARSERS`, `sample_26` `TriggerProperties.arg_types` = `10.16 s` = 69% of remaining sample gap)** are single-key mutations inside class-level dicts where no test looked up that key!
  - **Phase ordering decision (evidence-backed)**: Keep Phase 1 (sub-expression reachability + multi-site conjunction + `__init__.py` function-body scoping + optimized/stability-verified single-process profiling) FIRST so the fast, stability-checked single-process profiler foundation is rock-solid; immediately follow with Phase 2 (mapping-key lookup profiles, extended to BOTH `Dialect/Tokenizer/Parser/Generator` dicts AND `Expression.arg_types`!), then Phase 3 (commit-level equivalence), then Phase 4 (numeric ranges / constant sets / defaults), Phase 5 (broader evaluation + hold-out), Phase 6 (hygiene + morning report).
- **Next step**: Verify `gate.sh` passes on baseline Safety Infrastructure (ETA ~2 min), then begin Phase 1.
