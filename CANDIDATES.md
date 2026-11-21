# Candidate Submissions Log

| Commit Hash | Enabled Features | Real Sample Sim / CPU / Misses | Broader-Set Gap (dev / holdout) | Diff-Test Result | Repo Size (`.git` + tracked) | Risk | One-Line Description |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `2a3d3cbec3ca15d7df268b30ff6ed7b509c1a50b` | Static + dict-values + P1–P6 expr profiles (`verifier=off`) | `109.635 s` / `1.241 s` / `0` | `294.271 s` (162-edit baseline; practice `200.8 s`, 0 misses) | `0 / 133` wrong drops (`1,023` tests) | `9.99 MB` (map `2.43 MB`) | Low | Baseline safe submission on `main` prior to overnight phases |
