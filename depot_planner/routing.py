# -*- coding: utf-8 -*-
"""配分ルート最適化(容量 C のシャトル型、メイクスパン最小化)。

補給地点の選定(solver.py)のあとに走る 2 段目。選定で決まった
「どの補給地点に機材を何個置くか」を前提に、各車両の順路を OR-Tools で確定する。

前提:
    車 1 台に積める機材は vehicle_capacity 個まで(容量 C)。各車両は
        補給地点で最大 C 個積む → 最大 C か所の目的地へ順に配る → 補給地点へ戻って積み直す
    を繰り返す。補給地点 i には stock[i] 個の機材がある。車両の総数を指定しなければ
    ceil(stock[i] / C) 台の車両が補給地点 i で待機する。総数を指定した場合は、
    その台数を機材数に比例して各補給地点へ割り振る(allocate_vehicles)。
    車両と補給地点に固定の所属はなく、在庫が残っていれば
    どの車両もどの補給地点から積んでよい。

目的:
    全車両のルート所要時間の最大値(メイクスパン)を最小化。

所要時間の行列:
    time[p][q] は「地点 p → 地点 q」の移動時間[分]。地点の並びは
    [補給地点 0..k-1, 目的地 0..n-1] とする(runner が組み立てる)。
"""

import math
import time as _time
from dataclasses import dataclass, field

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from .solver import Cancelled

# 時間[分]を整数化するスケール(0.01 分単位)
TIME_SCALE = 100
# メイクスパン(global span)に掛ける重み。総所要より「最遅ルート短縮」を優先させる
MAKESPAN_WEIGHT = 10000


def vehicles_needed(stock, capacity):
    """機材 stock 個を積載量 capacity の車両で 1 回に積み切る台数。"""
    return math.ceil(stock / capacity)


def allocate_vehicles(stock, capacity, total=None):
    """補給地点ごとの待機台数を返す。

    total=None なら各拠点の機材を 1 回で積み切れる台数(vehicles_needed)。
    total を指定したら、機材のある拠点へまず 1 台ずつ(機材の多い順)、
    残りを「機材数 / (割当台数 + 1)」が最大の拠点へ 1 台ずつ配る(ドント方式)。
    """
    if total is None:
        return [vehicles_needed(s, capacity) for s in stock]
    alloc = [0] * len(stock)
    stocked = sorted((i for i, s in enumerate(stock) if s > 0), key=lambda i: -stock[i])
    if not stocked:
        return alloc
    for i in stocked[:total]:
        alloc[i] = 1
    for _ in range(total - sum(alloc)):
        i = max(stocked, key=lambda i: stock[i] / (alloc[i] + 1))
        alloc[i] += 1
    return alloc


# ---------------------------------------------------------------------------
# ノードモデル
#   OR-Tools は経路を「ノード番号の連鎖」で扱うため、
#   目的地 / 在庫 1 個ぶんの積込ノード / 出発ノード / 終点 を一列に並べ直す。
# ---------------------------------------------------------------------------
@dataclass
class NodeModel:
    point: list = field(default_factory=list)     # ノード -> 行列上の地点 index(garage は None)
    demand: list = field(default_factory=list)    # 積載変化(目的地 -1 / 積込 +1 / その他 0)
    depot_of: list = field(default_factory=list)  # 積込ノード -> 補給地点 index(他は None)
    n_targets: int = 0                            # 先頭 n_targets 個が目的地
    pickup_nodes: list = field(default_factory=list)
    garage: int = -1                              # 全車両共通の終点(移動時間 0)
    start_nodes: list = field(default_factory=list)

    def is_target(self, node):
        return node < self.n_targets

    def is_pickup(self, node):
        return self.demand[node] == +1


def build_node_model(n_depots, n_targets, stock, vehicle_depots):
    """
    レイアウト:
        [0 .. n-1]           目的地      demand -1(必須訪問)
        [n .. n+Σstock-1]    積込ノード  demand +1(任意訪問、在庫 1 個 = 1 ノード)
        [次の 1 個]          garage      demand  0(全車両の終点。帰路は評価しない)
        [末尾 台数ぶん]      出発ノード  demand  0(待機する補給地点の位置)
    """
    m = NodeModel(n_targets=n_targets)
    for j in range(n_targets):
        m.point.append(n_depots + j)
        m.demand.append(-1)
        m.depot_of.append(None)
    for i, s in enumerate(stock):
        for _ in range(s):
            m.pickup_nodes.append(len(m.point))
            m.point.append(i)
            m.demand.append(+1)
            m.depot_of.append(i)
    m.garage = len(m.point)
    m.point.append(None)
    m.demand.append(0)
    m.depot_of.append(None)
    for i in vehicle_depots:
        m.start_nodes.append(len(m.point))
        m.point.append(i)
        m.demand.append(0)
        m.depot_of.append(None)
    return m


def _initial_routes(m, vehicle_depots, move, capacity, max_deliveries):
    """「近い目的地を均等に受け持ち、近い補給地点で積んでは近い順に配る」自明解を作る(探索の種)。

    制約が厳しい(1 台 1 か所、台数が少なく積み直しが必須など)と OR-Tools の
    構築ヒューリスティクスが初期解を作れないことがあるため。作れなければ None。
    """
    nv, n = len(vehicle_depots), m.n_targets
    quota = min(max_deliveries, math.ceil(n / nv))
    if nv * quota < n:
        return None
    pool = {}
    for pn in m.pickup_nodes:
        pool.setdefault(m.depot_of[pn], []).append(pn)

    pairs = sorted((move(sn, tn), k, tn) for k, sn in enumerate(m.start_nodes)
                   for tn in range(n))
    drops = [[] for _ in vehicle_depots]
    done = set()
    for _, k, tn in pairs:
        if tn in done or len(drops[k]) >= quota:
            continue
        drops[k].append(tn)
        done.add(tn)
    if len(done) < n:
        return None

    routes = []
    for k in range(nv):
        route, here, rest = [], m.start_nodes[k], list(drops[k])
        while rest:
            # 在庫の残っている最寄りの補給地点で、積めるだけ積む
            depot = min((d for d in pool if pool[d]), key=lambda d: move(here, pool[d][-1]))
            c = min(capacity, len(rest), len(pool[depot]))
            for _ in range(c):
                here = pool[depot].pop()
                route.append(here)
            for _ in range(c):
                nxt = min(rest, key=lambda tn: move(here, tn))
                route.append(nxt)
                rest.remove(nxt)
                here = nxt
        routes.append(route)
    return routes


def solve_routes(time, stock, capacity=1, max_deliveries=None, num_vehicles=None,
                 service_min=0.0, reload_min=0.0, time_limit_sec=30,
                 progress=None, cancel=None):
    """各車両の順路を求める。

    time           : (k+n, k+n) の移動時間[分]。並びは [補給地点..., 目的地...]
    stock          : 補給地点ごとの機材数(合計 = 目的地数 n)
    capacity       : 1 台に積める機材の数
    max_deliveries : 1 台あたりの配送件数の上限(積み直し込み)。None なら
                     台数自動のとき capacity と同じ(1 回の積込で配り切り、積み直さない)、
                     台数指定のとき上限なし(足りない分は積み直しで補う)
    num_vehicles   : 車両の総数。None なら各拠点で 1 回に積み切れる台数の合計
    戻り値: dict | None(解なし)
        makespan    : 最も遅い車両の所要時間[分]
        vehicles    : [{start, time_min, deliveries, stops}, ...]
                      stops = [{"kind": "pickup", "depot": i, "count": c, "done_min": t}
                               | {"kind": "deliver", "target": j, "done_min": t}, ...]
    """
    k = len(stock)
    n = len(time) - k
    if capacity < 1:
        raise ValueError(f"積載量は 1 以上にしてください: {capacity}")
    if sum(stock) < n:
        raise ValueError(f"機材不足: 合計 {sum(stock)} < 目的地 {n}")
    if num_vehicles is not None and num_vehicles < 1:
        raise ValueError(f"車両の総数は 1 以上にしてください: {num_vehicles}")
    if max_deliveries is None:
        max_deliveries = capacity if num_vehicles is None else max(n, 1)

    alloc = allocate_vehicles(stock, capacity, num_vehicles)
    vehicle_depots = [i for i, a in enumerate(alloc) for _ in range(a)]
    nv = len(vehicle_depots)
    if max_deliveries * nv < n:
        raise ValueError(f"台数不足: {nv} 台 x 上限 {max_deliveries} 件 < 目的地 {n} 件")

    m = build_node_model(k, n, stock, vehicle_depots)

    def move(p, q):
        if m.point[p] is None or m.point[q] is None:
            return 0.0
        return float(time[m.point[p]][m.point[q]])

    manager = pywrapcp.RoutingIndexManager(len(m.point), nv, list(m.start_nodes), [m.garage] * nv)
    routing = pywrapcp.RoutingModel(manager)

    # 時間: 移動 + 到着先での作業/積込。メイクスパンを強く最小化する。
    def time_cb(a, b):
        p, q = manager.IndexToNode(a), manager.IndexToNode(b)
        if q == m.garage:
            return 0
        minutes = move(p, q)
        if m.is_target(q):
            minutes += service_min
        elif m.is_pickup(q):
            minutes += reload_min
        return int(round(minutes * TIME_SCALE))

    transit = routing.RegisterTransitCallback(time_cb)
    routing.SetArcCostEvaluatorOfAllVehicles(transit)
    routing.AddDimension(transit, 0, 10 ** 9, True, "Time")
    time_dim = routing.GetDimensionOrDie("Time")
    time_dim.SetGlobalSpanCostCoefficient(MAKESPAN_WEIGHT)

    # 積載: 積込 +1 / 配送 -1 が 0..capacity に収まる(積まずに配る・積みすぎを禁止)
    load = routing.RegisterUnaryTransitCallback(lambda a: m.demand[manager.IndexToNode(a)])
    routing.AddDimensionWithVehicleCapacity(load, 0, [capacity] * nv, True, "Load")

    # 配送件数の上限
    count = routing.RegisterUnaryTransitCallback(
        lambda a: 1 if m.is_target(manager.IndexToNode(a)) else 0)
    routing.AddDimensionWithVehicleCapacity(count, 0, [max_deliveries] * nv, True, "Deliveries")

    # 積込ノードは任意訪問(使わない在庫は飛ばしてよい)
    for pn in m.pickup_nodes:
        routing.AddDisjunction([manager.NodeToIndex(pn)], 0)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.FromSeconds(int(time_limit_sec))

    # 進捗と中止: 改善解が出るたびに呼ばれる
    started = _time.monotonic()
    found = [0]
    cancelled = [False]

    def on_solution():
        found[0] += 1
        if cancel and cancel():
            cancelled[0] = True
            routing.CancelSearch()
            return
        if progress:
            frac = (_time.monotonic() - started) / max(time_limit_sec, 1)
            span = max(time_dim.CumulVar(routing.End(v)).Value() for v in range(nv)) / TIME_SCALE
            progress(min(frac, 0.99), f"ルート探索中 改善 {found[0]} 回 / 最長 {span:.1f} 分")

    routing.AddAtSolutionCallback(on_solution)

    seed = _initial_routes(m, vehicle_depots, move, capacity, max_deliveries)
    if seed is not None:
        routing.CloseModelWithParameters(params)
        initial = routing.ReadAssignmentFromRoutes(seed, True)
        solution = routing.SolveFromAssignmentWithParameters(initial, params) if initial else None
    else:
        solution = routing.SolveWithParameters(params)
    if cancelled[0]:
        raise Cancelled()
    if solution is None:
        return None

    vehicles, makespan = [], 0.0
    for v in range(nv):
        idx = routing.Start(v)
        stops = []
        while not routing.IsEnd(idx):
            node = manager.IndexToNode(idx)
            done = solution.Value(time_dim.CumulVar(idx)) / TIME_SCALE
            if m.is_target(node):
                stops.append({"kind": "deliver", "target": node, "done_min": round(done, 1)})
            elif m.is_pickup(node):
                last = stops[-1] if stops else None
                if last and last["kind"] == "pickup" and last["depot"] == m.depot_of[node]:
                    last["count"] += 1          # 同じ補給地点での連続積込は 1 回にまとめる
                    last["done_min"] = round(done, 1)
                else:
                    stops.append({"kind": "pickup", "depot": m.depot_of[node], "count": 1,
                                  "done_min": round(done, 1)})
            idx = solution.Value(routing.NextVar(idx))
        t = solution.Value(time_dim.CumulVar(routing.End(v))) / TIME_SCALE
        makespan = max(makespan, t)
        vehicles.append({"start": vehicle_depots[v], "time_min": round(t, 1),
                         "deliveries": sum(s["kind"] == "deliver" for s in stops),
                         "stops": stops})
    return {"makespan": round(makespan, 1), "vehicles": vehicles,
            "num_vehicles": nv, "max_deliveries": max_deliveries, "solutions_found": found[0],
            "elapsed_sec": round(_time.monotonic() - started, 1)}
