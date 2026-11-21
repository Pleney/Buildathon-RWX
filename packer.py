"""Optimal worker packer for Blast-Radius test selection.

Chooses worker count k in 1..8 minimizing exact integer millisecond cost:
    max(worker_load_ms) + 2000 * k

Uses Longest-Processing-Time-first (LPT) initialization followed by
move and 1-1 swap local search between the busiest worker and lighter workers.
Standard library only.
"""

from __future__ import annotations

WORKER_COST_MS = 2_000
MAX_WORKERS = 8


def _lpt_and_local_search(
    ordered: list[tuple[int, int]],
    k: int,
    max_iters: int = 64,
) -> tuple[int, list[list[int]]]:
    """Pack `(duration_ms, test_num)` pairs onto `k` workers using LPT + move/swap local search."""
    workers: list[list[tuple[int, int]]] = [[] for _ in range(k)]
    loads = [0] * k

    # 1. LPT initialization
    for item in ordered:
        # Find lightest worker
        lightest = 0
        min_load = loads[0]
        for j in range(1, k):
            if loads[j] < min_load:
                min_load = loads[j]
                lightest = j
        workers[lightest].append(item)
        loads[lightest] += item[0]

    if k == 1:
        return loads[0] + WORKER_COST_MS, [[n for _, n in workers[0]]]

    # 2. Local search: reduce busiest worker load via moves and 1-1 swaps
    for _ in range(max_iters):
        # Identify busiest worker
        busiest = 0
        max_load = loads[0]
        for j in range(1, k):
            if loads[j] > max_load:
                max_load = loads[j]
                busiest = j

        b_items = workers[busiest]
        improved = False

        # 2a. Try best single-item move from busiest worker to any lighter worker
        best_move: tuple[int, int, int] | None = None  # (new_pair_max, b_idx, target_w)
        for bi, (d, _) in enumerate(b_items):
            if d == 0:
                continue
            rem_b = max_load - d
            for j in range(k):
                if j == busiest:
                    continue
                new_j = loads[j] + d
                if new_j < max_load:
                    pair_max = rem_b if rem_b >= new_j else new_j
                    if best_move is None or pair_max < best_move[0]:
                        best_move = (pair_max, bi, j)

        if best_move is not None:
            _, bi, j = best_move
            item = b_items.pop(bi)
            workers[j].append(item)
            loads[busiest] -= item[0]
            loads[j] += item[0]
            improved = True
            continue

        # 2b. Try best 1-1 swap between busiest worker and a lighter worker
        best_swap: tuple[int, int, int, int] | None = None  # (new_pair_max, bi, j, ji)
        # Only inspect top/unique durations if worker is huge to guarantee <1ms runtime
        b_scan = range(len(b_items)) if len(b_items) <= 120 else range(min(len(b_items), 120))
        for bi in b_scan:
            da = b_items[bi][0]
            if da == 0:
                continue
            for j in range(k):
                if j == busiest:
                    continue
                slack = max_load - loads[j]
                if slack <= 1:
                    continue
                j_items = workers[j]
                j_scan = range(len(j_items)) if len(j_items) <= 120 else range(min(len(j_items), 120))
                for ji in j_scan:
                    dc = j_items[ji][0]
                    diff = da - dc
                    if 0 < diff < slack:
                        new_b = max_load - diff
                        new_j = loads[j] + diff
                        pair_max = new_b if new_b >= new_j else new_j
                        if best_swap is None or pair_max < best_swap[0]:
                            best_swap = (pair_max, bi, j, ji)

        if best_swap is not None:
            _, bi, j, ji = best_swap
            item_a = b_items[bi]
            item_c = workers[j][ji]
            b_items[bi] = item_c
            workers[j][ji] = item_a
            diff = item_a[0] - item_c[0]
            loads[busiest] -= diff
            loads[j] += diff
            improved = True

        if not improved:
            break

    nonempty = [[n for _, n in w] for w in workers if w]
    actual_k = len(nonempty)
    actual_max = max(loads[i] for i, w in enumerate(workers) if w) if actual_k else 0
    return actual_max + WORKER_COST_MS * actual_k, nonempty


def pack_tests(
    test_nums: list[int],
    dur_ms: dict[int, int],
    max_workers: int = MAX_WORKERS,
) -> list[list[int]]:
    """Pack `test_nums` onto `k` in 1..max_workers minimizing `max_load + 2000*k`."""
    if not test_nums:
        return []

    ordered = sorted(((dur_ms[n], n) for n in test_nums), key=lambda x: (-x[0], x[1]))
    total_ms = sum(d for d, _ in ordered)
    max_single_ms = ordered[0][0]

    # k = 1 baseline
    best_cost = total_ms + WORKER_COST_MS
    best_workers: list[list[int]] = [[n for _, n in ordered]]

    limit_k = min(max_workers, len(ordered))
    for k in range(2, limit_k + 1):
        # Exact theoretical lower bound for worker count k
        ceil_avg = (total_ms + k - 1) // k
        lb = (max_single_ms if max_single_ms >= ceil_avg else ceil_avg) + WORKER_COST_MS * k
        if lb >= best_cost:
            continue
        cost, workers = _lpt_and_local_search(ordered, k)
        if cost < best_cost:
            best_cost = cost
            best_workers = workers

    return best_workers
