# -*- coding: utf-8 -*-
"""エントリポイント。

実行:
    uv run main.py
"""

import sys

from waypoints import waypoints, supply_points
from distribute_solver import solve_distribution
from mapbox import build_travel_time


def build_inputs():
    """waypoints / supply_points からソルバー入力を組み立てる。"""
    targets = [{"name": w["name"], "lat": w["lat"], "lon": w["lon"]}
               for w in waypoints if w["group"] == 1]
    depots = [{"name": s["name"], "lat": s["lat"], "lon": s["lon"], "stock": s["stock"]}
              for s in supply_points]
    return targets, depots


def print_result(result):
    if result is None:
        print("解なし(条件を確認してください)")
        return
    print(f"メイクスパン: {result['makespan_min']} 分\n")
    for r in result["routes"]:
        used = ",".join(r["depots_used"]) if r["depots_used"] else "-"
        print(f"[車両{r['vehicle']}] 配送={r['deliveries']}件 "
              f"使用デポ={used} 所要={r['time_min']}分")
        print("   " + (" -> ".join(r["steps"]) if r["steps"] else "(未使用)"))


def main():
    # Windowsコンソールの文字化け対策(UTF-8で出力)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    targets, depots = build_inputs()

    # Mapbox Matrix API から実測ベースの移動時間を取得(道路ネットワーク上の運転時間)
    travel_time_min = build_travel_time(targets, depots, profile="driving")

    # 台数はデポ数と無関係に指定できる(例: 6台)
    result = solve_distribution(
        targets, depots, num_vehicles=10,
        speed_kmph=40, service_min=10, reload_min=5, time_limit_sec=15,
        travel_time_min=travel_time_min)

    print_result(result)


if __name__ == "__main__":
    main()
