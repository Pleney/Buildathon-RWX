#!/usr/bin/env python3
"""Economic report for the Hybrid v2 verifier (measurement only, no behaviour change).

Prints per-commit:
- static selection size & packed cost (s)
- verified selection size & packed cost (s)
- verifier CPU spent (s, user + sys including forked child)
- simulated saving achieved by verifier (s)
- net gain = saving - verifier CPU (s)

Designed to run inside the scorer container under exact limits (--cpus 2,
--read-only, --user 65534:65534, --network none).
"""

from __future__ import annotations

import argparse
import json
import math
import resource
import sys
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
if "/app" not in sys.path and Path("/app/selector.py").is_file():
    sys.path.insert(0, "/app")

import packer  # noqa: E402
import selector as sel  # noqa: E402


def _cpu_sec() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF)
    rc = resource.getrusage(resource.RUSAGE_CHILDREN)
    return r.ru_utime + r.ru_stime + rc.ru_utime + rc.ru_stime


def _packed_cost_ms(test_nums: list[int], dur_ms: dict[int, int]) -> int:
    if not test_nums:
        return 0
    ws = packer.pack_tests(test_nums, dur_ms)
    return max(sum(dur_ms[n] for n in w) for w in ws) + 2000 * len(ws)


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure per-commit verifier economics.")
    parser.add_argument("--data-dir", type=Path, default=Path("/data"))
    parser.add_argument("--map", type=Path, default=Path("/app/map.pkl.gz"))
    args = parser.parse_args()

    map_path = args.map if args.map.is_file() else (APP_DIR / "map.pkl.gz")
    t_init0 = _cpu_sec()
    map_data = sel.load_map(map_path)
    tests = json.loads((args.data_dir / "tests.json").read_text("utf-8"))["tests"]
    dur_ms = {t["number"]: math.floor(t["seconds"] * 1000 + 0.5) for t in tests}

    s = sel.BlastRadiusSelector(map_data, tests)
    init_cpu = _cpu_sec() - t_init0

    # Instrument verify_candidates without altering its behavior
    last_stat: dict[str, Any] = {}
    if s.verifier is not None:
        orig_verify = s.verifier.verify_candidates

        def wrapped_verify(cands, ppy, pnon, is_fixture_only=False):
            static_nums = [n for n, _, _ in cands]
            c0 = _cpu_sec()
            out = orig_verify(cands, ppy, pnon, is_fixture_only=is_fixture_only)
            vcpu = _cpu_sec() - c0
            last_stat["called"] = True
            last_stat["static_nums"] = static_nums
            last_stat["final_nums"] = out
            last_stat["vcpu"] = vcpu
            last_stat["is_fixture_only"] = is_fixture_only
            return out

        s.verifier.verify_candidates = wrapped_verify

    rows: list[dict[str, Any]] = []
    t_batch0 = _cpu_sec()
    for p in sorted((args.data_dir / "commits").glob("*.patch")):
        cid = p.name.removesuffix(".patch")
        patch_text = p.read_text("utf-8", errors="replace")
        last_stat.clear()
        c0 = _cpu_sec()
        final_nums = s.select_test_numbers(patch_text)
        total_c_cpu = _cpu_sec() - c0

        if last_stat.get("called"):
            static_nums = last_stat["static_nums"]
            vcpu = last_stat["vcpu"]
            is_fix = last_stat["is_fixture_only"]
        else:
            static_nums = final_nums
            vcpu = 0.0
            is_fix = False

        static_ms = _packed_cost_ms(static_nums, dur_ms)
        final_ms = _packed_cost_ms(final_nums, dur_ms)
        saving_s = (static_ms - final_ms) / 1000.0
        net_s = saving_s - vcpu
        rows.append(
            {
                "commit": cid,
                "n_static": len(static_nums),
                "n_final": len(final_nums),
                "static_s": static_ms / 1000.0,
                "final_s": final_ms / 1000.0,
                "vcpu_s": vcpu,
                "sel_cpu_s": total_c_cpu - vcpu,
                "saving_s": saving_s,
                "net_s": net_s,
                "is_fix": is_fix,
            }
        )
    batch_cpu = _cpu_sec() - t_batch0

    print(
        f"{'commit':8s} {'n_st':>4s} {'n_fn':>4s} {'static_s':>9s} {'final_s':>9s} "
        f"{'ver_cpu':>8s} {'saving':>8s} {'net_gain':>9s}  notes"
    )
    print("-" * 82)
    for r in sorted(rows, key=lambda x: x["net_s"]):
        tag = ""
        if r["vcpu_s"] > 0 and r["net_s"] < 0:
            tag = "LOSES MONEY"
        elif r["vcpu_s"] > 0 and r["net_s"] > 0:
            tag = "PROFIT"
        elif r["is_fix"]:
            tag = "fixture-skip"
        print(
            f"{r['commit']:8s} {r['n_static']:4d} {r['n_final']:4d} "
            f"{r['static_s']:9.3f} {r['final_s']:9.3f} "
            f"{r['vcpu_s']:8.3f} {r['saving_s']:+8.3f} {r['net_s']:+9.3f}  {tag}"
        )

    tot_static = sum(r["static_s"] for r in rows)
    tot_final = sum(r["final_s"] for r in rows)
    tot_vcpu = sum(r["vcpu_s"] for r in rows)
    tot_saving = tot_static - tot_final
    tot_net = tot_saving - tot_vcpu
    losers = [r for r in rows if r["vcpu_s"] > 0 and r["net_s"] < 0]
    winners = [r for r in rows if r["vcpu_s"] > 0 and r["net_s"] >= 0]

    print("-" * 82)
    print(f"Init CPU:           {init_cpu:.3f} s  |  Batch CPU: {batch_cpu:.3f} s  |  Grand CPU: {init_cpu + batch_cpu:.3f} s")
    print(f"Total static cost:  {tot_static:.3f} s")
    print(f"Total final cost:   {tot_final:.3f} s")
    print(f"Total verifier CPU: {tot_vcpu:.3f} s  (across {len(winners) + len(losers)} verified commits)")
    print(f"Total saving:       {tot_saving:+.3f} s")
    print(f"Total net gain:     {tot_net:+.3f} s")
    print(f"Money-losing commits ({len(losers)}): sum net = {sum(r['net_s'] for r in losers):+.3f} s (CPU burned = {sum(r['vcpu_s'] for r in losers):.3f} s)")
    print(f"Profitable commits   ({len(winners)}): sum net = {sum(r['net_s'] for r in winners):+.3f} s")


if __name__ == "__main__":
    main()
