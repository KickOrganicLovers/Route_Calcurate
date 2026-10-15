# -*- coding: utf-8 -*-
"""OSRM 由来の所要時間行列。

Mapbox のトークンが使えない場合のフォールバック経路として、公開の OSRM
デモサーバ(router.project-osrm.org)の Table サービスから所要時間行列を取る。

Mapbox Matrix と同じく「道路ネットワーク上の運転時間」なので、直線距離近似より
現実に近い。ただしデモサーバは無保証・レート制限ありのため、常用する場合は
自前の OSRM か Mapbox トークンの再発行を推奨する。

インタフェースは mapbox.build_duration_matrix と互換
(points: [(lat, lon), ...] -> matrix[i][j] = 分)。
"""

import json
import urllib.request

TABLE_URL = "https://router.project-osrm.org/table/v1/driving/{coords}"

# Table サービスは 1 リクエストの座標数に上限がある(デモサーバは概ね 100)。
MAX_COORDS_PER_REQUEST = 90


def _request_table(coords, sources, destinations):
    """coords 内の sources -> destinations の所要時間[秒]行列を取得。"""
    coord_str = ";".join(f"{lon},{lat}" for (lat, lon) in coords)
    query = (f"?annotations=duration"
             f"&sources={';'.join(str(i) for i in sources)}"
             f"&destinations={';'.join(str(j) for j in destinations)}")
    url = TABLE_URL.format(coords=coord_str) + query
    req = urllib.request.Request(url, headers={"User-Agent": "route-calc/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("code") != "Ok":
        raise RuntimeError(f"OSRM Table API エラー: {data.get('code')} "
                           f"{data.get('message', '')}")
    return data["durations"]


def build_duration_matrix(points, profile="driving"):
    """unique な points 全対の所要時間[分]行列を OSRM から構築する。

    座標数が上限内なら 1 リクエストで全対を取得できる(Mapbox と違い
    タイル分割が不要なぶん呼び出し回数が少なくて済む)。
    """
    n = len(points)
    if n > MAX_COORDS_PER_REQUEST:
        raise RuntimeError(
            f"座標数 {n} が OSRM デモサーバの上限 {MAX_COORDS_PER_REQUEST} を超えます")

    idx = list(range(n))
    durations = _request_table(points, idx, idx)

    matrix = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            sec = durations[i][j]
            # 到達不能は None で返る。極端に大きい値で埋める。
            matrix[i][j] = (sec / 60.0) if sec is not None else 1e9
    return matrix
