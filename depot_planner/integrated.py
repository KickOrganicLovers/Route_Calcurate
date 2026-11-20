# -*- coding: utf-8 -*-
"""補給地点の選定と配分ルート最適化を一体で解く(Location-Routing)。

これまでは
    1. 選定(solver.py)… 各目的地へ直行する前提で K 拠点を選び、機材数を決める
    2. ルート(routing.py)… 1 の拠点・機材数を固定して車両の順路を決める
を順に 1 回ずつ走らせていたため、積載量 C >= 2 や車両の総数を指定したときに
「直行なら最良だが、実際に回るとそうでもない拠点の組合せ」を選んでしまうことがあった。

ここでは次の 3 段で、ルートの所要時間そのもので拠点を選ぶ。

    a. 候補の絞り込み … 選定ソルバーで直行前提の上位 screen_candidates 組を出す
                         (優先 / なるべく避ける を考慮した上位と、考慮しない上位の両方)
    b. ルートで比較   … 各組で短い探索時間(screen_time_sec)のルート最適化を回し、
                         実際の最長所要(目的関数が total なら総所要)で比べる
    c. 本計算         … 選んだ組で探索時間 time_limit_sec のルート最適化をやり直す。
                         直行前提の 1 位(従来方式が選ぶ組)も従来と同じ条件で解き、比べ直す
                         (従来の 2 段方式より悪い結果にはしない)

各拠点に置く機材の数もルートの結果で決める。1 組ごとに次の 2 通りで解き、良いほうを採る。
    fixed    … 機材数 = 直行前提の担当数(従来の 2 段方式と同じ前提)
    flexible … 採用拠点のどこからでも積める(pickup_limit = 目的地数)。
                実際に積んだ個数をその拠点の機材数とする
flexible のほうが選択肢は広いが、時間制限つきの局所探索では fixed より悪い解で
止まることがあるため、両方を試す(探索時間を延ばしてもこの差はほとんど縮まらない)。
車両の待機配置はどちらも直行前提の担当数から決める(routing.allocate_vehicles)。

1 台 1 か所(積載量 1・配送上限 1・台数自動)のときは、ルートの所要時間が直行前提の
所要時間と一致するので、a の 1 位がそのまま最良になる。このときは比較を省く。

優先 / なるべく避ける は、b の結果に対して solver と同じ規則で適用する:
ルートでの最良値 + 許容幅 以内の組のうち、目的地を担当する(機材を積んだ)
「優先」拠点が多い → 「なるべく避ける」拠点が少ない → 所要時間が短い 順に選ぶ。
"""

import numpy as np

from .routing import solve_routes
from .solver import evaluate


def effective_max_deliveries(max_deliveries, capacity, num_vehicles, n_targets):
    """routing.solve_routes と同じ規則で、配送上限の実際の値を返す。"""
    if max_deliveries is not None:
        return int(max_deliveries)
    return capacity if num_vehicles is None else max(n_targets, 1)


def is_direct_equivalent(capacity, max_deliveries, num_vehicles, n_targets):
    """ルートの所要時間が直行前提の所要時間と一致する条件(1 台 1 か所・台数自動)。"""
    md = effective_max_deliveries(max_deliveries, capacity, num_vehicles, n_targets)
    return capacity == 1 and md == 1 and num_vehicles is None


def _sub_matrix(tmat, subset, n_cands, n_targets):
    """全候補+目的地の行列から [採用拠点..., 目的地...] の部分行列を取り出す。"""
    rows = list(subset) + [n_cands + j for j in range(n_targets)]
    return tmat[np.ix_(rows, rows)]


STOCK_MODES = ("fixed", "flexible")


def route_subset(tmat, cost, subset, n_cands, *, capacity, max_deliveries, num_vehicles,
                 service_min, reload_min, time_limit_sec, flexible=True,
                 progress=None, cancel=None):
    """拠点の組 subset でルート最適化を 1 回解く。res の depot / start は subset 内の位置。"""
    n = cost.shape[1]
    _, _, owner, _ = evaluate(cost, subset)
    stock = [int((owner == i).sum()) for i in subset]
    res = solve_routes(_sub_matrix(tmat, subset, n_cands, n), stock,
                       capacity=capacity, max_deliveries=max_deliveries,
                       num_vehicles=num_vehicles, service_min=service_min,
                       reload_min=reload_min, time_limit_sec=time_limit_sec,
                       progress=progress, cancel=cancel,
                       pickup_limit=[n] * len(subset) if flexible else None)
    if res is not None:
        res["direct_stock"] = stock
        res["stock_mode"] = "flexible" if flexible else "fixed"
    return res


def route_best(tmat, cost, subset, n_cands, objective, modes=STOCK_MODES, progress=None, **kw):
    """modes の各方式で解き、目的関数の良いほうを返す(どれも解なしなら None)。"""
    best = None
    for n, mode in enumerate(modes):
        sp = None
        if progress:
            sp = (lambda lo, hi: lambda f, msg: progress(lo + (hi - lo) * f, msg))(
                n / len(modes), (n + 1) / len(modes))
        res = route_subset(tmat, cost, subset, n_cands, flexible=(mode == "flexible"),
                           progress=sp, **kw)
        if res is not None and (best is None or _primary(res, objective) < _primary(best, objective)):
            best = res
    return best


def picked_per_depot(res, k):
    """ルート結果から、拠点(subset 内の位置)ごとに積んだ機材の個数。"""
    picked = [0] * k
    for v in res["vehicles"]:
        for s in v["stops"]:
            if s["kind"] == "pickup":
                picked[s["depot"]] += s["count"]
    return picked


def _primary(res, objective):
    if objective == "makespan":
        return float(res["makespan"]), float(sum(v["time_min"] for v in res["vehicles"]))
    return float(sum(v["time_min"] for v in res["vehicles"])), float(res["makespan"])


def plan(tmat, cost, alternatives, *, objective="makespan", capacity=1, max_deliveries=None,
         num_vehicles=None, service_min=0.0, reload_min=0.0, time_limit_sec=30,
         screen_time_sec=2, preferred=(), avoid=(), margin=0.0,
         progress=None, cancel=None):
    """選定の上位候補 alternatives をルートで比べ、最良の組とそのルートを返す。

    tmat         : (n_cands + n, n_cands + n) の移動時間。並びは [全候補..., 目的地...]
    cost         : (n_cands, n) の直行前提コスト(選定で使ったもの)
    alternatives : 候補 index の組のリスト(先頭が直行前提の最良)
    戻り値 dict:
        subset, route(solve_routes の戻り値), screened([{rank, subset, direct_*, route_*}]),
        chosen_rank(1 始まり), best_primary(優先などを考慮しないルートでの最良値),
        skipped(比較を省いた理由 or None)
    """
    n_cands, n = cost.shape
    common = dict(capacity=capacity, max_deliveries=max_deliveries, num_vehicles=num_vehicles,
                  service_min=service_min, reload_min=reload_min)
    pref, avd = set(preferred), set(avoid)

    skipped = None
    cands = [list(a) for a in alternatives]
    if is_direct_equivalent(capacity, max_deliveries, num_vehicles, n):
        skipped = "1 台 1 か所の配送なので、直行前提の評価がそのままルートの所要時間になる"
        cands = cands[:1]
    elif len(cands) <= 1:
        skipped = "比べる候補が 1 組しかない"

    def stage(lo, hi):
        if not progress:
            return None
        return lambda f, msg: progress(lo + (hi - lo) * max(0.0, min(f, 1.0)), msg)

    def make_row(rank, sub, res):
        mk, tot, _, _ = evaluate(cost, sub)
        row = {"rank": rank, "subset": sub, "direct_makespan": mk, "direct_total": tot,
               "route": res, "stock_mode": res["stock_mode"] if res else None, "final": False}
        set_route(row, res)
        return row

    def set_route(row, res):
        row["route"] = res
        if res is None:
            return
        sub = row["subset"]
        picked = picked_per_depot(res, len(sub))
        used = [sub[i] for i, c in enumerate(picked) if c > 0]
        row.update(route_makespan=float(res["makespan"]),
                   route_total=float(sum(v["time_min"] for v in res["vehicles"])),
                   key=_primary(res, objective), stock_mode=res["stock_mode"],
                   n_pref=sum(u in pref for u in used), n_avoid=sum(u in avd for u in used))

    def choose(rows):
        ok = [r for r in rows if r["route"] is not None]
        if not ok:
            return rows[0], None
        best = min(r["key"][0] for r in ok)
        m = margin if (pref or avd) else 0.0
        within = [r for r in ok if r["key"][0] <= best + m + 1e-9]
        return min(within, key=lambda r: (-r["n_pref"], r["n_avoid"], r["key"], r["rank"])), best

    # b. ルートで比較(試算)
    screened = []
    compare = len(cands) > 1
    screen_share = 0.5 if compare else 0.0
    for rank, sub in enumerate(cands if compare else [], 1):
        sp = stage(screen_share * (rank - 1) / len(cands), screen_share * rank / len(cands))
        if sp:
            sp(0.0, f"候補 {rank}/{len(cands)} 組目のルートを試算中")
        res = route_best(tmat, cost, sub, n_cands, objective, time_limit_sec=screen_time_sec,
                         cancel=cancel, **common)
        screened.append(make_row(rank, sub, res))

    # c. 本計算: 試算で選んだ組を、良かった方式で探索時間いっぱい解く。
    #    直行前提の 1 位(従来の 2 段方式が選ぶ組)も、従来と同じ fixed 方式・同じ探索時間で解いて
    #    比べ直す。これで従来方式より悪い結果にはならない。
    if compare:
        chosen, _ = choose(screened)
        finals = [chosen] + ([screened[0]] if chosen is not screened[0] else [])
    else:
        finals = [make_row(1, cands[0], None)]
    for n_f, row in enumerate(finals):
        if compare and row["rank"] == 1:
            # 従来方式と同じ条件。自身が採用候補で試算が flexible なら、それも解き直す
            extra = (row["stock_mode"],) if row is chosen and row["stock_mode"] == "flexible" else ()
            modes = ("fixed",) + extra
        elif row["route"] is not None:
            modes = (row["stock_mode"],)
        elif is_direct_equivalent(capacity, max_deliveries, num_vehicles, n):
            modes = ("fixed",)      # 直行前提の担当どおりが最適(各目的地を最寄り拠点から)
        else:
            modes = STOCK_MODES
        fp = stage(screen_share + (1 - screen_share) * n_f / len(finals),
                   screen_share + (1 - screen_share) * (n_f + 1) / len(finals))
        if fp:
            fp(0.0, f"候補 {row['rank']} 組目のルートを本計算中")
        res = route_best(tmat, cost, row["subset"], n_cands, objective, modes=modes,
                         time_limit_sec=time_limit_sec, progress=fp, cancel=cancel, **common)
        # 時間制限つきの探索なので、本計算が試算より悪いことが稀にある。そのときは試算の解を使う
        if res is not None and (row["route"] is None or _primary(res, objective) <= row["key"]):
            set_route(row, res)
        row["final"] = True

    rows = screened if compare else finals
    chosen, best_primary = choose(rows)
    return {"subset": [int(i) for i in chosen["subset"]], "route": chosen["route"],
            "chosen_rank": chosen["rank"], "best_primary": best_primary, "skipped": skipped,
            "screened": [{k: v for k, v in r.items() if k not in ("route", "key")}
                         for r in screened]}
