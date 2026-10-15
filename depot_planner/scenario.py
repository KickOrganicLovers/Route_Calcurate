# -*- coding: utf-8 -*-
"""シナリオ(目的地・補給地点・パラメータ一式)の JSON 入出力と検証。

GUI も CLI も同じ scenarios/*.json を読み書きする。書式:

{
  "version": 1,
  "name": "埼玉 全33地点",
  "description": "",
  "targets":    [{"id", "name", "lat", "lon", "address", "enabled", "note", "mapbox_id"}],
  "candidates": [{"id", "name", "lat", "lon", "address", "group",
                  "status": "candidate" | "required" | "excluded", "note", "mapbox_id"}],
  "params": {...}   # DEFAULT_PARAMS 参照
}
"""

import copy
import json
import re
from pathlib import Path

from . import config
from .mapbox_api import PROFILES
from .solver import OBJECTIVES
from .travel_times import PROVIDERS

STATUSES = ("candidate", "required", "excluded")
STATUS_LABEL = {"candidate": "候補", "required": "必須", "excluded": "除外"}

DEFAULT_PARAMS = {
    "num_depots": 5,            # 採用する補給地点の数
    "service_min": 10.0,        # 目的地での作業時間[分]
    "reload_min": 5.0,          # 補給地点での積込時間[分]
    "profile": "driving",       # driving / driving-traffic / walking / cycling
    "provider": "mapbox",       # mapbox / osrm / auto
    "objective": "makespan",    # makespan(最遅車両を最小化) / total(総所要を最小化)
    "sensitivity": {"enabled": True, "min": 1, "max": 10},
    "max_exact_combinations": 5_000_000,
    # 2 段目: 配分ルート最適化(OR-Tools)
    "routing": {
        "enabled": True,
        "vehicle_capacity": 1,      # 1 台に積み込める機材の数
        "max_deliveries": None,     # 1 台あたりの配送件数上限。None = 積載量と同じ(積み直しなし)
        "time_limit_sec": 30,       # 探索時間[秒]
    },
}
NESTED_PARAMS = ("sensitivity", "routing")


def new_scenario(name="新しいシナリオ"):
    return {"version": 1, "name": name, "description": "",
            "targets": [], "candidates": [], "params": copy.deepcopy(DEFAULT_PARAMS)}


def _next_id(prefix, used):
    n = 1
    while f"{prefix}{n:03d}" in used:
        n += 1
    return f"{prefix}{n:03d}"


def normalize(sc):
    """欠けている項目を既定値で埋めたコピーを返す。"""
    sc = copy.deepcopy(sc)
    base = new_scenario(sc.get("name") or "無題")
    for k, v in base.items():
        sc.setdefault(k, v)
    params = copy.deepcopy(DEFAULT_PARAMS)
    for k, v in (sc.get("params") or {}).items():
        if k in NESTED_PARAMS and isinstance(v, dict):
            params[k].update(v)
        else:
            params[k] = v
    sc["params"] = params

    for key, prefix in (("targets", "T"), ("candidates", "C")):
        used = {str(p["id"]) for p in sc[key] if p.get("id")}
        for p in sc[key]:
            if not p.get("id"):
                p["id"] = _next_id(prefix, used)
                used.add(p["id"])
            p["id"] = str(p["id"])
            p.setdefault("name", p["id"])
            p.setdefault("address", "")
            p.setdefault("note", "")
            p["lat"], p["lon"] = float(p["lat"]), float(p["lon"])
    for t in sc["targets"]:
        t.setdefault("enabled", True)
    for c in sc["candidates"]:
        c.setdefault("group", "")
        c.setdefault("status", "candidate")
    return sc


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def validate(sc):
    """実行前チェック。問題点のリスト(空なら OK)を返す。"""
    errors = []
    p = sc["params"]
    targets = [t for t in sc["targets"] if t.get("enabled", True)]
    cands = [c for c in sc["candidates"] if c.get("status") != "excluded"]
    required = [c for c in cands if c.get("status") == "required"]
    if not targets:
        errors.append("有効な目的地が 1 件もありません")
    if not cands:
        errors.append("選定対象の補給地点が 1 件もありません(すべて除外されています)")
    k = p.get("num_depots")
    if not isinstance(k, int) or k < 1:
        errors.append("採用数は 1 以上の整数にしてください")
    elif k > len(cands):
        errors.append(f"採用数 {k} が選定対象の補給地点 {len(cands)} 件を超えています")
    elif len(required) > k:
        errors.append(f"必須の補給地点 {len(required)} 件が採用数 {k} を超えています")
    if p.get("profile") not in PROFILES:
        errors.append(f"移動手段は {PROFILES} のいずれか")
    if p.get("provider") not in PROVIDERS:
        errors.append(f"所要時間の取得元は {PROVIDERS} のいずれか")
    if p.get("provider") == "osrm" and p.get("profile") != "driving":
        errors.append("OSRM は driving のみ対応です")
    if p.get("objective") not in OBJECTIVES:
        errors.append(f"目的関数は {OBJECTIVES} のいずれか")
    for key in ("service_min", "reload_min"):
        if not isinstance(p.get(key), (int, float)) or p[key] < 0:
            errors.append(f"{key} は 0 以上の数値にしてください")
    rt = p.get("routing") or {}
    if rt.get("enabled"):
        cap, md, tl = rt.get("vehicle_capacity"), rt.get("max_deliveries"), rt.get("time_limit_sec")
        if not _is_int(cap) or cap < 1:
            errors.append("積載量は 1 以上の整数にしてください")
        if md is not None and (not _is_int(md) or md < 1):
            errors.append("1 台あたりの配送上限は 1 以上の整数か空欄にしてください")
        if not _is_int(tl) or not 1 <= tl <= 3600:
            errors.append("ルート探索時間は 1〜3600 秒の整数にしてください")
    for kind, items in (("目的地", sc["targets"]), ("補給地点", sc["candidates"])):
        ids = [x["id"] for x in items]
        if len(ids) != len(set(ids)):
            errors.append(f"{kind}の id が重複しています")
        for x in items:
            if not (-90 <= x["lat"] <= 90 and -180 <= x["lon"] <= 180):
                errors.append(f"{kind} {x['name']} の座標が不正です")
    for c in sc["candidates"]:
        if c.get("status") not in STATUSES:
            errors.append(f"補給地点 {c['name']} の status が不正です: {c.get('status')}")
    return errors


# ---------------------------------------------------------------------------
# ファイル入出力
# ---------------------------------------------------------------------------
def safe_filename(name):
    """シナリオ名 → ファイル名(ディレクトリ外へ出られない文字は置換)。"""
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(name)).strip(" .")
    if stem.lower().endswith(".json"):
        stem = stem[:-5]
    return (stem or "scenario") + ".json"


def scenario_path(name, directory=config.SCENARIO_DIR):
    return Path(directory) / safe_filename(name)


def load(path):
    return normalize(json.loads(Path(path).read_text(encoding="utf-8")))


def save(sc, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(normalize(sc), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def list_scenarios(directory=config.SCENARIO_DIR):
    out = []
    for p in sorted(Path(directory).glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        out.append({"file": p.name, "name": data.get("name", p.stem),
                    "targets": len(data.get("targets", [])),
                    "candidates": len(data.get("candidates", [])),
                    "updated_at": p.stat().st_mtime})
    return out
