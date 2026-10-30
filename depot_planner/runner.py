# -*- coding: utf-8 -*-
"""シナリオ 1 本を実行して結果 dict を返す(GUI / CLI 共通)。

    1. 所要時間(補給地点候補 → 目的地)を取得
    2. 補給地点の選定(solver.py)… 採用拠点と、各拠点に置く機材の数を決める
    3. 配分ルート最適化(routing.py)… 積載量を踏まえて各車両の順路を決める(任意)
"""

import time

import numpy as np

from . import scenario as scn
from .routing import allocate_vehicles, solve_routes
from .solver import solve
from .travel_times import UNREACHABLE, build_matrix


class ScenarioError(ValueError):
    pass


def _stage(progress, lo, hi):
    """全体進捗 [lo, hi] の区間に写像する progress を作る。"""
    if not progress:
        return None
    return lambda frac, msg: progress(lo + (hi - lo) * max(0.0, min(frac, 1.0)), msg)


def run_scenario(sc, progress=None, cancel=None, allow_fetch=True):
    sc = scn.normalize(sc)
    errors = scn.validate(sc)
    if errors:
        raise ScenarioError("\n".join(errors))
    p = sc["params"]
    rt = p["routing"]
    capacity = int(rt["vehicle_capacity"]) if rt.get("enabled") else 1
    fleet = rt.get("num_vehicles") if rt.get("enabled") else None   # None = 台数自動
    started = time.time()
    # 進捗の配分: 所要時間 → 選定 → (ルート用の所要時間 → ルート探索)
    sel_lo, sel_hi = (0.2, 0.5) if rt.get("enabled") else (0.3, 1.0)

    targets = [t for t in sc["targets"] if t.get("enabled", True)]
    cands = [c for c in sc["candidates"] if c["status"] != "excluded"]
    required = [i for i, c in enumerate(cands) if c["status"] == "required"]

    # 1. 所要時間
    if progress:
        progress(0.0, "所要時間を準備中")
    move, provider, fetched = build_matrix(
        [(c["lat"], c["lon"]) for c in cands], [(t["lat"], t["lon"]) for t in targets],
        profile=p["profile"], provider=p["provider"], allow_fetch=allow_fetch,
        progress=_stage(progress, 0.0, sel_lo))
    cost = move + float(p["reload_min"]) + float(p["service_min"])
    cost[move >= UNREACHABLE] = UNREACHABLE

    warnings = []
    lb_per_target = cost.min(axis=0)
    unreachable = [targets[j]["name"] for j in np.where(lb_per_target >= UNREACHABLE)[0]]
    if unreachable:
        raise ScenarioError("どの補給地点からも到達できない目的地があります: " + "、".join(unreachable))
    bottleneck = int(lb_per_target.argmax())

    # 2. 感度分析の K 一覧(本計算の K を必ず含める)
    k_main = p["num_depots"]
    ks = [k_main]
    sens = p["sensitivity"]
    if sens.get("enabled"):
        lo = max(int(sens.get("min", 1)), len(required), 1)
        hi = min(int(sens.get("max", k_main)), len(cands))
        ks = sorted(set(range(lo, hi + 1)) | {k_main})

    runs = {}
    for n, k in enumerate(ks):
        runs[k] = solve(cost, k, required=required, objective=p["objective"],
                        max_exact=int(p["max_exact_combinations"]),
                        progress=_stage(progress, sel_lo + (sel_hi - sel_lo) * n / len(ks),
                                        sel_lo + (sel_hi - sel_lo) * (n + 1) / len(ks)),
                        cancel=cancel)
        if progress:
            progress(sel_lo + (sel_hi - sel_lo) * (n + 1) / len(ks), f"拠点数 {k} の計算が完了")
    main = runs[k_main]
    if main["method"] == "heuristic":
        warnings.append(f"組合せ数 {main['combinations']:,} が上限を超えたため近似解です"
                        "(max_exact_combinations を上げると厳密解になります)")

    # 3. 結果の整形
    by_depot = {i: [] for i in main["subset"]}
    assignments = []
    for j, t in enumerate(targets):
        i = main["owner"][j]
        by_depot[i].append(j)
        assignments.append({
            "target_id": t["id"], "target": t["name"], "lat": t["lat"], "lon": t["lon"],
            "depot_id": cands[i]["id"], "depot": cands[i]["name"],
            "travel_min": round(float(move[i, j]), 1),
            "time_min": round(float(cost[i, j]), 1),
        })
    assignments.sort(key=lambda a: -a["time_min"])

    alloc = dict(zip(main["subset"], allocate_vehicles(
        [len(by_depot[i]) for i in main["subset"]], capacity, fleet)))
    depots = []
    for i in main["subset"]:
        c = cands[i]
        js = sorted(by_depot[i], key=lambda j: -cost[i, j])
        depots.append({
            "id": c["id"], "name": c["name"], "lat": c["lat"], "lon": c["lon"],
            "address": c.get("address", ""), "group": c.get("group", ""),
            "note": c.get("note", ""), "status": c["status"],
            "stock": len(js),
            "vehicles": alloc[i],
            "max_min": round(float(max((cost[i, j] for j in js), default=0.0)), 1),
            "targets": [targets[j]["name"] for j in js],
            "target_ids": [targets[j]["id"] for j in js],
        })
    unused = [d["name"] for d in depots if d["stock"] == 0]
    if unused:
        warnings.append("担当する目的地が 0 件の補給地点があります: " + "、".join(unused))

    adopted = {cands[i]["id"] for i in main["subset"]}
    candidates = [{
        "id": c["id"], "name": c["name"], "group": c.get("group", ""),
        "lat": c["lat"], "lon": c["lon"], "address": c.get("address", ""),
        "status": c["status"],
        "result": "除外" if c["status"] == "excluded" else ("採用" if c["id"] in adopted else "-"),
    } for c in sc["candidates"]]

    def sens_depots(r):
        stocks = [r["owner"].count(i) for i in r["subset"]]
        return [{"id": cands[i]["id"], "name": cands[i]["name"], "stock": st, "vehicles": nv}
                for i, st, nv in zip(r["subset"], stocks, allocate_vehicles(stocks, capacity, fleet))]

    sensitivity = [{
        "k": k, "makespan_min": round(r["makespan"], 1), "total_min": round(r["total"], 1),
        "method": r["method"], "depots": sens_depots(r),
    } for k, r in sorted(runs.items())]

    routing = None
    if rt.get("enabled"):
        routing = _run_routing(p, cands, targets, depots, provider, allow_fetch,
                               progress, cancel)
        if routing is None:
            warnings.append("ルート最適化で解が見つかりませんでした(探索時間を延ばしてください)")

    n = len(targets)
    return {
        "scenario_name": sc["name"],
        "run_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_sec": round(time.time() - started, 2),
        "provider": provider,
        "fetched_pairs": fetched,
        "params": p,
        "summary": {
            "makespan_min": round(main["makespan"], 1),
            "total_min": round(main["total"], 1),
            "avg_min": round(main["total"] / n, 1),
            "num_targets": n,
            "num_vehicles": sum(d["vehicles"] for d in depots),
            "vehicle_capacity": capacity,
            "fixed_fleet": fleet is not None,
            "num_depots": k_main,
            "num_candidates": len(cands),
            "num_excluded": len(sc["candidates"]) - len(cands),
            "method": main["method"],
            "combinations": main["combinations"],
            "lower_bound_min": round(float(lb_per_target.max()), 1),
            "bottleneck_target": targets[bottleneck]["name"],
        },
        "depots": depots,
        "assignments": assignments,
        "candidates": candidates,
        "sensitivity": sensitivity,
        "routing": routing,
        "warnings": warnings,
    }


def _run_routing(p, cands, targets, depots, provider, allow_fetch, progress, cancel):
    """採用拠点の機材数を前提に、各車両の順路を求めて結果 dict に整形する。"""
    rt = p["routing"]
    used = [d for d in depots if d["stock"] > 0]
    points = ([(d["lat"], d["lon"]) for d in used]
              + [(t["lat"], t["lon"]) for t in targets])
    # ルートには 目的地→目的地・目的地→補給地点 の移動も要るので全地点対で取る
    if progress:
        progress(0.5, "ルート用の所要時間を準備中")
    tmat, _, _ = build_matrix(points, points, profile=p["profile"], provider=provider,
                              allow_fetch=allow_fetch, progress=_stage(progress, 0.5, 0.55))
    res = solve_routes(tmat, [d["stock"] for d in used],
                       capacity=int(rt["vehicle_capacity"]),
                       max_deliveries=rt.get("max_deliveries"),
                       num_vehicles=rt.get("num_vehicles"),
                       service_min=float(p["service_min"]), reload_min=float(p["reload_min"]),
                       time_limit_sec=int(rt["time_limit_sec"]),
                       progress=_stage(progress, 0.55, 1.0), cancel=cancel)
    if res is None:
        return None

    def stop(s):
        if s["kind"] == "pickup":
            d = used[s["depot"]]
            return {"kind": "pickup", "id": d["id"], "name": d["name"], "lat": d["lat"],
                    "lon": d["lon"], "count": s["count"], "done_min": s["done_min"]}
        t = targets[s["target"]]
        return {"kind": "deliver", "id": t["id"], "name": t["name"], "lat": t["lat"],
                "lon": t["lon"], "done_min": s["done_min"]}

    # 待機拠点の順(拠点内は所要の長い順)に 1 から番号を振る
    vehicles = []
    ordered = sorted(res["vehicles"], key=lambda v: (v["start"], -v["time_min"]))
    for no, v in enumerate(ordered, 1):
        d = used[v["start"]]
        vehicles.append({"no": no, "start_id": d["id"], "start": d["name"],
                         "start_lat": d["lat"], "start_lon": d["lon"],
                         "time_min": v["time_min"], "deliveries": v["deliveries"],
                         "stops": [stop(s) for s in v["stops"]]})
    return {
        "vehicle_capacity": int(rt["vehicle_capacity"]),
        "max_deliveries": int(res["max_deliveries"]),
        "num_vehicles": res["num_vehicles"],
        "fixed_fleet": rt.get("num_vehicles") is not None,
        "makespan_min": res["makespan"],
        "total_min": round(sum(v["time_min"] for v in vehicles), 1),
        "time_limit_sec": int(rt["time_limit_sec"]),
        "elapsed_sec": res["elapsed_sec"],
        "vehicles": vehicles,
    }


def route_text(v):
    """車両 1 台の順路を 1 行で表す(待機 → 補(x)×n → 配[y] ...)。"""
    parts = [f"待機({v['start']})"]
    for s in v["stops"]:
        if s["kind"] == "pickup":
            parts.append(f"補({s['name']})" + (f"×{s['count']}" if s["count"] > 1 else ""))
        else:
            parts.append(f"配[{s['name']}]")
    return " → ".join(parts)


def format_text(result):
    """CLI 表示用のテキスト。"""
    s = result["summary"]
    p = result["params"]
    rt = result.get("routing")
    lines = [
        f"シナリオ: {result['scenario_name']}  ({result['run_at']})",
        f"所要時間: {result['provider']} / {p['profile']}  "
        f"作業 {p['service_min']} 分・積込 {p['reload_min']} 分  目的関数 {p['objective']}",
        f"目的地 {s['num_targets']} 件 / 選定対象 {s['num_candidates']} 件"
        f"(除外 {s['num_excluded']} 件)から {s['num_depots']} 拠点を採用"
        f"  [{'厳密解' if s['method'] == 'exact' else '近似解'}]",
        f"積載量 {s['vehicle_capacity']} 個/台 → 車両 {s['num_vehicles']} 台"
        + ("(総数を指定)" if s.get("fixed_fleet") else "(1 回で積み切れる台数)"),
        "",
        "=== 1. 補給地点の選定(各目的地へ直行する前提) ===",
        f"最長所要 {s['makespan_min']} 分 / 総所要 {s['total_min']} 分 / 平均 {s['avg_min']} 分",
        f"(拠点数を増やしても最長所要は {s['lower_bound_min']} 分未満にならない: "
        f"{s['bottleneck_target']} がボトルネック)",
        "",
        "--- 採用した補給地点 ---",
    ]
    for d in result["depots"]:
        lines.append(f"[{d['id']}] {d['name']}  機材 {d['stock']} 個 / 車両 {d['vehicles']} 台"
                     f"  最長 {d['max_min']} 分")
        lines.append("     担当: " + ", ".join(d["targets"]))
    if len(result["sensitivity"]) > 1:
        lines += ["", "--- 拠点数ごとの最長所要(括弧内は機材数) ---"]
        for r in result["sensitivity"]:
            mark = " ★" if r["k"] == s["num_depots"] else ""
            lines.append(f"  K={r['k']:>2}: {r['makespan_min']:>6} 分 (総 {r['total_min']})  "
                         + " / ".join(f"{d['name']}({d['stock']})" for d in r["depots"]) + mark)
    lines += ["", "--- 目的地ごとの担当(遅い順) ---"]
    for a in result["assignments"]:
        lines.append(f"  {a['target']} <- {a['depot']}  {a['time_min']} 分")
    if rt:
        lines += ["", "=== 2. 配分ルート最適化 ===",
                  f"最長所要 {rt['makespan_min']} 分 / 車両 {rt['num_vehicles']} 台"
                  f"(積載量 {rt['vehicle_capacity']}・1 台最大 {rt['max_deliveries']} 件)"
                  f"  探索 {rt['elapsed_sec']} 秒"]
        for v in rt["vehicles"]:
            lines.append(f"[車両{v['no']:>2}] 配送 {v['deliveries']} 件  所要 {v['time_min']} 分")
            lines.append("     " + route_text(v))
    for w in result["warnings"]:
        lines.append(f"[注意] {w}")
    return "\n".join(lines)
