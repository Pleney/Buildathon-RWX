# Candidate Submissions Log

| Commit Hash | Enabled Features | Real Sample Sim / CPU / Misses | Broader-Set Gap (dev / holdout) | Diff-Test Result | Repo Size (`.git` + tracked) | Risk | One-Line Description |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `2a3d3cbec3ca15d7df268b30ff6ed7b509c1a50b` | Static + dict-values + P1–P6 expr profiles (`verifier=off`) | `109.635 s` / `1.241 s` / `0` | `294.271 s` (162-edit baseline; practice `200.8 s`, 0 misses) | `0 / 133` wrong drops (`1,023` tests) | `9.99 MB` (map `2.43 MB`) | Low | Baseline safe submission on `main` prior to overnight phases |
| `5a84dfc3537f6674bd5ecc55c85c4a93a27348eb` | Phase 1: + sub-expr reachability, multi-site conjunction, `__init__` func scoping, 4-tuple spans, 2x stability (`verifier=off`) | `109.635 s` / `1.165 s` / `0` | `184.348 s` (dev `142.040 s`, holdout `27.024 s`, `-107.848 s` vs baseline, `-4.092 s` on holdout) | `0 / 41` quick (`3,806` tests) | `12.85 MB` (`map 2.24` + `prof 0.35 = 2.58 MB`) | Low | CHECKPOINT 1: Proposal 1 passed 6/6 gate, 0 unstable sites across 13,828 sites |
