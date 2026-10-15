# -*- coding: utf-8 -*-
"""全33地点を対象にした補給地点選定シミュレーション(K=7)。

のぼり／くだりでパスを分けず、点1-33 をまとめて 1 つの対象集合として
補給地点 7 地点を選ぶ。

実行:
    uv run simulate_all33.py
"""

import sys

from waypoints import (waypoints, candidate_supply_points, targets_for,
                       NUM_DEPOTS_TO_SELECT)
from depot_selection import select_depots
from travel_time import build_travel_time
from main import SERVICE_MIN, RELOAD_MIN

K_RANGE = range(3, 11)

# 候補から外す拠点(make_report_all33.py と揃えること)
# W3 = 国立女性教育会館
# E7/E8 = 江戸川河川事務所、A1-A3 = 荒川上流河川事務所(利用不可。利根川上流は可)
EXCLUDE_CODES = {"W3", "E7", "E8", "A1", "A2", "A3"}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    all_candidates = list(candidate_supply_points)
    targets = targets_for([w["id"] for w in waypoints])
    # キャッシュを使い回すため、移動時間は除外前の全候補で構築する
    travel_time_min = build_travel_time(targets, all_candidates, profile="driving")
    candidates = [c for c in all_candidates if c["code"] not in EXCLUDE_CODES]
    print(f"移動時間の取得元: {getattr(travel_time_min, 'provider', '?')}")
    print(f"対象 {len(targets)} 地点 / 候補 {len(candidates)} 件 "
          f"(除外 {sorted(EXCLUDE_CODES)})")

    print("\n--- 採用地点数ごとのメイクスパン ---")
    for k in K_RANGE:
        s = select_depots(targets, candidates, travel_time_min, num_depots=k,
                          service_min=SERVICE_MIN, reload_min=RELOAD_MIN)
        mark = " ★採用" if k == NUM_DEPOTS_TO_SELECT else ""
        print(f"  K={k}: {s['makespan_min']:>5} 分 (総 {s['total_min']} 分)  "
              + " / ".join(f"{d['code']}:{d['name']}({d['stock']})" for d in s["depots"]) + mark)

    sel = select_depots(targets, candidates, travel_time_min,
                        num_depots=NUM_DEPOTS_TO_SELECT,
                        service_min=SERVICE_MIN, reload_min=RELOAD_MIN)
    by_depot = {}
    for a in sel["assignments"]:
        by_depot.setdefault(a["depot"], []).append(a)

    print(f"\n--- K={NUM_DEPOTS_TO_SELECT} の採用拠点 ---")
    for d in sel["depots"]:
        rows = sorted(by_depot[d["name"]], key=lambda x: -x["time_min"])
        print(f"[{d['code']}] {d['name']} ({d['kind']}/{d['area']}) 車両 {d['stock']} 台 "
              f"最長 {rows[0]['time_min']} 分")
        print(f"     担当: " + ", ".join(f"{r['target']}({r['time_min']})" for r in rows))
        print(f"     所在地: {d.get('address', '(未登録)')}")
        print(f"     駅: {d.get('station', '要確認')}")
        print(f"     レンタカー: {d.get('rental', '要確認')}")
    print(f"\nメイクスパン {sel['makespan_min']} 分 / 総所要 {sel['total_min']} 分 "
          f"/ 車両 {sel['num_vehicles']} 台 / 平均 {round(sel['total_min']/sel['num_vehicles'],1)} 分")
    print("--- 地点ごとの担当拠点(遅い順) ---")
    for a in sorted(sel["assignments"], key=lambda x: -x["time_min"]):
        print(f"  点{a['target']:>3} <- {a['depot']}  {a['time_min']} 分")


if __name__ == "__main__":
    main()
