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

優先 / なるべく避ける(preferred / avoid):
    まず重みを無視した最良値 best(目的関数の主たる値)を求め、
    主たる値が best + margin 以内の組合せの中から
        1. 目的地を担当する「優先」拠点が多い
        2. 目的地を担当する「なるべく避ける」拠点が少ない
        3. 目的関数が良い
    の順で選ぶ。margin=10 なら「最良より 10 分遅くなるまでは優先拠点を採る」。
    担当 0 件の拠点は採用しても意味がないので数えない。

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


def _pref_counts(cost, subset, weight):
    """subset のうち目的地を担当する拠点の (優先の数, なるべく避けるの数)。"""
    sub = np.asarray(subset)
    used = sub[np.unique(cost[sub].argmin(axis=0))]
    w = weight[used]
    return int((w > 0).sum()), int((w < 0).sum())


def _pick(classes, margin):
    """{(-優先数, 避ける数): (key, subset)} から許容幅内で最も好ましいものを選ぶ。"""
    best = min(v[0][0] for v in classes.values())
    ok = [(c, v) for c, v in classes.items() if v[0][0] <= best + margin + 1e-9]
    c, (key, subset) = min(ok, key=lambda cv: (cv[0], cv[1][0]))
    return subset, best


def _exact(cost, pool, required, r, objective, progress, cancel, n_comb, weight, margin):
    req = np.asarray(required, dtype=int)
    if r == 0:
        return list(required), 1, None
    pool_arr = np.asarray(pool)
    use_w = weight is not None
    classes = {}     # (-優先数, 避ける数) → (key, subset)。重みが無ければ (0, 0) の 1 クラスだけ
    it = combinations(range(len(pool)), r)
    done = 0
    n_t = cost.shape[1]
    while True:
        chunk = list(islice(it, CHUNK))
        if not chunk:
            break
        if cancel and cancel():
            raise Cancelled()
        idx = pool_arr[np.asarray(chunk)]                    # (m, r)
        if len(req):
            idx = np.hstack([np.broadcast_to(req, (len(idx), len(req))), idx])
        sub_cost = cost[idx]                                 # (m, k, n)
        local = sub_cost.argmin(axis=1)                      # (m, n)
        times = np.take_along_axis(sub_cost, local[:, None, :], axis=1)[:, 0, :]
        primary, secondary = _score(times, objective)
        if use_w:
            m, k = idx.shape
            used = np.zeros((m, k), dtype=bool)
            used[np.repeat(np.arange(m), n_t), local.ravel()] = True
            w = weight[idx]
            n_pref = ((w > 0) & used).sum(axis=1)
            n_avoid = ((w < 0) & used).sum(axis=1)
            cls = np.stack([-n_pref, n_avoid], axis=1)
        else:
            cls = np.zeros((len(idx), 2), dtype=int)
        for c in np.unique(cls, axis=0):
            mask = np.where((cls == c).all(axis=1))[0]
            o = mask[np.lexsort((secondary[mask], primary[mask]))[0]]
            key = (float(primary[o]), float(secondary[o]))
            ck = (int(c[0]), int(c[1]))
            if ck not in classes or key < classes[ck][0]:
                classes[ck] = (key, [int(x) for x in idx[o]])
        done += len(chunk)
        if progress:
            progress(done / n_comb, f"全組合せを探索中 {done:,}/{n_comb:,}")
    subset, best = _pick(classes, margin if use_w else 0.0)
    return subset, done, best


def _heuristic_weighted(cost, pool, required, r, objective, progress, cancel, weight, margin):
    """重みなしで最良値を求めてから、許容幅内で優先拠点の多い組合せを入替え探索する。"""
    plain, evaluated = _heuristic(cost, pool, required, r, objective,
                                  _sub(progress, 0.0, 0.5), cancel)
    best = _key(cost, plain, objective)[0]
    limit = best + margin + 1e-9

    def key(subset):
        k = _key(cost, subset, objective)
        p, a = _pref_counts(cost, subset, weight)
        return (k[0] > limit, -p, a, k[0], k[1])

    chosen = list(plain)
    current = key(chosen)
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
                k = key(trial)
                evaluated += 1
                if k < best_key:
                    best_key, best_move = k, (pos, u)
        if best_move:
            chosen[best_move[0]] = best_move[1]
            current, improved = best_key, True
        if progress:
            progress(min(0.5 + 0.05 * rounds, 0.99), f"優先を考慮した入替え探索 {rounds} 回目")
    return chosen, evaluated, best


def _sub(progress, lo, hi):
    if not progress:
        return None
    return lambda f, msg: progress(lo + (hi - lo) * f, msg)


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
          max_exact=5_000_000, progress=None, cancel=None,
          preferred=(), avoid=(), margin=10.0):
    """候補から k 拠点を選ぶ。

    cost      : (n_candidates, n_targets) の numpy 配列 [分]
    allowed   : 選定対象にする候補 index(None なら全件)
    required  : 必ず採用する候補 index
    preferred : 優先して採用する候補 index
    avoid     : なるべく採用しない候補 index
    margin    : 優先 / なるべく避けるのために許す、目的関数(主たる値)の悪化幅[分]
    戻り値    : dict(subset, owner, times, makespan, total, method, evaluated, combinations,
                     best_primary … 優先などを無視したときの最良値)
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

    weight = None
    if preferred or avoid:
        weight = np.zeros(n_cand, dtype=int)
        weight[list(preferred)] = 1
        weight[list(avoid)] = -1
    margin = max(float(margin or 0.0), 0.0)

    n_comb = math.comb(len(pool), r)
    if n_comb <= max_exact:
        subset, evaluated, best = _exact(cost, pool, required, r, objective, progress, cancel,
                                         n_comb, weight, margin)
        method = "exact"
    elif weight is not None:
        subset, evaluated, best = _heuristic_weighted(cost, pool, required, r, objective,
                                                      progress, cancel, weight, margin)
        method = "heuristic"
    else:
        subset, evaluated = _heuristic(cost, pool, required, r, objective, progress, cancel)
        best, method = None, "heuristic"
    mk, tot, owner, times = evaluate(cost, subset)
    primary = mk if objective == "makespan" else tot
    n_pref, n_avoid = _pref_counts(cost, subset, weight) if weight is not None else (0, 0)
    return {"subset": [int(i) for i in subset], "owner": owner.tolist(),
            "times": times.tolist(), "makespan": mk, "total": tot,
            "method": method, "evaluated": int(evaluated), "combinations": n_comb,
            "best_primary": primary if best is None else float(best),
            "num_preferred": n_pref, "num_avoided": n_avoid}
