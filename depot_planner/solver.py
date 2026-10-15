# -*- coding: utf-8 -*-
"""補給地点の選定ソルバー(p-center / p-median)。

前提(1 目的地 1 台):
    各目的地に車両を 1 台ずつ割り当てる。車両の動きは
    「補給地点で積込 → 担当の目的地へ移動 → 作業」だけなので、
        cost[i][j] = 積込 + 移動(補給地点 i → 目的地 j) + 作業
    で車両 1 台の所要時間が決まる。拠点あたりの台数上限は設けないため、
    選んだ拠点集合に対しては「各目的地を最寄り拠点へ」が常に最適な割当になる。

目的関数:
    makespan … 最も遅い車両の所要時間を最小化(同点なら総所要時間)  = p-center
    total    … 総所要時間を最小化(同点なら最も遅い車両)            = p-median

探索:
    組合せ数が max_exact 以下なら numpy で全組合せを総当たり(厳密解)。
    超える場合は 貪欲法 + 入替え局所探索(近似解)。
"""

import math
from itertools import combinations, islice

import numpy as np

CHUNK = 20000
OBJECTIVES = ("makespan", "total")


class Cancelled(Exception):
    pass


def _score(times, objective):
    """times: (m, n_targets) → 並べ替えキー (主, 副) の配列。"""
    mk, tot = times.max(axis=1), times.sum(axis=1)
    return (mk, tot) if objective == "makespan" else (tot, mk)


def evaluate(cost, subset):
    """subset(拠点 index の列)の (makespan, total, owner, times) を返す。"""
    sub = np.asarray(subset)
    local = cost[sub].argmin(axis=0)
    owner = sub[local]
    times = cost[owner, np.arange(cost.shape[1])]
    return float(times.max()), float(times.sum()), owner, times


def _key(cost, subset, objective):
    mk, tot, _, _ = evaluate(cost, subset)
    return (mk, tot) if objective == "makespan" else (tot, mk)


def _exact(cost, pool, required, r, objective, progress, cancel, n_comb):
    base = cost[required].min(axis=0) if required else np.full(cost.shape[1], np.inf)
    if r == 0:
        return list(required), 1
    pool_arr = np.asarray(pool)
    best_key, best = None, None
    it = combinations(range(len(pool)), r)
    done = 0
    while True:
        chunk = list(islice(it, CHUNK))
        if not chunk:
            break
        if cancel and cancel():
            raise Cancelled()
        idx = pool_arr[np.asarray(chunk)]                    # (m, r)
        times = np.minimum(cost[idx].min(axis=1), base)      # (m, n)
        primary, secondary = _score(times, objective)
        o = np.lexsort((secondary, primary))[0]
        key = (primary[o], secondary[o])
        if best_key is None or key < best_key:
            best_key, best = key, list(required) + [int(x) for x in idx[o]]
        done += len(chunk)
        if progress:
            progress(done / n_comb, f"全組合せを探索中 {done:,}/{n_comb:,}")
    return best, done


def _heuristic(cost, pool, required, r, objective, progress, cancel):
    """貪欲に 1 拠点ずつ追加 → 1 対 1 の入替えで改善が止まるまで回す。"""
    chosen = list(required)
    evaluated = 0
    for step in range(r):
        best_key, best_i = None, None
        for i in pool:
            if i in chosen:
                continue
            k = _key(cost, chosen + [i], objective)
            evaluated += 1
            if best_key is None or k < best_key:
                best_key, best_i = k, i
        chosen.append(best_i)
        if progress:
            progress(0.3 * (step + 1) / max(r, 1), f"貪欲法 {step + 1}/{r}")

    current = _key(cost, chosen, objective)
    improved, rounds = True, 0
    while improved and rounds < 200:
        improved, rounds = False, rounds + 1
        if cancel and cancel():
            raise Cancelled()
        best_move, best_key = None, current
        for pos in range(len(required), len(chosen)):
            for u in pool:
                if u in chosen:
                    continue
                trial = chosen[:pos] + [u] + chosen[pos + 1:]
                k = _key(cost, trial, objective)
                evaluated += 1
                if k < best_key:
                    best_key, best_move = k, (pos, u)
        if best_move:
            chosen[best_move[0]] = best_move[1]
            current, improved = best_key, True
        if progress:
            progress(min(0.3 + 0.07 * rounds, 0.99), f"入替え探索 {rounds} 回目")
    return chosen, evaluated


def solve(cost, k, allowed=None, required=(), objective="makespan",
          max_exact=5_000_000, progress=None, cancel=None):
    """候補から k 拠点を選ぶ。

    cost     : (n_candidates, n_targets) の numpy 配列 [分]
    allowed  : 選定対象にする候補 index(None なら全件)
    required : 必ず採用する候補 index
    戻り値   : dict(subset, owner, times, makespan, total, method, evaluated, combinations)
    """
    if objective not in OBJECTIVES:
        raise ValueError(f"objective は {OBJECTIVES} のいずれか: {objective}")
    n_cand = cost.shape[0]
    allowed = list(range(n_cand)) if allowed is None else list(allowed)
    required = [i for i in required]
    pool = [i for i in allowed if i not in required]
    r = k - len(required)
    if r < 0:
        raise ValueError(f"必須の補給地点 {len(required)} 件が採用数 {k} を超えています")
    if r > len(pool):
        raise ValueError(f"採用数 {k} が選定可能な補給地点 {len(required) + len(pool)} 件を超えています")

    n_comb = math.comb(len(pool), r)
    if n_comb <= max_exact:
        subset, evaluated = _exact(cost, pool, required, r, objective, progress, cancel, n_comb)
        method = "exact"
    else:
        subset, evaluated = _heuristic(cost, pool, required, r, objective, progress, cancel)
        method = "heuristic"
    mk, tot, owner, times = evaluate(cost, subset)
    return {"subset": [int(i) for i in subset], "owner": owner.tolist(),
            "times": times.tolist(), "makespan": mk, "total": tot,
            "method": method, "evaluated": int(evaluated), "combinations": n_comb}
