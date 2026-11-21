#!/usr/bin/env python3
"""Local evaluation and cross-validation script for the Blast-Radius selector.

Runs selector.py against sample/ (using score.py for exact scoring) and reports:
- Total simulated seconds, CPU seconds, misses, per-commit cost vs oracle
- Slowest selected tests driving excess cost per commit
- Optional leave-10-out (3-fold) cross-validation for cost thresholds
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import packer  # noqa: E402
import score  # noqa: E402
import selector as sel  # noqa: E402


def oracle_commit_cost_ms(failing: list[int], dur_ms: dict[int, int]) -> int:
    if not failing:
        return 0
    ws = packer.pack_tests(failing, dur_ms)
    return max(sum(dur_ms[n] for n in w) for w in ws) + 2000 * len(ws)


def evaluate_config(
    selector_obj: sel.BlastRadiusSelector,
    patches: dict[str, str],
    tests: list[dict[str, Any]],
    answers: dict[str, Any],
    verbose: bool = True,
) -> dict[str, Any]:
    dur_ms = {t["number"]: math.floor(t["seconds"] * 1000 + 0.5) for t in tests}
    id_by_num = {t["number"]: t["id"] for t in tests}

    t0 = time.process_time()
    results = [selector_obj.select_and_pack(cid, patches[cid]) for cid in sorted(patches)]
    cpu_sec = time.process_time() - t0

    stdout_bytes = (
        "\n".join(json.dumps(r, separators=(",", ":")) for r in results) + "\n"
    ).encode("utf-8")
    ev = score.evaluate_run(stdout_bytes, tests, answers)
    if not ev["ok"]:
        raise RuntimeError(f"Invalid output: {ev}")

    sim_ms = ev["simulated_milliseconds"]
    misses = ev["missed_commits"]
    total_oracle_ms = 0
    per_commit: list[dict[str, Any]] = []

    for r in results:
        cid = r["commit"]
        ws = r["workers"]
        selected = [n for w in ws for n in w]
        cost_ms = (
            max(sum(dur_ms[n] for n in w) for w in ws) + 2000 * len(ws)
            if ws
            else 0
        )
        failing = answers[cid]["failing"]
        ora_ms = oracle_commit_cost_ms(failing, dur_ms)
        total_oracle_ms += ora_ms
        missed = sorted(set(failing) - set(selected))
        top_sel = sorted(selected, key=lambda n: -dur_ms[n])[:3]
        per_commit.append(
            {
                "commit": cid,
                "kind": answers[cid].get("kind", "?"),
                "n_sel": len(selected),
                "k": len(ws),
                "cost_ms": cost_ms,
                "oracle_ms": ora_ms,
                "excess_ms": cost_ms - ora_ms,
                "n_fail": len(failing),
                "missed": missed,
                "top_sel": [(n, dur_ms[n] / 1000.0, id_by_num[n].split("::")[-1]) for n in top_sel],
            }
        )

    if verbose:
        print(
            f"Simulated: {sim_ms/1000:.3f} s  |  Oracle: {total_oracle_ms/1000:.3f} s  |  "
            f"Excess: {(sim_ms-total_oracle_ms)/1000:.3f} s  |  "
            f"Misses: {len(misses)} {misses}  |  CPU: {cpu_sec:.3f} s"
        )
        print(
            f"40-commit scaled est: {sim_ms/1000 * (40/len(patches)):.2f} s "
            f"(target < 142.9 s)"
        )
        print("-" * 105)
        for row in sorted(per_commit, key=lambda x: -x["excess_ms"]):
            top_str = ", ".join(f"{name}({sec:.2f}s)" for _, sec, name in row["top_sel"])
            miss_tag = f" MISS({len(row['missed'])})" if row["missed"] else ""
            print(
                f"  {row['commit']} [{row['kind']:7s}] sel={row['n_sel']:4d} k={row['k']} "
                f"cost={row['cost_ms']/1000:6.3f}s ora={row['oracle_ms']/1000:6.3f}s "
                f"+{row['excess_ms']/1000:6.3f}s | {top_str}{miss_tag}"
            )

    return {
        "sim_ms": sim_ms,
        "oracle_ms": total_oracle_ms,
        "misses": misses,
        "cpu_sec": cpu_sec,
        "per_commit": per_commit,
    }


def run_leave_out_cv(
    map_data: dict[str, Any],
    patches: dict[str, str],
    tests: list[dict[str, Any]],
    answers: dict[str, Any],
) -> None:
    """3-fold (tune on 20, validate on 10) cross-validation over cost/probability thresholds."""
    cids = sorted(patches.keys())
    folds = [cids[0:10], cids[10:20], cids[20:30]]

    # Small principled grid over key FR4 knobs
    grid: list[tuple[str, dict[str, float]]] = []
    for p_ef in (0.0, 0.002, 0.01):
        for p_daf in (0.0, 0.002, 0.01):
            for p_ast in (0.0, 0.001):
                for k_div in (1.0, 2.0):
                    cfg = {
                        "p_enclosing_func": p_ef,
                        "p_dialect_attr_fallback": p_daf,
                        "p_ast_identical": p_ast,
                        "fr4_k_divisor": k_div,
                    }
                    label = f"ef={p_ef},daf={p_daf},ast={p_ast},k={k_div}"
                    grid.append((label, cfg))

    # Precompute per-commit cost and miss flag for every candidate config once
    dur_ms = {t["number"]: math.floor(t["seconds"] * 1000 + 0.5) for t in tests}
    per_cfg_scores: list[tuple[str, dict[str, float], dict[str, tuple[int, bool]]]] = []
    for label, cfg in grid:
        sel_obj = sel.BlastRadiusSelector(map_data, tests, config=cfg)
        c_scores: dict[str, tuple[int, bool]] = {}
        for cid in cids:
            r = sel_obj.select_and_pack(cid, patches[cid])
            ws = r["workers"]
            selected = {n for w in ws for n in w}
            cost_ms = (
                max(sum(dur_ms[n] for n in w) for w in ws) + 2000 * len(ws)
                if ws
                else 0
            )
            missed = any(f not in selected for f in answers[cid]["failing"])
            if missed:
                cost_ms += 300_000
            c_scores[cid] = (cost_ms, missed)
        per_cfg_scores.append((label, cfg, c_scores))

    print("\n=== 3-Fold (20 train / 10 val) Cross-Validation ===")
    total_val_ms = 0
    total_val_misses = 0
    for fi, val_cids in enumerate(folds):
        train_cids = [c for c in cids if c not in val_cids]
        # Pick best config on train_cids (requiring 0 misses on train first)
        best = min(
            per_cfg_scores,
            key=lambda item: (
                sum(1 for c in train_cids if item[2][c][1]),
                sum(item[2][c][0] for c in train_cids),
            ),
        )
        label, _, c_scores = best
        train_ms = sum(c_scores[c][0] for c in train_cids)
        train_miss = sum(1 for c in train_cids if c_scores[c][1])
        val_ms = sum(c_scores[c][0] for c in val_cids)
        val_miss = sum(1 for c in val_cids if c_scores[c][1])
        total_val_ms += val_ms
        total_val_misses += val_miss
        print(
            f"  Fold {fi+1}: best_train=[{label}]  "
            f"train={train_ms/1000:.3f}s (miss={train_miss}, avg={train_ms/20000:.3f}s/c)  "
            f"val={val_ms/1000:.3f}s (miss={val_miss}, avg={val_ms/10000:.3f}s/c)"
        )
    print(
        f"  CV Aggregate (out-of-fold 30 commits): {total_val_ms/1000:.3f} s, "
        f"misses={total_val_misses}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate Blast-Radius selector on sample set.")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "sample")
    parser.add_argument("--answers", type=Path, default=ROOT / "sample" / "answers.json")
    parser.add_argument("--map", type=Path, default=ROOT / "map.pkl.gz")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--cv", action="store_true", help="Run 3-fold (20/10) cross-validation")
    args = parser.parse_args()

    t0 = time.process_time()
    map_data = sel.load_map(args.map)
    load_cpu = time.process_time() - t0

    tests = json.loads((args.data_dir / "tests.json").read_text("utf-8"))["tests"]
    answers = json.loads(args.answers.read_text("utf-8"))["commits"]
    patches = {
        p.name.removesuffix(".patch"): p.read_text("utf-8", errors="replace")
        for p in sorted((args.data_dir / "commits").glob("*.patch"))
    }

    selector_obj = sel.BlastRadiusSelector(map_data, tests)
    res = evaluate_config(selector_obj, patches, tests, answers, verbose=not args.quiet)
    print(f"\nMap load CPU: {load_cpu:.3f} s  |  Total CPU (load + select): {load_cpu + res['cpu_sec']:.3f} s")

    if args.cv:
        run_leave_out_cv(map_data, patches, tests, answers)


if __name__ == "__main__":
    main()
