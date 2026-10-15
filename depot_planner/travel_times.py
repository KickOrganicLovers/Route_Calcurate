# -*- coding: utf-8 -*-
"""補給地点 → 目的地 の所要時間行列の取得とキャッシュ。

キャッシュは「座標ペア単位」で持つ。地点を 1 件足しても、足した地点に関わる
ペアだけを取得すればよいので、GUI で試行錯誤するときの API 呼び出しが少なくて済む。

取得元(provider):
    mapbox … Mapbox Matrix API(道路ネットワーク上の所要時間)
    osrm   … 公開 OSRM デモサーバ(無保証・レート制限あり。driving のみ)
    auto   … mapbox を試し、失敗したら osrm
"""

import json
import threading
import time
import urllib.request

import numpy as np

from . import config, mapbox_api

UNREACHABLE = 1e9
OSRM_TABLE_URL = "https://router.project-osrm.org/table/v1/driving/{coords}"
OSRM_COORD_LIMIT = 90
PROVIDERS = ("mapbox", "osrm", "auto")

_lock = threading.Lock()


def _pt(p):
    return f"{p[0]:.7f},{p[1]:.7f}"


class PairCache:
    """{"provider|profile": {"lat,lon>lat,lon": 分 or null}} を JSON で保持する。"""

    def __init__(self, path=config.CACHE_PATH):
        self.path = path
        self.data = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8")).get("entries", {})
            except (ValueError, OSError):
                self.data = {}

    def bucket(self, provider, profile):
        return self.data.setdefault(f"{provider}|{profile}", {})

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 1, "updated_at": time.time(),
                                   "entries": self.data}), encoding="utf-8")
        tmp.replace(self.path)


def _osrm_block(sources, destinations):
    coords = list(sources) + list(destinations)
    coord_str = ";".join(f"{lon:.7f},{lat:.7f}" for lat, lon in coords)
    ns = len(sources)
    url = (OSRM_TABLE_URL.format(coords=coord_str)
           + "?annotations=duration"
           + "&sources=" + ";".join(str(i) for i in range(ns))
           + "&destinations=" + ";".join(str(ns + j) for j in range(len(destinations))))
    req = urllib.request.Request(url, headers={"User-Agent": "depot-planner/0.1"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("code") != "Ok":
        raise RuntimeError(f"OSRM エラー: {data.get('code')} {data.get('message', '')}")
    return [[(s / 60.0) if s is not None else None for s in row] for row in data["durations"]]


def _chunks(seq, size):
    return [seq[i:i + size] for i in range(0, len(seq), size)]


def _fetch_missing(bucket, sources, destinations, provider, profile, progress):
    """bucket に無いペアを取得して bucket に書き込む。取得したペア数を返す。"""
    missing_src, missing_dst = [], []
    for s in sources:
        for d in destinations:
            if f"{_pt(s)}>{_pt(d)}" not in bucket:
                if s not in missing_src:
                    missing_src.append(s)
                if d not in missing_dst:
                    missing_dst.append(d)
    if not missing_src:
        return 0

    if provider == "osrm":
        if profile != "driving":
            raise RuntimeError("OSRM デモサーバは driving のみ対応です")
        limit, fetch = OSRM_COORD_LIMIT, _osrm_block
    else:
        limit = mapbox_api.MATRIX_COORD_LIMIT[profile]
        fetch = lambda s, d: mapbox_api.matrix_block(s, d, profile)   # noqa: E731
    half = limit // 2

    blocks = [(sb, db) for sb in _chunks(missing_src, half)
              for db in _chunks(missing_dst, limit - len(sb))]
    # 既に全ペアがキャッシュにあるブロックは飛ばす
    blocks = [(sb, db) for sb, db in blocks
              if any(f"{_pt(s)}>{_pt(d)}" not in bucket for s in sb for d in db)]
    fetched = 0
    for i, (sb, db) in enumerate(blocks):
        if progress:
            progress(i / len(blocks), f"所要時間を取得中 ({provider}) {i + 1}/{len(blocks)}")
        rows = fetch(sb, db)
        for s, row in zip(sb, rows):
            for d, minutes in zip(db, row):
                bucket[f"{_pt(s)}>{_pt(d)}"] = minutes
                fetched += 1
    return fetched


def build_matrix(sources, destinations, profile="driving", provider="mapbox",
                 allow_fetch=True, progress=None, cache=None):
    """matrix[i][j] = sources[i] → destinations[j] の所要時間[分] (numpy)。

    sources / destinations: [(lat, lon), ...]
    戻り値: (matrix, provider_used, fetched_pairs)
    到達不能ペアは UNREACHABLE。
    """
    if provider not in PROVIDERS:
        raise ValueError(f"provider は {PROVIDERS} のいずれか: {provider}")
    sources = [(float(a), float(b)) for a, b in sources]
    destinations = [(float(a), float(b)) for a, b in destinations]

    with _lock:
        cache = cache or PairCache()
        order = ["mapbox", "osrm"] if provider == "auto" else [provider]
        used, fetched, errors = None, 0, []
        for prov in order:
            bucket = cache.bucket(prov, profile)
            try:
                if allow_fetch:
                    fetched = _fetch_missing(bucket, sources, destinations, prov, profile, progress)
                used = prov
                break
            except Exception as e:          # auto のときは次の provider へ
                errors.append(f"{prov}: {e}")
                if fetched:
                    cache.save()
        if used is None:
            raise RuntimeError("所要時間を取得できませんでした: " + " / ".join(errors))
        if fetched:
            cache.save()

        bucket = cache.bucket(used, profile)
        m = np.full((len(sources), len(destinations)), UNREACHABLE)
        missing = 0
        for i, s in enumerate(sources):
            for j, d in enumerate(destinations):
                key = f"{_pt(s)}>{_pt(d)}"
                if key not in bucket:
                    missing += 1
                elif bucket[key] is not None:
                    m[i, j] = bucket[key]
        if missing:
            raise RuntimeError(f"キャッシュに無いペアが {missing} 件あります(取得を許可して再実行してください)")
    return m, used, fetched
