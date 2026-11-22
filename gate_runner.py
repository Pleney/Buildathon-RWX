#!/usr/bin/env python3
"""Automated release gate invoked by gate.sh.

Requires ALL of:
  1. No-cache Docker build < 360 s (6 min)
  2. Exact scorer run on sample/ (3 runs): 0 misses, median CPU < 2.0 s,
     3 byte-identical outputs, verifier_mode == off (verifier not imported)
  3. Robustness suite (python -S -E tests/test_robustness.py) passes
  4. Quick differential test (diff_test.py --quick): 0 wrong drops on real pytest
  5. 1.5x commit batch (45 commits) under --cpus 2 + exact scorer flags:
     wall time < 60 s and 3 byte-identical outputs
  6. Size & hygiene: map.pkl.gz + profiles.pkl.gz <= 6 MB; .git + tracked <= 15 MB;
     0 sample commit ID leaks in tracked files.

Prints PASS or FAIL and a one-line summary. Exits 0 on PASS, 1 on FAIL.
"""

from __future__ import annotations

import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent
SAMPLE_DIR = REPO / "sample"
IMAGE = "blast-radius-entry"

SCORER_FLAGS = [
    "--platform", "linux/amd64",
    "--network", "none",
    "--cpus", "2",
    "--memory", "4096m",
    "--memory-swap", "4096m",
    "--pids-limit", "256",
    "--read-only",
    "--tmpfs", "/tmp:rw,size=64m,mode=1777",
    "--cap-drop", "ALL",
    "--security-opt", "no-new-privileges",
    "--user", "65534:65534",
    "--env", "HOME=/tmp",
]

MEASURE_PY = (
    "import resource, subprocess, sys\n"
    "p = subprocess.run(['python', '-S', '-E', '/app/selector.py'], stdout=sys.stdout, stderr=sys.stderr)\n"
    "ru = resource.getrusage(resource.RUSAGE_CHILDREN)\n"
    "sys.stderr.write(f'__CPU__={ru.ru_utime + ru.ru_stime:.4f}\\n')\n"
    "sys.exit(p.returncode)\n"
)


def _run(cmd: list[str], cwd: Path = REPO, timeout: float | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, timeout=timeout)


def main() -> int:
    t_all = time.monotonic()
    failures: list[str] = []

    # ------------------------------------------------------------------
    # 1. No-cache Docker build (< 360 s)
    # ------------------------------------------------------------------
    print("[gate 1/6] Running no-cache Docker build (limit 360s)...", flush=True)
    t0 = time.monotonic()
    p_bld = _run(["docker", "build", "--no-cache", "--platform", "linux/amd64", "-t", IMAGE, "."], timeout=420)
    build_sec = time.monotonic() - t0
    if p_bld.returncode != 0:
        failures.append(f"docker_build_failed(rc={p_bld.returncode})")
        print(p_bld.stderr.decode("utf-8", "replace")[-1000:], flush=True)
        print(f"FAIL | build failed ({build_sec:.1f}s)")
        return 1
    if build_sec >= 360.0:
        failures.append(f"build_slow({build_sec:.1f}s>=360s)")
    print(f"  OK: build={build_sec:.1f}s", flush=True)

    # ------------------------------------------------------------------
    # 2. Sample run (exact scorer flags, 3 runs: 0 misses, CPU < 2.0s, deterministic)
    # ------------------------------------------------------------------
    print("[gate 2/6] Running sample x3 under exact scorer flags...", flush=True)
    # First check verifier is OFF & not imported
    check_off = (
        "import sys, json; sys.path.insert(0, '/app'); import selector; from pathlib import Path; "
        "m = selector.load_map(Path('/app/map.pkl.gz')); "
        "ts = json.loads(Path('/data/tests.json').read_text())['tests']; "
        "s = selector.BlastRadiusSelector(m, ts); "
        "assert s.verifier_mode == 'off', s.verifier_mode; "
        "assert 'verifier' not in sys.modules, 'verifier imported!'; "
        "print('verifier_off_ok')"
    )
    p_off = _run(["docker", "run", "--rm", *SCORER_FLAGS, "-v", f"{SAMPLE_DIR}:/data:ro", IMAGE, "python", "-E", "-c", check_off])
    if p_off.returncode != 0 or b"verifier_off_ok" not in p_off.stdout:
        failures.append("verifier_not_off_default")
        print("  FAIL: verifier default check:", p_off.stderr.decode("utf-8", "replace")[-400:], flush=True)

    cpus: list[float] = []
    outs: list[bytes] = []
    for r in range(3):
        p = _run([
            "docker", "run", "--rm", *SCORER_FLAGS,
            "-v", f"{SAMPLE_DIR}:/data:ro",
            IMAGE, "python", "-c", MEASURE_PY,
        ], timeout=120)
        if p.returncode != 0:
            failures.append(f"sample_run_{r+1}_rc={p.returncode}")
            print(p.stderr.decode("utf-8", "replace")[-500:], flush=True)
            break
        err_txt = p.stderr.decode("utf-8", "replace")
        m_cpu = re.search(r"__CPU__=([0-9.]+)", err_txt)
        cpus.append(float(m_cpu.group(1)) if m_cpu else 999.0)
        outs.append(p.stdout)

    sim_sec = 9999.0
    misses = 999
    med_cpu = statistics.median(cpus) if cpus else 999.0
    if len(outs) == 3:
        if not (outs[0] == outs[1] == outs[2]):
            failures.append("sample_nondeterministic")
        out_path = REPO / "output.jsonl"
        out_path.write_bytes(outs[0])
        p_sc = _run([sys.executable, str(REPO / "score.py"), str(SAMPLE_DIR), str(SAMPLE_DIR / "answers.json"), str(out_path), "--cpu-seconds", str(med_cpu)])
        sc_txt = p_sc.stdout.decode("utf-8", "replace")
        m_sim = re.search(r"Simulated:\s+([0-9.]+)\s+s", sc_txt)
        m_mis = re.search(r"Misses:\s+(\d+)", sc_txt)
        sim_sec = float(m_sim.group(1)) if m_sim else 9999.0
        misses = int(m_mis.group(1)) if m_mis else 999
        if p_sc.returncode != 0 or misses != 0:
            failures.append(f"sample_misses={misses}")
        if med_cpu >= 2.0:
            failures.append(f"cpu_high({med_cpu:.3f}s>=2.0s)")
    print(f"  sim={sim_sec:.3f}s med_cpu={med_cpu:.3f}s ({[round(c,3) for c in cpus]}) misses={misses}", flush=True)

    # ------------------------------------------------------------------
    # 3. Robustness suite (python -S -E)
    # ------------------------------------------------------------------
    print("[gate 3/6] Running robustness suite (python -S -E)...", flush=True)
    p_rob = _run([sys.executable, "-S", "-E", str(REPO / "tests/test_robustness.py")], timeout=60)
    if p_rob.returncode != 0:
        failures.append("robustness_failed")
        print(p_rob.stderr.decode("utf-8", "replace")[-500:], flush=True)
    else:
        print("  OK: robustness passed", flush=True)

    # ------------------------------------------------------------------
    # 4. Quick differential test (0 wrong drops on real pytest)
    # ------------------------------------------------------------------
    print("[gate 4/6] Running quick differential test (diff_test.py --quick)...", flush=True)
    t_df = time.monotonic()
    # Check if IMAGE contains /sqlglot & pytest; otherwise use blast-radius-mapgen + mounted code
    has_sg = _run(["docker", "run", "--rm", "--platform", "linux/amd64", IMAGE, "python", "-c", "import pathlib, pytest; assert pathlib.Path('/sqlglot/sqlglot').is_dir()"]).returncode == 0
    if has_sg:
        df_cmd = [
            "docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none",
            "--cpus", "2", "--memory", "4096m", "--tmpfs", "/tmp:rw,size=512m,mode=1777",
            "-v", f"{SAMPLE_DIR}:/data:ro",
            "-v", f"{REPO / 'diff_test.py'}:/work/diff_test.py:ro",
            IMAGE, "python", "-u", "/work/diff_test.py", "--quick",
        ]
    else:
        df_cmd = [
            "docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none",
            "--cpus", "2", "--memory", "4096m", "--tmpfs", "/tmp:rw,size=512m,mode=1777",
            "-v", f"{SAMPLE_DIR}:/data:ro",
            "-v", f"{REPO}:/app:ro",
            "-v", f"{REPO / 'build_tmp/sqlglot'}:/sqlglot:ro",
            "blast-radius-mapgen", "python", "-u", "/app/diff_test.py", "--quick",
        ]
    p_df = _run(df_cmd, timeout=1200)
    df_sec = time.monotonic() - t_df
    df_out = p_df.stdout.decode("utf-8", "replace")
    m_wd = re.search(r"TOTAL WRONG DROPS:\s*(\d+)", df_out)
    wrong_drops = int(m_wd.group(1)) if m_wd else -1
    if p_df.returncode != 0 or wrong_drops != 0:
        failures.append(f"diff_quick_wrong_drops={wrong_drops}(rc={p_df.returncode})")
        print(df_out[-1200:], flush=True)
        print(p_df.stderr.decode("utf-8", "replace")[-500:], flush=True)
    else:
        last_line = [ln for ln in df_out.splitlines() if "TOTAL" in ln][-1]
        print(f"  OK ({df_sec:.1f}s): {last_line}", flush=True)

    # ------------------------------------------------------------------
    # 5. 1.5x commit batch (45 commits) under --cpus 2: wall < 60s, 3 byte-identical
    # ------------------------------------------------------------------
    print("[gate 5/6] Running 1.5x batch (45 commits) x3 under --cpus 2...", flush=True)
    bdir = REPO / "gate_tmp" / "batch15x"
    if bdir.exists():
        shutil.rmtree(bdir)
    (bdir / "commits").mkdir(parents=True)
    shutil.copy2(SAMPLE_DIR / "tests.json", bdir / "tests.json")
    sample_patches = sorted((SAMPLE_DIR / "commits").glob("*.patch"))
    for p in sample_patches:
        shutil.copy2(p, bdir / "commits" / p.name)
    for idx_dup, p in enumerate(sample_patches[:15], 1):
        shutil.copy2(p, bdir / "commits" / f"dup{idx_dup:02d}_{p.name}")

    b_walls: list[float] = []
    b_outs: list[bytes] = []
    for r in range(3):
        tb = time.monotonic()
        p_b = _run(["docker", "run", "--rm", *SCORER_FLAGS, "-v", f"{bdir}:/data:ro", IMAGE], timeout=90)
        bw = time.monotonic() - tb
        if p_b.returncode != 0:
            failures.append(f"batch15x_rc={p_b.returncode}")
            break
        b_walls.append(bw)
        b_outs.append(p_b.stdout)
    shutil.rmtree(bdir, ignore_errors=True)
    max_b_wall = max(b_walls) if b_walls else 999.0
    if len(b_outs) == 3:
        if not (b_outs[0] == b_outs[1] == b_outs[2]):
            failures.append("batch15x_nondeterministic")
        if len(b_outs[0].splitlines()) != 45:
            failures.append(f"batch15x_lines={len(b_outs[0].splitlines())}")
        if max_b_wall >= 60.0:
            failures.append(f"batch15x_slow({max_b_wall:.1f}s>=60s)")
    print(f"  walls={[round(w,2) for w in b_walls]}s deterministic={len(b_outs)==3 and b_outs[0]==b_outs[1]==b_outs[2]}", flush=True)

    # ------------------------------------------------------------------
    # 6. Repo size <= 15 MB, map+profiles <= 6 MB, 0 sample commit ID leaks
    # ------------------------------------------------------------------
    print("[gate 6/6] Checking repo size (<=15MB), map+profiles (<=6MB), and zero leaks...", flush=True)
    map_b = (REPO / "map.pkl.gz").stat().st_size if (REPO / "map.pkl.gz").is_file() else 0
    prof_b = (REPO / "profiles.pkl.gz").stat().st_size if (REPO / "profiles.pkl.gz").is_file() else 0
    art_mb = (map_b + prof_b) / 1e6
    if art_mb > 6.0:
        failures.append(f"artifacts_too_large({art_mb:.2f}MB>6MB)")

    git_files = subprocess.check_output(["git", "-C", str(REPO), "ls-files"], text=True).splitlines()
    # Include profiles.pkl.gz if not yet staged
    tracked_set = set(git_files)
    if (REPO / "profiles.pkl.gz").is_file():
        tracked_set.add("profiles.pkl.gz")
    tracked_b = sum((REPO / f).stat().st_size for f in tracked_set if (REPO / f).is_file())
    git_b = sum(p.stat().st_size for p in (REPO / ".git").rglob("*") if p.is_file())
    repo_mb = (tracked_b + git_b) / 1e6
    if repo_mb > 15.0:
        failures.append(f"repo_too_large({repo_mb:.2f}MB>15MB)")

    co_txt = subprocess.check_output(["git", "-C", str(REPO), "count-objects", "-vH"], text=True).strip()
    print(f"  git count-objects: {' | '.join(co_txt.splitlines()[:4])}", flush=True)
    print(f"  map={map_b/1e6:.2f}MB profiles={prof_b/1e6:.2f}MB sum={art_mb:.2f}MB | .git={git_b/1e6:.2f}MB tracked={tracked_b/1e6:.2f}MB total={repo_mb:.2f}MB", flush=True)

    sample_ids = [p.stem for p in sample_patches]
    leaks: list[tuple[str, str]] = []
    for f in tracked_set:
        if f.endswith(".pkl.gz"):
            continue
        fp = REPO / f
        if not fp.is_file():
            continue
        txt = fp.read_text("utf-8", errors="ignore")
        for sid in sample_ids:
            if sid in txt:
                leaks.append((f, sid))
    if leaks:
        failures.append(f"sample_id_leaks={len(leaks)}({leaks[0][0]})")

    total_wall = time.monotonic() - t_all
    if failures:
        print(f"FAIL | {'; '.join(failures)} (gate_wall={total_wall:.1f}s)", flush=True)
        return 1
    print(
        f"PASS | sim={sim_sec:.3f}s cpu={med_cpu:.3f}s total={sim_sec+med_cpu:.3f}s misses={misses} "
        f"diff_wrong={wrong_drops} build={build_sec:.1f}s batch15x={max_b_wall:.1f}s "
        f"art={art_mb:.2f}MB repo={repo_mb:.2f}MB (gate_wall={total_wall:.1f}s)",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
