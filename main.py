# -*- coding: utf-8 -*-
"""エントリポイント。

処理は 2 段構え:
    1. 補給地点の選定  … 候補リストから最良 5 地点を選び、車両台数を配分する
    2. 配分ルート最適化 … 選定結果を前提に、各車両の順路を確定する

実行:
    uv run main.py                 # 積載量は VEHICLE_CAPACITY(既定 1)
    uv run main.py --capacity 3    # 1台に機材 3 個まで積める場合
"""

import argparse
import math
import sys

from waypoints import waypoints, candidate_supply_points, NUM_DEPOTS_TO_SELECT
from depot_selection import select_depots
from distribute_solver import solve_distribution
from travel_time import build_travel_time

# 共通パラメータ(選定とルート最適化で同じ値を使う)
SERVICE_MIN = 10      # 1 地点での作業時間
RELOAD_MIN = 5        # 補給地点での積込時間(機材 1 個あたり)

# 1台に積み込める機材の数(= 1回の積込で配れる地点数)。--capacity で上書き可。
VEHICLE_CAPACITY = 1
# 1台あたりの配送件数の上限。None なら積載量と同じ(1回の積込で配り切り、積み直さない)。
# 積み直しを許して往復させたい場合は大きな値を入れる。
MAX_DELIVERIES_PER_VEHICLE = None


def build_inputs():
    """waypoints / 補給地点候補からソルバー入力を組み立てる。"""
    targets = [{"name": w["name"], "lat": w["lat"], "lon": w["lon"]}
               for w in waypoints if w["group"] == 1]
    return targets, list(candidate_supply_points)


def vehicles_needed(stock, capacity):
    """機材 stock 個を積載量 capacity の車両で運ぶのに要る台数。"""
    return math.ceil(stock / capacity)


def build_vehicle_starts(depots, capacity):
    """各車両の出発地点(デポindex)を割り当てる。

    デポ i には機材が stock 個置かれ、それを 1 回で積み切れる台数
    ceil(stock / capacity) の車両が待機している状態とする。
    capacity=1 なら従来どおり「1局1台」(車両数 = 地点数)。
    """
    starts = []
    for di, d in enumerate(depots):
        starts.extend([di] * vehicles_needed(d["stock"], capacity))
    return starts


def run_selection(targets, candidates, travel_time_min):
    """候補から最良 NUM_DEPOTS_TO_SELECT 地点を選び、台数を配分する。"""
    return select_depots(
        targets, candidates, travel_time_min,
        num_depots=NUM_DEPOTS_TO_SELECT,
        service_min=SERVICE_MIN, reload_min=RELOAD_MIN)


def print_selection(sel, capacity):
    n_vehicles = sum(vehicles_needed(d["stock"], capacity) for d in sel["depots"])
    print("=== 補給地点の選定結果 ===")
    print(f"候補 {len(candidate_supply_points)} セット中 "
          f"{len(sel['depots'])} 地点を採用 / 機材 {sel['num_vehicles']} 個 / "
          f"車両 {n_vehicles} 台(積載量 {capacity})\n")
    for d in sel["depots"]:
        print(f"[{d['code']}] {d['name']}  機材 {d['stock']} 個 / "
              f"車両 {vehicles_needed(d['stock'], capacity)} 台  "
              f"({d['kind']} / {d['area']})")
        print(f"     {d.get('address', '(住所未登録)')}")
        # 追加候補は駅・レンタカーの動線が未確認
        print(f"     駅: {d.get('station', '要確認')} / "
              f"{d.get('rental', 'レンタカー要確認')}")
    print(f"\n選定時点のメイクスパン: {sel['makespan_min']} 分 "
          f"(総所要 {sel['total_min']} 分)")
    if capacity > 1:
        print("  ※選定は各地点へ拠点から直行する前提の評価。複数地点を回る分は"
              "下のルート最適化で反映される。")
    print()

    print("--- 地点ごとの担当拠点 ---")
    for a in sorted(sel["assignments"], key=lambda x: -x["time_min"]):
        print(f"  点{a['target']:>3} <- {a['depot']}  {a['time_min']} 分")
    print()


def print_result(result):
    if result is None:
        print("解なし(条件を確認してください)")
        return
    print("=== 配分ルート最適化 ===")
    print(f"メイクスパン: {result['makespan_min']} 分\n")
    for r in result["routes"]:
        used = ",".join(r["depots_used"]) if r["depots_used"] else "-"
        start = r.get("start_depot") or "-"
        print(f"[車両{r['vehicle']}] 出発={start} 配送={r['deliveries']}件 "
              f"使用デポ={used} 所要={r['time_min']}分")
        print("   " + (" -> ".join(r["steps"]) if r["steps"] else "(未使用)"))


def parse_args():
    ap = argparse.ArgumentParser(description="補給地点の選定と配分ルート最適化")
    ap.add_argument("--capacity", type=int, default=VEHICLE_CAPACITY, metavar="N",
                    help=f"1台に積み込める機材の数(既定 {VEHICLE_CAPACITY})")
    ap.add_argument("--max-deliveries", type=int,
                    default=MAX_DELIVERIES_PER_VEHICLE, metavar="N",
                    help="1台あたりの配送件数の上限(既定: 積載量と同じ=積み直しなし)")
    ap.add_argument("--time-limit", type=int, default=60, metavar="SEC",
                    help="ルート最適化の探索時間[秒](既定 60)")
    args = ap.parse_args()
    if args.capacity < 1:
        ap.error("--capacity は 1 以上を指定してください")
    if args.max_deliveries is None:
        args.max_deliveries = args.capacity
    return args


def main():
    args = parse_args()

    # Windowsコンソールの文字化け対策(UTF-8で出力)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    targets, candidates = build_inputs()

    # 実測ベースの移動時間を取得(道路ネットワーク上の運転時間)
    travel_time_min = build_travel_time(targets, candidates, profile="driving")
    print(f"移動時間の取得元: {getattr(travel_time_min, 'provider', '?')}\n")

    # 1. 候補から最良 K 地点を選定し、各拠点に置く機材の数を決める
    sel = run_selection(targets, candidates, travel_time_min)
    if sel is None:
        print("補給地点を選定できませんでした(条件を確認してください)")
        return
    print_selection(sel, args.capacity)

    # 2. 選定結果を前提にルートを確定する。車両は積載量に応じて各拠点に待機。
    depots = sel["depots"]
    vehicle_starts = build_vehicle_starts(depots, args.capacity)

    result = solve_distribution(
        targets, depots, num_vehicles=len(vehicle_starts),
        travel_time_min=travel_time_min,
        vehicle_starts=vehicle_starts,
        vehicle_capacity=args.capacity,
        max_deliveries_per_vehicle=args.max_deliveries,
        service_min=SERVICE_MIN, reload_min=RELOAD_MIN,
        time_limit_sec=args.time_limit)

    print_result(result)


if __name__ == "__main__":
    main()
