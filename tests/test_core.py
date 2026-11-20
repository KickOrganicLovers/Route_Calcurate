# -*- coding: utf-8 -*-
"""ソルバー・シナリオ・API のテスト(ネットワークには出ない)。"""

from itertools import combinations

import numpy as np
import pytest
from fastapi.testclient import TestClient

from depot_planner import config, routing, runner, server, solver
from depot_planner import scenario as scn


def _brute(cost, k, pool, required=(), objective="makespan"):
    best = None
    for sub in combinations([i for i in pool if i not in required], k - len(required)):
        s = list(required) + list(sub)
        key = solver._key(cost, s, objective)
        if best is None or key < best[0]:
            best = (key, s)
    return best[0]


@pytest.fixture
def cost():
    rng = np.random.default_rng(0)
    return rng.uniform(5, 60, size=(12, 20))


@pytest.mark.parametrize("objective", solver.OBJECTIVES)
@pytest.mark.parametrize("k", [1, 3, 5])
def test_exact_matches_naive(cost, k, objective):
    r = solver.solve(cost, k, objective=objective)
    assert r["method"] == "exact"
    key = (r["makespan"], r["total"]) if objective == "makespan" else (r["total"], r["makespan"])
    assert key == pytest.approx(_brute(cost, k, range(12), objective=objective))


def test_required_and_allowed(cost):
    r = solver.solve(cost, 4, allowed=[0, 1, 2, 3, 4, 5, 6], required=[6])
    assert 6 in r["subset"] and set(r["subset"]) <= set(range(7))
    assert (r["makespan"], r["total"]) == pytest.approx(_brute(cost, 4, range(7), required=[6]))


def test_heuristic_is_close(cost):
    exact = solver.solve(cost, 4)
    heur = solver.solve(cost, 4, max_exact=1)
    assert heur["method"] == "heuristic"
    assert heur["makespan"] >= exact["makespan"] - 1e-9
    assert heur["makespan"] <= exact["makespan"] * 1.25


def _brute_pref(cost, k, pref, avoid, margin, required=(), objective="makespan"):
    """優先 / なるべく避ける の定義どおりに総当たりで選ぶ。"""
    w = np.zeros(cost.shape[0], dtype=int)
    w[list(pref)], w[list(avoid)] = 1, -1
    rows = []
    for sub in combinations([i for i in range(cost.shape[0]) if i not in required],
                            k - len(required)):
        s = list(required) + list(sub)
        rows.append((solver._key(cost, s, objective), solver._pref_counts(cost, s, w)))
    best = min(r[0][0] for r in rows)
    ok = [r for r in rows if r[0][0] <= best + margin + 1e-9]
    key, (np_, na) = min(ok, key=lambda r: (-r[1][0], r[1][1], r[0]))
    return key, np_, na, best


@pytest.mark.parametrize("objective", solver.OBJECTIVES)
@pytest.mark.parametrize("margin", [0, 3, 10, 1000])
@pytest.mark.parametrize("required", [(), (7,)])
def test_preferred_and_avoid_match_naive(cost, margin, objective, required):
    pref, avoid = [0, 4, 9], [1, 2, 11]
    r = solver.solve(cost, 4, objective=objective, preferred=pref, avoid=avoid,
                     margin=margin, required=required)
    key, np_, na, best = _brute_pref(cost, 4, pref, avoid, margin, required, objective)
    got = (r["makespan"], r["total"]) if objective == "makespan" else (r["total"], r["makespan"])
    assert got == pytest.approx(key)
    assert (r["num_preferred"], r["num_avoided"]) == (np_, na)
    assert r["best_primary"] == pytest.approx(best)
    assert got[0] <= best + margin + 1e-9


def test_preferred_margin_trades_time(cost):
    plain = solver.solve(cost, 3)
    # 最良解に入っていない拠点を優先にすると、許容幅が大きければ採用される
    other = next(i for i in range(12) if i not in plain["subset"])
    tight = solver.solve(cost, 3, preferred=[other], margin=0)
    loose = solver.solve(cost, 3, preferred=[other], margin=1000)
    assert tight["makespan"] == pytest.approx(plain["makespan"])
    assert other in loose["subset"] and loose["num_preferred"] == 1
    # なるべく避ける: 最良解の拠点を避けると、許容幅が大きければ外れる
    av = plain["subset"][0]
    r = solver.solve(cost, 3, avoid=[av], margin=1000)
    assert r["num_avoided"] == 0


def test_heuristic_respects_preference(cost):
    other = next(i for i in range(12) if i not in solver.solve(cost, 4)["subset"])
    r = solver.solve(cost, 4, max_exact=1, preferred=[other], margin=1000)
    assert r["method"] == "heuristic" and r["num_preferred"] == 1
    r = solver.solve(cost, 4, max_exact=1, preferred=[other], margin=0)
    assert r["makespan"] <= r["best_primary"] + 1e-9


def test_errors(cost):
    with pytest.raises(ValueError):
        solver.solve(cost, 3, required=[0, 1, 2, 3])
    with pytest.raises(ValueError):
        solver.solve(cost, 5, allowed=[0, 1])


def _scenario():
    sc = scn.new_scenario("テスト")
    sc["targets"] = [{"name": f"t{i}", "lat": 36 + i * 0.01, "lon": 139.5} for i in range(6)]
    sc["candidates"] = [{"name": f"c{i}", "lat": 36 + i * 0.02, "lon": 139.6, "group": "g"} for i in range(4)]
    sc["candidates"][3]["status"] = "excluded"
    sc["params"].update(num_depots=2, sensitivity={"enabled": True, "min": 1, "max": 5})
    sc["params"]["routing"] = {"time_limit_sec": 1}
    return scn.normalize(sc)


def _fake_matrix(sources, destinations, **kw):
    m = np.array([[abs(s[0] - d[0]) * 1000 + 3 for d in destinations] for s in sources])
    return m, "fake", 0


def test_runner(monkeypatch):
    monkeypatch.setattr(runner, "build_matrix", _fake_matrix)
    res = runner.run_scenario(_scenario())
    assert res["summary"]["num_candidates"] == 3
    assert sum(d["vehicles"] for d in res["depots"]) == 6
    assert [x["k"] for x in res["sensitivity"]] == [1, 2, 3]      # 除外があるので上限は 3
    assert {c["id"]: c["result"] for c in res["candidates"]}["C004"] == "除外"
    ms = [x["makespan_min"] for x in res["sensitivity"]]
    assert ms == sorted(ms, reverse=True)
    assert res["summary"]["lower_bound_min"] <= res["summary"]["makespan_min"]


def test_validate_messages():
    sc = _scenario()
    sc["params"]["num_depots"] = 9
    assert any("採用数" in e for e in scn.validate(sc))
    sc["params"]["num_depots"] = 1
    for c in sc["candidates"]:
        c["status"] = "required"
    sc["candidates"][3]["status"] = "excluded"
    assert any("必須" in e for e in scn.validate(sc))


def test_safe_filename():
    for bad in ("../../etc/passwd", "..\\..\\x", "a/b", "a\\b", "..", ""):
        name = scn.safe_filename(bad)
        assert "/" not in name and "\\" not in name and not name.startswith(".")
        assert (scn.config.SCENARIO_DIR / name).resolve().parent == scn.config.SCENARIO_DIR.resolve()
    assert scn.safe_filename("x.json") == "x.json"


def test_save_token_keeps_other_lines(tmp_path, monkeypatch):
    monkeypatch.delenv(config.TOKEN_KEY, raising=False)
    env = tmp_path / ".env"
    env.write_text("OTHER=1\nMAPBOX_ACCESS_TOKEN=old\n", encoding="utf-8")
    config.save_token("pk.new", env)
    assert env.read_text(encoding="utf-8") == "OTHER=1\nMAPBOX_ACCESS_TOKEN=pk.new\n"
    monkeypatch.delenv(config.TOKEN_KEY, raising=False)
    assert config.get_token(env) == "pk.new"


def test_ensure_data_dir_copies_samples_once(tmp_path, monkeypatch):
    bundled, data = tmp_path / "bundled", tmp_path / "data" / "scenarios"
    bundled.mkdir()
    (bundled / "a.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "BUNDLED_SCENARIO_DIR", bundled)
    monkeypatch.setattr(config, "SCENARIO_DIR", data)
    config.ensure_data_dir()
    assert [p.name for p in data.glob("*.json")] == ["a.json"]
    (data / "a.json").unlink()
    (data / "mine.json").write_text("{}", encoding="utf-8")
    config.ensure_data_dir()                       # 利用者のシナリオがあれば何もしない
    assert [p.name for p in data.glob("*.json")] == ["mine.json"]


def test_api_run_and_scenarios(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "build_matrix", _fake_matrix)
    monkeypatch.setattr(scn, "scenario_path", lambda name, directory=tmp_path: tmp_path / scn.safe_filename(name))
    monkeypatch.setattr(server.report, "run_dir", lambda result, base=None: tmp_path / "run")
    c = TestClient(server.app)

    assert c.get("/api/config").status_code == 200
    assert c.get("/").status_code == 200
    assert c.get("/static/vendor/carbon/styles.min.css").status_code == 200
    r = c.put("/api/scenarios/テスト", json=_scenario())
    assert r.status_code == 200 and r.json()["file"] == "テスト.json"
    assert c.get("/api/scenarios/テスト.json").json()["scenario"]["name"] == "テスト"

    bad = _scenario()
    bad["params"]["num_depots"] = 10
    assert c.post("/api/runs", json=bad).status_code == 400

    job = c.post("/api/runs", json=_scenario()).json()["id"]
    import time
    for _ in range(100):
        st = c.get(f"/api/runs/{job}").json()
        if st["status"] != "running":
            break
        time.sleep(0.05)
    assert st["status"] == "done", st
    for fmt in ("json", "csv", "routes", "docx"):
        assert c.get(f"/api/runs/{job}/export/{fmt}").status_code == 200

    # 道路沿いの線: 1 台目だけ取得に失敗させ、他は立ち寄り地点をそのまま線にする
    calls = []

    def fake_line(points, profile="driving", token=None):
        calls.append(points)
        if len(calls) == 1:
            raise server.mapbox_api.MapboxError("NoRoute")
        return [[lon, lat] for lat, lon in points]

    monkeypatch.setattr(server.mapbox_api, "directions_line", fake_line)
    g = c.get(f"/api/runs/{job}/geometry").json()
    assert len(g["vehicles"]) == len(calls) >= 1
    assert sum(v is None for v in g["vehicles"].values()) == 1 and g["errors"] == ["NoRoute"]
    assert c.get(f"/api/runs/{job}/geometry").json() == g and len(calls) == len(g["vehicles"])  # 2 回目は再取得しない
    assert "geometry" not in c.get(f"/api/runs/{job}").json()


def test_directions_line_splits_and_joins(monkeypatch):
    from depot_planner import mapbox_api
    monkeypatch.setattr(mapbox_api, "DIRECTIONS_COORD_LIMIT", 3)
    requests = []

    def fake_get(url, params):
        coords = [[float(x) for x in c.split(",")] for c in url.rsplit("/", 1)[1].split(";")]
        requests.append(coords)
        return {"code": "Ok", "routes": [{"geometry": {"coordinates": coords}}]}

    monkeypatch.setattr(mapbox_api, "_get_json", fake_get)
    pts = [(35.0, 139.0), (35.0, 139.0), (35.1, 139.1), (35.2, 139.2), (35.3, 139.3), (35.4, 139.4)]
    line = mapbox_api.directions_line(pts, token="pk.x")
    # 連続する同一地点はまとめ、3 点ずつ端点を重ねて分割、つなぎ目は 1 回だけ
    assert [len(r) for r in requests] == [3, 3]
    assert line == [[lon, lat] for lat, lon in pts[1:]]
    assert mapbox_api.directions_line([(35.0, 139.0), (35.0, 139.0)], token="pk.x") is None


# ---------------------------------------------------------------------------
# 配分ルート最適化
# ---------------------------------------------------------------------------
def _route_time(n_depots=3, n_targets=9, seed=1):
    rng = np.random.default_rng(seed)
    pts = rng.uniform(0, 30, size=(n_depots + n_targets, 2))
    return np.linalg.norm(pts[:, None] - pts[None], axis=2)


def _check_routes(res, stock, n_targets, capacity, max_deliveries):
    delivered = []
    for v in res["vehicles"]:
        load = 0
        for st in v["stops"]:
            load += st["count"] if st["kind"] == "pickup" else -1
            assert 0 <= load <= capacity          # 積まずに配らない・積みすぎない
            if st["kind"] == "deliver":
                delivered.append(st["target"])
        assert v["deliveries"] <= max_deliveries
    assert sorted(delivered) == list(range(n_targets))   # 全目的地にちょうど 1 回
    picked = {}
    for v in res["vehicles"]:
        for st in v["stops"]:
            if st["kind"] == "pickup":
                picked[st["depot"]] = picked.get(st["depot"], 0) + st["count"]
    assert all(picked.get(i, 0) <= s for i, s in enumerate(stock))   # 在庫を超えない


@pytest.mark.parametrize("capacity,max_deliveries", [(1, None), (3, None), (2, 4)])
def test_solve_routes(capacity, max_deliveries):
    stock = [4, 3, 2]
    res = routing.solve_routes(_route_time(), stock, capacity=capacity,
                               max_deliveries=max_deliveries, service_min=5, reload_min=2,
                               time_limit_sec=1)
    assert res["num_vehicles"] == sum(routing.vehicles_needed(s, capacity) for s in stock)
    _check_routes(res, stock, 9, capacity, max_deliveries or capacity)
    assert res["makespan"] == max(v["time_min"] for v in res["vehicles"])


@pytest.mark.parametrize("num_vehicles,capacity,max_deliveries",
                         [(2, 1, None), (3, 2, None), (4, 2, 3), (12, 1, None)])
def test_solve_routes_fixed_fleet(num_vehicles, capacity, max_deliveries):
    stock = [4, 3, 2]
    res = routing.solve_routes(_route_time(), stock, capacity=capacity,
                               max_deliveries=max_deliveries, num_vehicles=num_vehicles,
                               service_min=5, reload_min=2, time_limit_sec=1)
    assert res["num_vehicles"] == len(res["vehicles"]) == num_vehicles
    _check_routes(res, stock, 9, capacity, max_deliveries or 9)


def test_allocate_vehicles():
    assert routing.allocate_vehicles([4, 3, 2], 2) == [2, 2, 1]          # 自動
    assert routing.allocate_vehicles([4, 3, 2], 1, total=2) == [1, 1, 0]  # 台数 < 拠点数
    assert routing.allocate_vehicles([4, 3, 2], 1, total=6) == [3, 2, 1]
    assert routing.allocate_vehicles([5, 0, 1], 1, total=4) == [3, 0, 1]  # 機材 0 の拠点には置かない
    assert sum(routing.allocate_vehicles([7, 1, 1], 3, total=10)) == 10


def test_solve_routes_errors():
    with pytest.raises(ValueError):
        routing.solve_routes(_route_time(), [4, 3, 2], capacity=0)
    with pytest.raises(ValueError):
        routing.solve_routes(_route_time(), [1, 1, 1], capacity=1)     # 機材不足


def test_runner_with_capacity(monkeypatch):
    monkeypatch.setattr(runner, "build_matrix", _fake_matrix)
    sc = _scenario()
    sc["params"]["routing"].update(vehicle_capacity=2)
    res = runner.run_scenario(sc)
    rt = res["routing"]
    assert res["summary"]["num_vehicles"] == rt["num_vehicles"] == sum(d["vehicles"] for d in res["depots"])
    assert sum(d["stock"] for d in res["depots"]) == len(sc["targets"])
    delivered = [st["id"] for v in rt["vehicles"] for st in v["stops"] if st["kind"] == "deliver"]
    assert sorted(delivered) == sorted(t["id"] for t in sc["targets"])
    assert all(v["deliveries"] <= 2 for v in rt["vehicles"])
    assert "配送" in runner.format_text(res)

    sc["params"]["routing"]["enabled"] = False
    assert runner.run_scenario(sc)["routing"] is None


def test_validate_routing():
    sc = _scenario()
    sc["params"]["routing"].update(vehicle_capacity=0)
    assert any("積載量" in e for e in scn.validate(sc))
    sc["params"]["routing"].update(vehicle_capacity=2, max_deliveries=0)
    assert any("配送上限" in e for e in scn.validate(sc))
    sc["params"]["routing"].update(max_deliveries=None, num_vehicles=0)
    assert any("車両の総数" in e for e in scn.validate(sc))
    sc["params"]["routing"].update(num_vehicles=1, max_deliveries=1)
    assert any("回り切れません" in e for e in scn.validate(sc))
    sc["params"]["routing"].update(num_vehicles=None, max_deliveries=None, enabled=False, vehicle_capacity=0)
    assert scn.validate(sc) == []        # 無効なら見ない


def test_runner_with_fixed_fleet(monkeypatch):
    monkeypatch.setattr(runner, "build_matrix", _fake_matrix)
    sc = _scenario()
    sc["params"]["routing"].update(vehicle_capacity=1, num_vehicles=2)
    res = runner.run_scenario(sc)
    rt = res["routing"]
    assert res["summary"]["num_vehicles"] == rt["num_vehicles"] == 2
    assert sum(d["vehicles"] for d in res["depots"]) == 2
    assert rt["fixed_fleet"] and res["summary"]["fixed_fleet"]
    delivered = [st["id"] for v in rt["vehicles"] for st in v["stops"] if st["kind"] == "deliver"]
    assert sorted(delivered) == sorted(t["id"] for t in sc["targets"])
    assert "総数を指定" in runner.format_text(res)


# ---------------------------------------------------------------------------
# 選定とルートの一体計算
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("objective", solver.OBJECTIVES)
def test_alternatives_are_top_subsets(cost, objective):
    r = solver.solve(cost, 3, objective=objective, top=6)
    alts = r["alternatives"]
    assert alts[0] == r["subset"] and len(alts) == 6
    assert len({tuple(sorted(a)) for a in alts}) == 6
    keys = [solver._key(cost, a, objective) for a in alts]
    every = sorted(solver._key(cost, list(s), objective) for s in combinations(range(12), 3))
    assert keys == pytest.approx(every[:6])           # 総当たりの上位 6 件と一致


def test_alternatives_heuristic_and_preference(cost):
    r = solver.solve(cost, 4, max_exact=1, top=5)
    assert r["alternatives"][0] == r["subset"] and len(r["alternatives"]) == 5
    r = solver.solve(cost, 4, preferred=[0], avoid=[1], margin=5, top=4)
    assert r["alternatives"][0] == r["subset"]
    assert solver.solve(cost, 4)["alternatives"] == [solver.solve(cost, 4)["subset"]]


def _line_case():
    """直行では A 組が最良だが、積載 3 で回ると B 組のほうが速くなる配置。"""
    # 候補 0..3 と目的地 0..5 を数直線上に置く(移動時間 = 距離)
    cpos = [0.0, 10.0, 4.0, 30.0]
    tpos = [1.0, 2.0, 3.0, 9.0, 11.0, 29.0]
    pts = cpos + tpos
    tmat = np.abs(np.subtract.outer(pts, pts))
    cost = tmat[:4, 4:] + 1.0
    return tmat, cost


def test_integrated_plan_picks_best_route():
    from depot_planner import integrated as itg
    tmat, cost = _line_case()
    alts = solver.solve(cost, 2, top=6)["alternatives"]
    res = itg.plan(tmat, cost, alts, capacity=3, service_min=1, reload_min=0,
                   time_limit_sec=1, screen_time_sec=1)
    assert res["skipped"] is None and len(res["screened"]) == len(alts)
    best = min(r["route_makespan"] for r in res["screened"])
    assert res["route"]["makespan"] <= best + 1e-9
    assert res["subset"] == res["screened"][res["chosen_rank"] - 1]["subset"]
    # どの目的地にもちょうど 1 回届き、積載量を守る
    _check_routes(res["route"], [6] * 2, 6, 3, 3)


def test_integrated_plan_skips_when_direct_equivalent():
    from depot_planner import integrated as itg
    tmat, cost = _line_case()
    alts = solver.solve(cost, 2, top=6)["alternatives"]
    res = itg.plan(tmat, cost, alts, capacity=1, time_limit_sec=1)
    assert res["skipped"] and res["screened"] == [] and res["subset"] == alts[0]
    assert itg.is_direct_equivalent(1, None, None, 6)
    assert not itg.is_direct_equivalent(1, None, 3, 6)
    assert not itg.is_direct_equivalent(2, None, None, 6)


@pytest.mark.parametrize("routing_kw", [dict(vehicle_capacity=2), dict(vehicle_capacity=1, num_vehicles=2),
                                        dict(vehicle_capacity=1)])
def test_runner_integrated(monkeypatch, routing_kw):
    monkeypatch.setattr(runner, "build_matrix", _fake_matrix)
    sc = _scenario()
    sc["params"]["routing"].update(screen_candidates=3, screen_time_sec=1, **routing_kw)
    res = runner.run_scenario(sc)
    rt, s, ig = res["routing"], res["summary"], res["integration"]
    assert s["integrated"] and rt["integrated"] and ig is not None
    assert s["makespan_min"] == rt["makespan_min"]
    assert s["direct_makespan_min"] <= s["makespan_min"] + 1e-9   # 直行は下界
    # 機材数 = 実際に積んだ個数、担当 = 積んだ拠点
    picked = {}
    for v in rt["vehicles"]:
        for st in v["stops"]:
            if st["kind"] == "pickup":
                picked[st["id"]] = picked.get(st["id"], 0) + st["count"]
    assert {d["id"]: d["stock"] for d in res["depots"] if d["stock"]} == picked
    assert sum(d["stock"] for d in res["depots"]) == s["num_targets"]
    assert sum(d["vehicles"] for d in res["depots"]) == rt["num_vehicles"] == s["num_vehicles"]
    assert max(a["time_min"] for a in res["assignments"]) == pytest.approx(rt["makespan_min"])
    text = runner.format_text(res)
    assert "一体" in text


def test_runner_separate_mode(monkeypatch):
    monkeypatch.setattr(runner, "build_matrix", _fake_matrix)
    sc = _scenario()
    sc["params"]["routing"].update(vehicle_capacity=2, integrated=False)
    res = runner.run_scenario(sc)
    assert res["integration"] is None and not res["summary"]["integrated"]
    assert res["summary"]["makespan_min"] == res["summary"]["direct_makespan_min"]
    assert all(d["vehicles"] == -(-d["stock"] // 2) for d in res["depots"])


def test_docx_with_integration(monkeypatch, tmp_path):
    from depot_planner import report
    monkeypatch.setattr(runner, "build_matrix", _fake_matrix)
    sc = _scenario()
    sc["params"]["routing"].update(vehicle_capacity=2, screen_candidates=3, screen_time_sec=1)
    res = runner.run_scenario(sc)
    report.save_all(res, tmp_path)
    assert (tmp_path / "report.docx").stat().st_size > 0


def test_validate_integrated():
    sc = _scenario()
    sc["params"]["routing"].update(screen_candidates=0)
    assert any("比べる組合せ" in e for e in scn.validate(sc))
    sc["params"]["routing"].update(screen_candidates=4, screen_time_sec=0)
    assert any("比較時の探索時間" in e for e in scn.validate(sc))
