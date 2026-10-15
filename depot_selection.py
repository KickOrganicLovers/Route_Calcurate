# -*- coding: utf-8 -*-
"""補給地点の選定 + 車両台数配分ソルバー

役割:
    「候補リストから最良 K 地点を採用し、各採用地点に車両を何台配置するか」
    を決める。ルート最適化(distribute_solver)の 1 段手前の意思決定。

前提(1局1台):
    測定局 23 地点に車両を 1 台ずつ割り当てるため、車両数 = 地点数。
    各車両の動きは「待機拠点で積む → 担当 1 地点へ向かう」だけなので、
    車両 1 台の所要時間は
        reload_min + 移動時間(拠点 → 担当地点) + service_min
    で確定する。よって拠点 i に地点 j を割り当てた時点で所要時間が決まり、
    問題は「どの 5 拠点を選び、どの地点をどの拠点に割り当てるか」に帰着する。

目的:
    メイクスパン(最も遅い車両の所要時間)の最小化。
    同点の場合は総所要時間が小さいほうを選ぶ。

    これは施設配置問題の p-center 問題そのもの。候補が十数件なら
    全組合せを総当たりでき、近似ではなく厳密解が得られる。

    拠点あたりの台数に上限は設けない。よって選んだ拠点集合に対しては
    「各地点を最寄り拠点へ」が常に最適な割当になる。
"""

from itertools import combinations, islice

# 総当たりを許容する候補数の上限(これを超えると組合せ爆発するため貪欲法へ)
MAX_CANDIDATES_FOR_BRUTE_FORCE = 30


# ---------------------------------------------------------------------------
# 1. コスト行列: 拠点 i から地点 j を担当したときの車両 1 台の所要時間[分]
# ---------------------------------------------------------------------------
def build_cost_matrix(targets, candidates, travel_time_min,
                      service_min=0.0, reload_min=0.0):
    """cost[i][j] = 拠点 candidates[i] から targets[j] を担当する所要時間[分]。"""
    cost = []
    for c in candidates:
        row = []
        for t in targets:
            move = travel_time_min((c["lat"], c["lon"]), (t["lat"], t["lon"]))
            row.append(reload_min + move + service_min)
        cost.append(row)
    return cost


# ---------------------------------------------------------------------------
# 2. 割当: 選んだ拠点集合に対して、地点をどの拠点へ振るか
# ---------------------------------------------------------------------------
def assign_nearest(cost, subset, n_targets):
    """各地点を「選択拠点のうち最短の拠点」へ割り当てる。

    台数上限がないので、最寄り割当が常に最適(他へ回してもメイクスパンは悪化のみ)。
    戻り値: [地点jが割り当てられた拠点index, ...]
    """
    return [min(subset, key=lambda i: cost[i][j]) for j in range(n_targets)]


def evaluate_subset(cost, subset, n_targets):
    """拠点集合 subset の評価値 (メイクスパン, 総時間, 割当) を返す。"""
    owner = assign_nearest(cost, subset, n_targets)
    times = [cost[owner[j]][j] for j in range(n_targets)]
    return max(times), sum(times), owner



# 候補数が増えると combinations の純 Python ループでは日が暮れるため、
# numpy があれば同じ全組合せ探索をベクトル化して回す(結果は厳密解のまま)。
# チャンクあたりの組合せ数。メモリは chunk * k * n_targets * 8 バイト。
_BRUTE_FORCE_CHUNK = 20000


def _brute_force_numpy(cost, n_candidates, n_targets, k):
    """全組合せをベクトル化して評価し、最良の拠点集合を返す。

    numpy が無ければ None を返し、呼び出し側の純 Python 実装へ委ねる。
    評価順序(メイクスパン → 総時間)は evaluate_subset と揃えてある。
    """
    try:
        import numpy as np
    except ImportError:
        return None

    cost_arr = np.asarray(cost, dtype=float)
    best_key, best_subset = None, None
    it = combinations(range(n_candidates), k)
    while True:
        chunk = list(islice(it, _BRUTE_FORCE_CHUNK))
        if not chunk:
            break
        idx = np.asarray(chunk)                       # (m, k)
        times = cost_arr[idx].min(axis=1)             # (m, n_targets) 最寄り拠点
        makespan = times.max(axis=1)
        total = times.sum(axis=1)
        # メイクスパン優先、同点は総時間で決める
        order = np.lexsort((total, makespan))[0]
        key = (makespan[order], total[order])
        if best_key is None or key < best_key:
            best_key, best_subset = key, list(chunk[order])
    return best_subset

# ---------------------------------------------------------------------------
# 3. 拠点集合の探索
# ---------------------------------------------------------------------------
def _search_subsets(cost, n_candidates, n_targets, k):
    """メイクスパン最小の拠点集合を探す。

    候補数が小さければ全組合せを総当たり(厳密解)。
    大きい場合は「1 地点ずつ追加して最も改善する拠点を採る」貪欲法へ落とす。
    """
    if n_candidates <= MAX_CANDIDATES_FOR_BRUTE_FORCE:
        best_subset = _brute_force_numpy(cost, n_candidates, n_targets, k)
        if best_subset is None:
            best, best_subset = None, None
            for subset in combinations(range(n_candidates), k):
                got = evaluate_subset(cost, list(subset), n_targets)
                if best is None or (got[0], got[1]) < best[:2]:
                    best, best_subset = got, list(subset)
            return best_subset, best
        return best_subset, evaluate_subset(cost, best_subset, n_targets)

    chosen = []
    best = None
    while len(chosen) < k:
        cand_best, cand_i = None, None
        for i in range(n_candidates):
            if i in chosen:
                continue
            got = evaluate_subset(cost, chosen + [i], n_targets)
            if cand_best is None or (got[0], got[1]) < cand_best[:2]:
                cand_best, cand_i = got, i
        chosen.append(cand_i)
        best = cand_best
    return chosen, best


# ---------------------------------------------------------------------------
# 4. 司令塔
# ---------------------------------------------------------------------------
def select_depots(targets, candidates, travel_time_min, num_depots=5,
                  service_min=0.0, reload_min=0.0):
    """候補から num_depots 地点を選び、車両台数を配分する。

    targets         : [{name, lat, lon}, ...] 測定局(1 地点 = 車両 1 台)
    candidates      : [{code, name, lat, lon, ...}, ...] 補給地点候補
    travel_time_min : ((lat,lon),(lat,lon)) -> 分 の callable
    num_depots      : 採用する補給地点の数

    戻り値: dict
        depots      : 採用した補給地点(stock = 配置台数)のリスト
        assignments : [{target, depot, time_min}, ...] 地点ごとの担当拠点
        makespan_min: 最も遅い車両の所要時間(分)
        total_min   : 全車両の所要時間の合計(分)
        num_vehicles: 総車両台数(= 地点数)
    """
    n_targets = len(targets)
    n_cand = len(candidates)
    if num_depots > n_cand:
        raise ValueError(f"採用数 {num_depots} が候補数 {n_cand} を超えています")

    cost = build_cost_matrix(targets, candidates, travel_time_min,
                             service_min, reload_min)
    subset, best = _search_subsets(cost, n_cand, n_targets, num_depots)
    makespan, total, owner = best

    counts = {i: 0 for i in subset}
    for j in range(n_targets):
        counts[owner[j]] += 1

    depots = []
    for i in subset:
        d = dict(candidates[i])
        d["stock"] = counts[i]          # 配置台数 = 供給する在庫数
        depots.append(d)

    assignments = [{
        "target": targets[j]["name"],
        "depot": candidates[owner[j]]["name"],
        "time_min": round(cost[owner[j]][j], 1),
    } for j in range(n_targets)]

    return {
        "depots": depots,
        "assignments": assignments,
        "makespan_min": round(makespan, 1),
        "total_min": round(total, 1),
        "num_vehicles": n_targets,
    }
