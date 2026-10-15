# -*- coding: utf-8 -*-
"""
配分ルート最適化ソルバー (容量Cシャトル型, メイクスパン最小化)

前提:
    車1台が一度に運べるのは「1地点分の人員+機材セット」を vehicle_capacity 個まで
    (容量C。既定は 1)。各車両は
        補給地点で最大C個積む -> 最大C地点へ順に配る -> 補給地点へ戻って積み直す
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
        5. add_capacity_dimension  容量C(積んだ個数を超える連続配送を禁止)
        6. add_pickup_options   pickupを任意訪問にする
        7. build_search_params  探索アルゴリズム設定
        8. extract_solution     解 -> 人間が読める順路へ
"""

from dataclasses import dataclass, field

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

# 時間[分]を整数化するスケール(0.01分単位)
TIME_SCALE = 100
# メイクスパン(global span)に掛ける重み。総距離より「最遅ルート短縮」を優先させる
MAKESPAN_WEIGHT = 10000


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
    garage: int = -1                                  # 全車両共通の仮想エンド
    start_nodes: list = field(default_factory=list)   # 車両ごとの出発ノード
    start_depot: list = field(default_factory=list)   # 車両ごとの出発デポindex

    def is_target(self, node):
        return node < self.n_targets

    def is_pickup(self, node):
        return self.demand[node] == +1

    def is_garage(self, node):
        return node == self.garage


def build_node_model(targets, depots, vehicle_starts):
    """配送先・在庫・仮想スタートを NodeModel へ展開する。

    レイアウト:
        [0 .. nT-1]            配送先         demand -1 (必須訪問)
        [nT .. nT+stock合計-1] pickupユニット  demand +1 (任意訪問, 在庫個数ぶん)
        [末尾1個]              garage         demand  0 (全車両の start/end)

    各車両は待機デポ(start_nodes)から発進し、共通の garage へ帰着する。
    garage は特定デポに紐づかない中立な発着点で、移動時間は 0 とする
    (帰路を評価しないための割り切り)。
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

    # 車両ごとの出発ノード(待機する補給地点の座標に置く)
    for di in vehicle_starts:
        m.start_nodes.append(len(m.coords))
        m.start_depot.append(di)
        m.coords.append((depots[di]["lat"], depots[di]["lon"]))
        m.demand.append(0)
        m.pickup_depot.append(None)

    return m


# ---------------------------------------------------------------------------
# 3. 時間次元(移動 + 作業/積込) と メイクスパン最小化
# ---------------------------------------------------------------------------
def make_move_min(model, travel_time_min):
    """ノード番号2つ -> 移動時間[分] の関数を作る。

    travel_time_min : ((lat,lon),(lat,lon)) -> 分 の callable
        (道路ネットワーク上の実測所要時間)。
    """
    def move_min(p, q):
        return travel_time_min(model.coords[p], model.coords[q])
    return move_min


def add_time_dimension(routing, manager, model, service_min, reload_min,
                       travel_time_min):
    """アークコスト=所要時間 を定義し、メイクスパンを最小化する目的を設定。"""
    move_min = make_move_min(model, travel_time_min)

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
# 4. 容量次元(容量C) … 「積載量を超えて積む」「積まずに配る」を物理的に禁止する
#    pickup で +1、配送で -1。積載量は 0..capacity の範囲に収まる。
# ---------------------------------------------------------------------------
def add_capacity_dimension(routing, manager, model, num_vehicles, capacity):
    def demand_cb(from_index):
        return model.demand[manager.IndexToNode(from_index)]

    demand_idx = routing.RegisterUnaryTransitCallback(demand_cb)
    routing.AddDimensionWithVehicleCapacity(
        demand_idx, 0, [capacity] * num_vehicles, True, "Load")


# ---------------------------------------------------------------------------
# 4b. 配送件数の上限 … 「1台につき最大N地点」を強制する
#     Load 次元とは別に、配送先の訪問回数だけを累積する次元を持たせる。
# ---------------------------------------------------------------------------
def add_delivery_limit(routing, manager, model, num_vehicles, max_deliveries):
    def count_cb(from_index):
        return 1 if model.is_target(manager.IndexToNode(from_index)) else 0

    count_idx = routing.RegisterUnaryTransitCallback(count_cb)
    routing.AddDimensionWithVehicleCapacity(
        count_idx, 0, [max_deliveries] * num_vehicles, True, "Deliveries")


# ---------------------------------------------------------------------------
# 5. pickupユニットを任意訪問にする(使わない在庫は skip)
#    在庫上限は「pickupノードを stock 個しか作らない」ことで構造的に保証済み。
#    どの車両がどの pickup を使うかは自由(デポ所属の固定なし)。
# ---------------------------------------------------------------------------
def add_pickup_options(routing, manager, model):
    for pn in model.pickup_nodes:
        routing.AddDisjunction([manager.NodeToIndex(pn)], 0)  # penalty 0 = skip可


# ---------------------------------------------------------------------------
# 5b. 初期解の生成
#     「1台1地点」のように制約が厳しい設定では、OR-Tools の構築ヒューリスティクスが
#     初期解を作れず解なしになることがある。そこで
#     「各車両は自分の待機デポで積めるだけ積み、近い地点から配る」という自明な解を
#     こちらで組み立てて種として与える。
# ---------------------------------------------------------------------------
def build_initial_routes(model, vehicle_starts, move_min, per_vehicle):
    """車両 -> [pickupノード..., 配送先ノード...] の初期ルート集合を貪欲に作る。

    per_vehicle : 1台が初期解で受け持つ地点数の上限(積載量と配送上限の小さいほう)。
    作れない場合(台数 x per_vehicle < 配送先数 など)は None を返し、通常の探索に任せる。
    """
    if len(vehicle_starts) * per_vehicle < model.n_targets:
        return None

    # 待機デポごとの pickup ノード(在庫)
    by_depot = {}
    for pn in model.pickup_nodes:
        by_depot.setdefault(model.pickup_depot[pn], []).append(pn)

    # (所要時間, 車両, 配送先) を小さい順に貪欲割当。積載量と在庫が尽きたら打ち止め。
    pairs = []
    for k, sn in enumerate(model.start_nodes):
        for tn in range(model.n_targets):
            pairs.append((move_min(sn, tn), k, tn))
    pairs.sort()

    pickups = [[] for _ in vehicle_starts]
    drops = [[] for _ in vehicle_starts]
    used_t = set()
    for _, k, tn in pairs:
        if tn in used_t or len(drops[k]) >= per_vehicle:
            continue
        pool = by_depot.get(vehicle_starts[k])
        if not pool:
            continue
        pickups[k].append(pool.pop())
        drops[k].append(tn)
        used_t.add(tn)
    if len(used_t) < model.n_targets:
        return None

    # 各車両: まとめて積む -> 直前地点から最も近い地点へ順に配る
    routes = []
    for k in range(len(vehicle_starts)):
        order, here, rest = [], model.start_nodes[k], list(drops[k])
        while rest:
            nxt = min(rest, key=lambda tn: move_min(here, tn))
            order.append(nxt)
            rest.remove(nxt)
            here = nxt
        routes.append(pickups[k] + order)
    return routes


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
        start_name = None
        if model.start_depot:
            start_name = depots[model.start_depot[k]]["name"]
            steps.append(f"待機({start_name})")
        while not routing.IsEnd(idx):
            node = manager.IndexToNode(idx)
            if model.is_target(node):
                steps.append(f"配[{targets[node]['name']}]")
                deliveries += 1
            elif model.is_pickup(node):
                name = depots[model.pickup_depot[node]]["name"]
                # 同じデポでの連続積込は 1 ステップにまとめる(補(x)×n)
                if steps and steps[-1].startswith(f"補({name})"):
                    n = int(steps[-1].rpartition("×")[2]) + 1 if "×" in steps[-1] else 2
                    steps[-1] = f"補({name})×{n}"
                else:
                    steps.append(f"補({name})")
                depots_used.append(name)
            idx = solution.Value(routing.NextVar(idx))

        t_min = solution.Value(time_dim.CumulVar(routing.End(k))) / TIME_SCALE
        makespan = max(makespan, t_min)
        routes.append({
            "vehicle": k,
            "start_depot": start_name,
            "deliveries": deliveries,
            "depots_used": sorted(set(depots_used)),   # 結果的に使ったデポ
            "time_min": round(t_min, 1),
            "steps": steps,
        })
    return {"makespan_min": round(makespan, 1), "routes": routes}


# ---------------------------------------------------------------------------
# 司令塔: 上のコンポーネントを順に呼ぶだけ
# ---------------------------------------------------------------------------
def solve_distribution(targets, depots, num_vehicles, travel_time_min,
                       vehicle_starts, vehicle_capacity=1,
                       max_deliveries_per_vehicle=None,
                       service_min=0.0, reload_min=0.0, time_limit_sec=15):
    """
    travel_time_min : ((lat,lon),(lat,lon)) -> 分 の callable
        (道路ネットワーク上の実測所要時間)。

    vehicle_starts : [デポindex, ...] (長さ num_vehicles)。
        各車両の待機場所(=出発地点)。補給地点の選定結果から組み立てる。

    vehicle_capacity : int
        1台に積み込める機材の数(=1回の積込で配れる地点数)。

    max_deliveries_per_vehicle : int | None
        1台あたりの配送件数の上限(積み直しを含めた合計)。None なら上限なし。
        vehicle_capacity と同じ値を渡すと「1回の積込で配り切る(積み直しなし)」になる。

    戻り値: dict | None
        makespan_min : メイクスパン(分)
        routes       : 車両ごとの {vehicle, deliveries, depots_used, time_min, steps}
    """
    # 1. 検証
    check_stock(targets, depots)
    if len(vehicle_starts) != num_vehicles:
        raise ValueError(
            f"vehicle_starts の長さ {len(vehicle_starts)} が "
            f"num_vehicles {num_vehicles} と一致しません")
    if vehicle_capacity < 1:
        raise ValueError(f"積載量は 1 以上を指定してください: {vehicle_capacity}")
    # 上限 x 台数 が配送先数に届かなければ、そもそも配り切れない
    if (max_deliveries_per_vehicle is not None
            and max_deliveries_per_vehicle * num_vehicles < len(targets)):
        raise ValueError(
            f"台数不足: {num_vehicles}台 x 上限{max_deliveries_per_vehicle}件 "
            f"< 配送先 {len(targets)} 件")

    # 2. ノードモデル構築
    model = build_node_model(targets, depots, vehicle_starts)

    # 3. RoutingModel
    #    各車両は待機デポ発、帰着は共通garage(=帰路を評価しない)。
    manager = pywrapcp.RoutingIndexManager(
        len(model.coords), num_vehicles, list(model.start_nodes),
        [model.garage] * num_vehicles)
    routing = pywrapcp.RoutingModel(manager)

    # 4-6. 制約・目的
    time_dim = add_time_dimension(routing, manager, model,
                                  service_min, reload_min, travel_time_min)
    add_capacity_dimension(routing, manager, model, num_vehicles,
                           vehicle_capacity)
    if max_deliveries_per_vehicle is not None:
        add_delivery_limit(routing, manager, model, num_vehicles,
                           max_deliveries_per_vehicle)
    add_pickup_options(routing, manager, model)

    # 7. 探索(制約が厳しい場合に備え、自明な初期解を種として与える)
    params = build_search_params(time_limit_sec)
    initial = None
    per_vehicle = vehicle_capacity
    if max_deliveries_per_vehicle is not None:
        per_vehicle = min(per_vehicle, max_deliveries_per_vehicle)
    routes0 = build_initial_routes(model, vehicle_starts,
                                   make_move_min(model, travel_time_min),
                                   per_vehicle)
    if routes0 is not None:
        routing.CloseModelWithParameters(params)
        initial = routing.ReadAssignmentFromRoutes(routes0, True)

    if initial is not None:
        solution = routing.SolveFromAssignmentWithParameters(initial, params)
    else:
        solution = routing.SolveWithParameters(params)
    if solution is None:
        return None

    # 8. 解の取り出し
    return extract_solution(routing, manager, solution, time_dim,
                            model, targets, depots)
