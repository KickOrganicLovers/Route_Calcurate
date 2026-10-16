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


def test_api_run_and_scenarios(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "build_matrix", _fake_matrix)
    monkeypatch.setattr(scn, "scenario_path", lambda name, directory=tmp_path: tmp_path / scn.safe_filename(name))
    monkeypatch.setattr(server.report, "run_dir", lambda result, base=None: tmp_path / "run")
    c = TestClient(server.app)

    assert c.get("/api/config").status_code == 200
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
    assert all(d["vehicles"] == -(-d["stock"] // 2) for d in res["depots"])
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
