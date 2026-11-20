# -*- coding: utf-8 -*-
"""シナリオ 1 本を実行して結果 dict を返す(GUI / CLI 共通)。

    1. 所要時間(補給地点候補 → 目的地)を取得
    2. 補給地点の選定(solver.py)… 直行前提で採用拠点の候補を出す
    3. 配分ルート最適化(routing.py)… 積載量を踏まえて各車両の順路を決める(任意)

ルート最適化が有効で params.routing.integrated が真(既定)なら、2 と 3 を一体で解く
(integrated.py): 選定の上位候補をルートで比べ、実際の所要時間が最も短い拠点の組を採用し、
各拠点の機材数もルートの結果から決める。integrated が偽なら従来どおり 2 → 3 を 1 回ずつ。
"""

import time

import numpy as np

from . import integrated as itg
from . import scenario as scn
from .routing import allocate_vehicles, solve_routes
from .solver import evaluate, solve
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
    unified = bool(rt.get("enabled") and rt.get("integrated", True))
    started = time.time()
    # 進捗の配分: 所要時間 → 選定 → (ルート用の所要時間 → ルート探索)
    sel_lo, sel_hi = ((0.2, 0.4) if unified else (0.2, 0.5)) if rt.get("enabled") else (0.3, 1.0)

    targets = [t for t in sc["targets"] if t.get("enabled", True)]
    cands = [c for c in sc["candidates"] if c["status"] != "excluded"]
    required = [i for i, c in enumerate(cands) if c["status"] == "required"]
    preferred = [i for i, c in enumerate(cands) if c["status"] == "preferred"]
    avoid = [i for i, c in enumerate(cands) if c["status"] == "avoid"]
    margin = float(p["preference_margin_min"])

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
                        preferred=preferred, avoid=avoid, margin=margin,
                        top=int(rt.get("screen_candidates", 8)) if unified and k == k_main else 1,
                        progress=_stage(progress, sel_lo + (sel_hi - sel_lo) * n / len(ks),
                                        sel_lo + (sel_hi - sel_lo) * (n + 1) / len(ks)),
                        cancel=cancel)
        if progress:
            progress(sel_lo + (sel_hi - sel_lo) * (n + 1) / len(ks), f"拠点数 {k} の計算が完了")
    main = runs[k_main]
    if main["method"] == "heuristic":
        warnings.append(f"組合せ数 {main['combinations']:,} が上限を超えたため近似解です"
                        "(max_exact_combinations を上げると厳密解になります)")

    # 2+3. 選定とルートを一体で解く
    plan = None
    if unified:
        plan = _run_integrated(p, cands, targets, cost, main["alternatives"], provider,
                               allow_fetch, preferred, avoid, margin, progress, cancel)
        if plan["route"] is None:
            warnings.append("ルート最適化で解が見つかりませんでした(探索時間を延ばしてください)。"
                            "選定は直行前提の結果です")
            plan = None
    subset = plan["subset"] if plan else main["subset"]
    d_mk, d_tot, d_owner, _ = evaluate(cost, subset)

    objective_primary = "makespan" if p["objective"] == "makespan" else "total"
    preference = None
    if preferred or avoid:
        if plan:
            route_mk = plan["route"]["makespan"]
            route_tot = sum(v["time_min"] for v in plan["route"]["vehicles"])
            chosen_primary = route_mk if objective_primary == "makespan" else route_tot
            best_primary = plan["best_primary"]
            used = _used_by_route(plan)
            n_pref_used = sum(u in preferred for u in used)
            n_avoid_used = sum(u in avoid for u in used)
        else:
            chosen_primary = main["makespan"] if objective_primary == "makespan" else main["total"]
            best_primary = main["best_primary"]
            n_pref_used, n_avoid_used = main["num_preferred"], main["num_avoided"]
        preference = {
            "margin_min": margin,
            "basis": "route" if plan else "direct",
            "best_min": round(best_primary, 1),
            "chosen_min": round(chosen_primary, 1),
            "delta_min": round(chosen_primary - best_primary, 1),
            "num_preferred": len(preferred), "num_preferred_used": n_pref_used,
            "num_avoid": len(avoid), "num_avoid_used": n_avoid_used,
        }

    # 3. 結果の整形
    if plan:
        depots, assignments, summary_times = _shape_integrated(plan, cands, targets, move, cost)
    else:
        depots, assignments = _shape_direct(subset, d_owner, cands, targets, move, cost,
                                            capacity, fleet)
        summary_times = None
    unused = [d["name"] for d in depots if d["stock"] == 0]
    if unused:
        warnings.append("担当する目的地が 0 件の補給地点があります: " + "、".join(unused))

    adopted = {cands[i]["id"] for i in subset}
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
    integration = None
    if plan:
        routing = _shape_routing(rt, plan["route"], [cands[i] for i in plan["subset"]], targets)
        integration = _shape_integration(plan, cands, rt, objective_primary)
        if integration["chosen_rank"] > 1:
            warnings.append(
                f"直行前提では {integration['chosen_rank']} 番目の拠点の組合せを、実際のルートで比べて採用しました"
                f"(直行前提の 1 位の組合せはルートで最長 {integration['screened'][0]['route_makespan_min']} 分)")
    elif rt.get("enabled") and not unified:
        routing = _run_routing(p, cands, targets, depots, provider, allow_fetch,
                               progress, cancel)
        if routing is None:
            warnings.append("ルート最適化で解が見つかりませんでした(探索時間を延ばしてください)")

    n = len(targets)
    if summary_times:
        mk_min, tot_min = summary_times
    else:
        mk_min, tot_min = d_mk, d_tot
    return {
        "scenario_name": sc["name"],
        "run_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_sec": round(time.time() - started, 2),
        "provider": provider,
        "fetched_pairs": fetched,
        "params": p,
        "summary": {
            "makespan_min": round(mk_min, 1),
            "total_min": round(tot_min, 1),
            "avg_min": round(tot_min / n, 1),
            "basis": "route" if plan else "direct",
            "direct_makespan_min": round(d_mk, 1),
            "direct_total_min": round(d_tot, 1),
            "integrated": bool(plan),
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
        "preference": preference,
        "routing": routing,
        "integration": integration,
        "warnings": warnings,
    }


def _shape_direct(subset, owner, cands, targets, move, cost, capacity, fleet):
    """直行前提の担当(各目的地 → 最寄りの採用拠点)から depots / assignments を作る。"""
    by_depot = {i: [] for i in subset}
    assignments = []
    for j, t in enumerate(targets):
        i = int(owner[j])
        by_depot[i].append(j)
        assignments.append({
            "target_id": t["id"], "target": t["name"], "lat": t["lat"], "lon": t["lon"],
            "depot_id": cands[i]["id"], "depot": cands[i]["name"],
            "travel_min": round(float(move[i, j]), 1),
            "time_min": round(float(cost[i, j]), 1),
        })
    assignments.sort(key=lambda a: -a["time_min"])
    alloc = dict(zip(subset, allocate_vehicles([len(by_depot[i]) for i in subset], capacity, fleet)))
    depots = []
    for i in subset:
        js = sorted(by_depot[i], key=lambda j: -cost[i, j])
        depots.append(_depot_row(cands[i], len(js), alloc[i],
                                 max((cost[i, j] for j in js), default=0.0),
                                 [targets[j] for j in js]))
    return depots, assignments


def _depot_row(c, stock, vehicles, max_min, ts):
    return {
        "id": c["id"], "name": c["name"], "lat": c["lat"], "lon": c["lon"],
        "address": c.get("address", ""), "group": c.get("group", ""),
        "note": c.get("note", ""), "status": c["status"],
        "stock": stock, "vehicles": vehicles,
        "max_min": round(float(max_min), 1),
        "targets": [t["name"] for t in ts],
        "target_ids": [t["id"] for t in ts],
    }


def _route_sources(route):
    """ルート結果から {目的地 j: (積んだ拠点の subset 内位置, 配送完了[分])}。"""
    out = {}
    for v in route["vehicles"]:
        cur = []          # 積んでいる機材の出どころ(拠点)。配るときは先に積んだものから
        for s in v["stops"]:
            if s["kind"] == "pickup":
                cur.extend([s["depot"]] * s["count"])
            else:
                out[s["target"]] = (cur.pop(0) if cur else v["start"], s["done_min"])
    return out


def _used_by_route(plan):
    picked = itg.picked_per_depot(plan["route"], len(plan["subset"]))
    return [plan["subset"][i] for i, c in enumerate(picked) if c > 0]


def _shape_integrated(plan, cands, targets, move, cost):
    """一体計算の結果から depots / assignments と (最長, 総所要) を作る。"""
    subset, route = plan["subset"], plan["route"]
    src = _route_sources(route)
    by_pos = {pos: [] for pos in range(len(subset))}
    assignments = []
    for j, t in enumerate(targets):
        pos, done = src[j]
        i = subset[pos]
        by_pos[pos].append((j, done))
        assignments.append({
            "target_id": t["id"], "target": t["name"], "lat": t["lat"], "lon": t["lon"],
            "depot_id": cands[i]["id"], "depot": cands[i]["name"],
            "travel_min": round(float(move[i, j]), 1),
            "time_min": round(float(done), 1),
        })
    assignments.sort(key=lambda a: -a["time_min"])
    starts = [0] * len(subset)
    for v in route["vehicles"]:
        starts[v["start"]] += 1
    depots = []
    for pos, i in enumerate(subset):
        rows = sorted(by_pos[pos], key=lambda x: -x[1])
        depots.append(_depot_row(cands[i], len(rows), starts[pos],
                                 max((d for _, d in rows), default=0.0),
                                 [targets[j] for j, _ in rows]))
    total = sum(v["time_min"] for v in route["vehicles"])
    return depots, assignments, (route["makespan"], total)


def _shape_integration(plan, cands, rt, objective):
    names = lambda sub: [cands[i]["name"] for i in sub]
    return {
        "screen_candidates": int(rt.get("screen_candidates", 8)),
        "screen_time_sec": int(rt.get("screen_time_sec", 2)),
        "skipped": plan["skipped"],
        "chosen_rank": plan["chosen_rank"],
        "objective": objective,
        "stock_mode": plan["route"]["stock_mode"],
        "screened": [{
            "rank": r["rank"], "depots": names(r["subset"]),
            "depot_ids": [cands[i]["id"] for i in r["subset"]],
            "direct_makespan_min": round(r["direct_makespan"], 1),
            "direct_total_min": round(r["direct_total"], 1),
            "route_makespan_min": round(r["route_makespan"], 1) if "route_makespan" in r else None,
            "route_total_min": round(r["route_total"], 1) if "route_total" in r else None,
            "stock_mode": r.get("stock_mode"),
            "chosen": r["rank"] == plan["chosen_rank"],
        } for r in plan["screened"]],
    }


def _run_integrated(p, cands, targets, cost, alternatives, provider, allow_fetch,
                    preferred, avoid, margin, progress, cancel):
    rt = p["routing"]
    points = [(c["lat"], c["lon"]) for c in cands] + [(t["lat"], t["lon"]) for t in targets]
    if progress:
        progress(0.4, "ルート用の所要時間を準備中")
    tmat, _, _ = build_matrix(points, points, profile=p["profile"], provider=provider,
                              allow_fetch=allow_fetch, progress=_stage(progress, 0.4, 0.45))
    return itg.plan(np.asarray(tmat, dtype=float), cost, alternatives,
                    objective=p["objective"], capacity=int(rt["vehicle_capacity"]),
                    max_deliveries=rt.get("max_deliveries"), num_vehicles=rt.get("num_vehicles"),
                    service_min=float(p["service_min"]), reload_min=float(p["reload_min"]),
                    time_limit_sec=int(rt["time_limit_sec"]),
                    screen_time_sec=int(rt.get("screen_time_sec", 2)),
                    preferred=preferred, avoid=avoid, margin=margin,
                    progress=_stage(progress, 0.45, 1.0), cancel=cancel)


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
    return _shape_routing(rt, res, used, targets, integrated=False)


def _shape_routing(rt, res, used, targets, integrated=True):
    """solve_routes の戻り値を結果 dict に整形する。used は res の depot 番号に対応する拠点。"""

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
        "integrated": integrated,
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


def preference_text(pf, objective="makespan"):
    """優先 / なるべく避ける の反映状況を 1 文で表す。"""
    what = "最長所要" if objective == "makespan" else "総所要"
    head = (f"優先 {pf['num_preferred_used']}/{pf['num_preferred']} 件・"
            f"なるべく避ける {pf['num_avoid_used']}/{pf['num_avoid']} 件を採用"
            f"(許容幅 +{pf['margin_min']:g} 分)。")
    if pf["delta_min"] > 0:
        return (head + f"優先などを考慮しない最良は{what} {pf['best_min']} 分で、"
                f"考慮したことで +{pf['delta_min']} 分の {pf['chosen_min']} 分になった。")
    return head + f"優先などを考慮しない場合と{what}は同じ({pf['chosen_min']} 分)。"


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
        ("=== 1. 補給地点の選定(ルートと一体で計算) ===" if s.get("integrated") else
         "=== 1. 補給地点の選定(各目的地へ直行する前提) ==="),
        (f"最長所要 {s['makespan_min']} 分 / 総所要 {s['total_min']} 分 / 平均 {s['avg_min']} 分"
         f"(ルート最適化後。直行なら最長 {s['direct_makespan_min']} 分)" if s.get("integrated") else
         f"最長所要 {s['makespan_min']} 分 / 総所要 {s['total_min']} 分 / 平均 {s['avg_min']} 分"),
        f"(拠点数を増やしても最長所要は {s['lower_bound_min']} 分未満にならない: "
        f"{s['bottleneck_target']} がボトルネック)",
    ]
    pf = result.get("preference")
    if pf:
        lines.append(preference_text(pf, p["objective"]))
    lines += [
        "",
        "--- 採用した補給地点 ---",
    ]
    for d in result["depots"]:
        lines.append(f"[{d['id']}] {d['name']}  機材 {d['stock']} 個 / 車両 {d['vehicles']} 台"
                     f"  最長 {d['max_min']} 分")
        lines.append("     担当: " + ", ".join(d["targets"]))
    ig = result.get("integration")
    if ig and ig["screened"]:
        lines += ["", f"--- ルートで比べた拠点の組合せ(各 {ig['screen_time_sec']} 秒の試算) ---"]
        for r in ig["screened"]:
            rm = "解なし" if r["route_makespan_min"] is None else f"{r['route_makespan_min']} 分"
            lines.append(f"  {'★' if r['chosen'] else ' '} 直行 {r['rank']} 位 {r['direct_makespan_min']:>6} 分"
                         f" → ルート {rm}  " + " / ".join(r["depots"]))
    elif ig and ig.get("skipped"):
        lines += ["", f"(ルートでの比較は省略: {ig['skipped']})"]
    if len(result["sensitivity"]) > 1:
        lines += ["", "--- 拠点数ごとの最長所要(直行前提。括弧内は機材数) ---"]
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
