# -*- coding: utf-8 -*-
"""
配分ルート最適化ソルバー (容量1シャトル型, メイクスパン最小化)

前提:
    車1台が一度に運べるのは「1地点分の人員+機材セット」=1個のみ(容量1)。
    各車両は  補給地点で積む -> 1地点へ配る -> 補給地点へ戻って積み直す
    というシャトル動作を繰り返す。補給地点は stock 個の在庫を持つ。

    車両と補給地点に固定の所属関係はない。どの車両も、在庫が尽きない限り
    任意の補給地点から積んでよい。結果として「ある車両が特定デポを多用する」
    ことはあり得るが、それは最適化の結果であって前提ではない。

目的:
    全車両のルート所要時間の最大値(メイクスパン)を最小化。

設計:
    処理を目的ごとに分割している。solve_distribution() が全体の司令塔で、
    以下のコンポーネントを順に呼ぶだけ。

        1. check_stock          在庫充足の検証
        2. build_node_model     配送先/pickup/仮想スタートを「ノード」へ変換
        3. RoutingModel 構築    全車両を共通の仮想スタートから発進させる
        4. add_time_dimension   移動+作業時間, メイクスパン最小化
        5. add_capacity_dimension  容量1(積まずに連続配送を禁止)
        6. add_pickup_options   pickupを任意訪問にする
        7. build_search_params  探索アルゴリズム設定
        8. extract_solution     解 -> 人間が読める順路へ
"""

from dataclasses import dataclass, field
from math import radians, sin, cos, asin, sqrt

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

# 時間[分]を整数化するスケール(0.01分単位)
TIME_SCALE = 100
# メイクスパン(global span)に掛ける重み。総距離より「最遅ルート短縮」を優先させる
MAKESPAN_WEIGHT = 10000


# ---------------------------------------------------------------------------
# 距離ユーティリティ
# ---------------------------------------------------------------------------
def haversine_km(a, b):
    """(lat, lon) 2点間の大円距離 [km]"""
    lat1, lon1, lat2, lon2 = map(radians, [a[0], a[1], b[0], b[1]])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * 6371.0088 * asin(sqrt(h))


# ---------------------------------------------------------------------------
# 1. 入力の検証
# ---------------------------------------------------------------------------
def check_stock(targets, depots):
    """在庫が配送先数を満たすか検証。"""
    total = sum(d["stock"] for d in depots)
    if total < len(targets):
        raise ValueError(f"在庫不足: stock合計 {total} < 配送先 {len(targets)}")


# ---------------------------------------------------------------------------
# 2. ノードモデル
#    OR-Tools は経路を「ノード番号の連鎖」で扱うため、
#    配送先 / pickupユニット / 仮想スタート を一列のノードへ並べ直す。
# ---------------------------------------------------------------------------
@dataclass
class NodeModel:
    coords: list = field(default_factory=list)        # 各ノードの (lat, lon)
    demand: list = field(default_factory=list)        # 積載変化(配送先-1, pickup+1, その他0)
    pickup_depot: list = field(default_factory=list)  # pickupノード -> デポindex(他はNone)
    n_targets: int = 0                                # 先頭から n_targets 個が配送先
    pickup_nodes: list = field(default_factory=list)  # pickupノード番号の一覧
    garage: int = -1                                  # 全車両共通の仮想スタート/エンド

    def is_target(self, node):
        return node < self.n_targets

    def is_pickup(self, node):
        return self.demand[node] == +1

    def is_garage(self, node):
        return node == self.garage


def build_node_model(targets, depots):
    """配送先・在庫・仮想スタートを NodeModel へ展開する。

    レイアウト:
        [0 .. nT-1]            配送先         demand -1 (必須訪問)
        [nT .. nT+stock合計-1] pickupユニット  demand +1 (任意訪問, 在庫個数ぶん)
        [末尾1個]              garage         demand  0 (全車両の start/end)

    garage は特定デポに紐づかない中立な発着点。どの車両もここから出て、
    任意のデポの pickup を自由に選べる。garage との移動時間は 0 とする
    (車両の初期位置を前提に置かないための割り切り)。
    """
    m = NodeModel(n_targets=len(targets))

    # 配送先
    for t in targets:
        m.coords.append((t["lat"], t["lon"]))
        m.demand.append(-1)
        m.pickup_depot.append(None)

    # pickupユニット(= 在庫1個 = 1ノード)。どの車両からでも利用可。
    for di, d in enumerate(depots):
        for _ in range(d["stock"]):
            m.pickup_nodes.append(len(m.coords))
            m.coords.append((d["lat"], d["lon"]))
            m.demand.append(+1)
            m.pickup_depot.append(di)

    # 仮想スタート/エンド(garage)
    m.garage = len(m.coords)
    m.coords.append((0.0, 0.0))   # 座標は使わない(garage移動は時間0)
    m.demand.append(0)
    m.pickup_depot.append(None)

    return m


# ---------------------------------------------------------------------------
# 3. 時間次元(移動 + 作業/積込) と メイクスパン最小化
# ---------------------------------------------------------------------------
def add_time_dimension(routing, manager, model, speed_kmph, service_min, reload_min,
                       travel_time_min=None):
    """アークコスト=所要時間 を定義し、メイクスパンを最小化する目的を設定。

    travel_time_min : ((lat,lon),(lat,lon)) -> 分 の callable。
        指定時は Mapbox 由来などの実測所要時間を使う。
        None のときは従来どおり「球面距離 ÷ speed_kmph」で近似する。
    """

    def move_min(p, q):
        if travel_time_min is not None:
            return travel_time_min(model.coords[p], model.coords[q])
        return haversine_km(model.coords[p], model.coords[q]) / speed_kmph * 60.0

    def time_cb(from_index, to_index):
        p = manager.IndexToNode(from_index)
        q = manager.IndexToNode(to_index)
        # garage の出入りは時間0(車両の初期位置を前提にしない)
        if model.is_garage(p) or model.is_garage(q):
            return 0
        minutes = move_min(p, q)
        if model.is_target(q):       # 配送先に到着 -> 作業時間
            minutes += service_min
        elif model.is_pickup(q):     # pickupに到着 -> 積込時間
            minutes += reload_min
        return int(round(minutes * TIME_SCALE))

    transit_idx = routing.RegisterTransitCallback(time_cb)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_idx)

    routing.AddDimension(transit_idx, 0, 10 ** 9, True, "Time")
    time_dim = routing.GetDimensionOrDie("Time")
    # 各ルート所要時間の最大(=メイクスパン)を強く最小化
    time_dim.SetGlobalSpanCostCoefficient(MAKESPAN_WEIGHT)
    return time_dim


# ---------------------------------------------------------------------------
# 4. 容量次元(容量1) … 「積まずに連続配送」を物理的に禁止する
# ---------------------------------------------------------------------------
def add_capacity_dimension(routing, manager, model, num_vehicles):
    def demand_cb(from_index):
        return model.demand[manager.IndexToNode(from_index)]

    demand_idx = routing.RegisterUnaryTransitCallback(demand_cb)
    routing.AddDimensionWithVehicleCapacity(
        demand_idx, 0, [1] * num_vehicles, True, "Load")


# ---------------------------------------------------------------------------
# 5. pickupユニットを任意訪問にする(使わない在庫は skip)
#    在庫上限は「pickupノードを stock 個しか作らない」ことで構造的に保証済み。
#    どの車両がどの pickup を使うかは自由(デポ所属の固定なし)。
# ---------------------------------------------------------------------------
def add_pickup_options(routing, manager, model):
    for pn in model.pickup_nodes:
        routing.AddDisjunction([manager.NodeToIndex(pn)], 0)  # penalty 0 = skip可


# ---------------------------------------------------------------------------
# 6. 探索アルゴリズムの設定
# ---------------------------------------------------------------------------
def build_search_params(time_limit_sec):
    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC)
    params.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH)
    params.time_limit.FromSeconds(time_limit_sec)
    return params


# ---------------------------------------------------------------------------
# 7. 解の取り出し(ノード連鎖 -> 人間が読める順路)
#    「使ったデポ」は結果として算出する(前提にしない)。
# ---------------------------------------------------------------------------
def extract_solution(routing, manager, solution, time_dim, model, targets, depots):
    routes, makespan = [], 0.0
    for k in range(routing.vehicles()):
        idx = routing.Start(k)
        steps, deliveries, depots_used = [], 0, []
        while not routing.IsEnd(idx):
            node = manager.IndexToNode(idx)
            if model.is_target(node):
                steps.append(f"配[{targets[node]['name']}]")
                deliveries += 1
            elif model.is_pickup(node):
                name = depots[model.pickup_depot[node]]["name"]
                steps.append(f"補({name})")
                depots_used.append(name)
            idx = solution.Value(routing.NextVar(idx))

        t_min = solution.Value(time_dim.CumulVar(routing.End(k))) / TIME_SCALE
        makespan = max(makespan, t_min)
        routes.append({
            "vehicle": k,
            "deliveries": deliveries,
            "depots_used": sorted(set(depots_used)),   # 結果的に使ったデポ
            "time_min": round(t_min, 1),
            "steps": steps,
        })
    return {"makespan_min": round(makespan, 1), "routes": routes}


# ---------------------------------------------------------------------------
# 司令塔: 上のコンポーネントを順に呼ぶだけ
# ---------------------------------------------------------------------------
def solve_distribution(targets, depots, num_vehicles,
                       speed_kmph=40.0, service_min=0.0, reload_min=0.0,
                       time_limit_sec=15, travel_time_min=None):
    """
    travel_time_min : ((lat,lon),(lat,lon)) -> 分 の callable。
        Mapbox 由来の実測所要時間などを渡す。None なら speed_kmph で近似。

    戻り値: dict | None
        makespan_min : メイクスパン(分)
        routes       : 車両ごとの {vehicle, deliveries, depots_used, time_min, steps}
    """
    # 1. 検証
    check_stock(targets, depots)

    # 2. ノードモデル構築
    model = build_node_model(targets, depots)

    # 3. RoutingModel(全車両を共通の仮想スタート=garageから発進・帰着させる)
    starts = [model.garage] * num_vehicles
    manager = pywrapcp.RoutingIndexManager(
        len(model.coords), num_vehicles, starts, list(starts))
    routing = pywrapcp.RoutingModel(manager)

    # 4-6. 制約・目的
    time_dim = add_time_dimension(routing, manager, model,
                                  speed_kmph, service_min, reload_min,
                                  travel_time_min=travel_time_min)
    add_capacity_dimension(routing, manager, model, num_vehicles)
    add_pickup_options(routing, manager, model)

    # 7. 探索
    solution = routing.SolveWithParameters(build_search_params(time_limit_sec))
    if solution is None:
        return None

    # 8. 解の取り出し
    return extract_solution(routing, manager, solution, time_dim,
                            model, targets, depots)
