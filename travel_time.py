# -*- coding: utf-8 -*-
"""移動時間プロバイダの選択とキャッシュ。

所要時間行列の取得元を 1 か所に集約する。優先順位は:

    1. Mapbox Matrix API   … トークンが有効なら最優先(mapbox.py)
    2. OSRM デモサーバ      … Mapbox が使えないときのフォールバック(osrm.py)

どちらも「道路ネットワーク上の運転時間」を返すので、切り替わっても
最適化の前提(直線近似ではない)は変わらない。取得結果は mapbox.py の
キャッシュ機構に相乗りして保存する(キーに provider を含めて混在を防ぐ)。
"""

import mapbox
import osrm

CACHE_PATH = mapbox.CACHE_PATH


def _cache_key(points, profile, provider):
    """provider ごとに別キーにする(取得元が変われば作り直す)。"""
    return mapbox._cache_key(points, f"{provider}:{profile}")


def _fetch_matrix(points, profile):
    """Mapbox を試し、駄目なら OSRM へ落とす。(matrix, provider) を返す。"""
    try:
        return mapbox.build_duration_matrix(points, profile=profile), "mapbox"
    except Exception as e:
        print(f"[travel_time] Mapbox を利用できません({e})。OSRM へフォールバックします。")
        return osrm.build_duration_matrix(points, profile=profile), "osrm"


def build_travel_time(targets, depots, profile="driving",
                      cache_path=CACHE_PATH, max_age_sec=None,
                      allow_fetch=True):
    """targets / depots の座標から (lat,lon),(lat,lon) -> 分 の callable を返す。

    キャッシュ優先。有効なキャッシュが無ければ取得し、書き出す。
    同一座標は 1 点に集約して行列を作るため、未知座標には大きな値を返す。
    """
    points, coord_index = mapbox._collect_points(targets, depots)

    matrix, provider = None, None
    for p in ("mapbox", "osrm"):
        cached = mapbox._read_cache(cache_path, _cache_key(points, profile, p),
                                    max_age_sec)
        if cached is not None:
            matrix, provider = cached, p
            break

    if matrix is None:
        if not allow_fetch:
            raise RuntimeError(
                f"有効なキャッシュ({cache_path})がありません。"
                "先に fetch_data.py でデータ取得してください。")
        matrix, provider = _fetch_matrix(points, profile)
        mapbox._write_cache(cache_path, _cache_key(points, profile, provider),
                            profile, points, matrix)

    def travel_time_min(a, b):
        i = coord_index.get((a[0], a[1]))
        j = coord_index.get((b[0], b[1]))
        if i is None or j is None:
            return 1e9
        return matrix[i][j]

    travel_time_min.provider = provider
    return travel_time_min


def fetch_and_cache(targets, depots, profile="driving", cache_path=CACHE_PATH):
    """常に再取得してキャッシュへ書き出す(fetch_data.py から呼ぶ)。"""
    points, _ = mapbox._collect_points(targets, depots)
    matrix, provider = _fetch_matrix(points, profile)
    mapbox._write_cache(cache_path, _cache_key(points, profile, provider),
                        profile, points, matrix)
    return matrix
