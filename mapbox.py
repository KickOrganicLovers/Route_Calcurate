# -*- coding: utf-8 -*-
"""Mapbox 由来の移動時間ユーティリティ。

配送先・補給地点の座標間の「実測に基づく所要時間」を Mapbox Matrix API
(directions-matrix) から取得し、(lat, lon) 2 点 -> 所要時間[分] を返す
callable を生成する。

これにより、従来の「球面距離 ÷ 速度」による直線近似を、道路ネットワーク上の
現実的な運転時間へ置き換えられる。

依存は標準ライブラリのみ(urllib)。アクセストークンは .env の
NEXT_PUBLIC_MAPBOX_ACCESS_TOKEN から読む。
"""

import hashlib
import json
import os
import time
import urllib.parse
import urllib.request

# Matrix API は 1 リクエストあたり最大 25 座標(driving プロファイル)。
# タイル分割時は「送信元ブロック + 送信先ブロック」を 1 リクエストに載せるため、
# 1 ブロックの上限は 25 // 2 = 12 とする。
MAX_COORDS_PER_REQUEST = 25
BLOCK_SIZE = MAX_COORDS_PER_REQUEST // 2

MATRIX_URL = "https://api.mapbox.com/directions-matrix/v1/mapbox/{profile}/{coords}"
TOKEN_ENV = "NEXT_PUBLIC_MAPBOX_ACCESS_TOKEN"

# 所要時間行列のキャッシュ保存先(座標＋プロファイルが変われば自動で無効化される)
CACHE_PATH = os.path.join("cache", "mapbox_matrix.json")


# ---------------------------------------------------------------------------
# .env / 環境変数からトークンを読む
# ---------------------------------------------------------------------------
def _load_token(env_path=".env"):
    token = os.environ.get(TOKEN_ENV)
    if token:
        return token
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                if key.strip() == TOKEN_ENV:
                    return val.strip().strip('"').strip("'")
    raise RuntimeError(
        f"{TOKEN_ENV} が見つかりません。.env か環境変数に設定してください。")


# ---------------------------------------------------------------------------
# Matrix API を叩いて所要時間行列(秒)を取得する
# ---------------------------------------------------------------------------
def _request_durations(coords, sources, destinations, token, profile):
    """coords 部分集合について source->destination の所要時間[秒]行列を取得。

    coords     : [(lat, lon), ...]  (この 1 リクエストに載せる全座標, <=25)
    sources    : coords 内インデックス(送信元)
    destinations: coords 内インデックス(送信先)
    戻り値: durations[i][j] = sources[i] -> destinations[j] の秒
    """
    # Mapbox は "lon,lat" 順(セミコロン区切り)
    coord_str = ";".join(f"{lon},{lat}" for (lat, lon) in coords)
    query = urllib.parse.urlencode({
        "annotations": "duration",
        "sources": ";".join(str(i) for i in sources),
        "destinations": ";".join(str(j) for j in destinations),
        "access_token": token,
    })
    url = MATRIX_URL.format(profile=profile, coords=coord_str) + "?" + query
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("code") != "Ok":
        raise RuntimeError(f"Mapbox Matrix API エラー: {data.get('code')} "
                           f"{data.get('message', '')}")
    return data["durations"]


def build_duration_matrix(points, token=None, profile="driving"):
    """unique な points 全対の所要時間[分]行列を Mapbox から構築する。

    points : [(lat, lon), ...]  (重複のない座標列)
    戻り値 : matrix[i][j] = points[i] -> points[j] の所要時間[分] (float)

    座標数が API 上限を超える場合は、ブロック対ごとのサブリクエストへ分割する。
    """
    token = token or _load_token()
    n = len(points)
    matrix = [[0.0] * n for _ in range(n)]

    blocks = [list(range(i, min(i + BLOCK_SIZE, n)))
              for i in range(0, n, BLOCK_SIZE)]

    for src_block in blocks:
        for dst_block in blocks:
            # このリクエストに載せる座標 = 送信元 + 送信先(重複排除)
            local_index = {}
            sub_coords = []
            for g in src_block + dst_block:
                if g not in local_index:
                    local_index[g] = len(sub_coords)
                    sub_coords.append(points[g])
            sources = [local_index[g] for g in src_block]
            destinations = [local_index[g] for g in dst_block]

            durations = _request_durations(
                sub_coords, sources, destinations, token, profile)

            for si, g_src in enumerate(src_block):
                for di, g_dst in enumerate(dst_block):
                    sec = durations[si][di]
                    # 到達不能は None で返る。極端に大きい値で埋める。
                    matrix[g_src][g_dst] = (sec / 60.0) if sec is not None else 1e9

    return matrix


# ---------------------------------------------------------------------------
# キャッシュ
#   座標列 + プロファイルのハッシュをキーにする。地点や profile を変えると
#   キーが変わり、古いキャッシュは自動的に無視される(=作り直しになる)。
# ---------------------------------------------------------------------------
def _collect_points(targets, depots):
    """targets / depots から unique 座標列と (lat,lon)->index を返す。"""
    coord_index = {}
    points = []
    for item in list(targets) + list(depots):
        key = (item["lat"], item["lon"])
        if key not in coord_index:
            coord_index[key] = len(points)
            points.append(key)
    return points, coord_index


def _cache_key(points, profile):
    h = hashlib.sha256()
    h.update(profile.encode("utf-8"))
    for lat, lon in points:
        h.update(f"{lat:.7f},{lon:.7f};".encode("utf-8"))
    return h.hexdigest()


def _read_cache(cache_path, key, max_age_sec):
    """キャッシュが有効なら matrix を返す。無効/不在なら None。"""
    if not os.path.exists(cache_path):
        return None
    try:
        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if data.get("key") != key:            # 地点/プロファイルが変わった
        return None
    if max_age_sec is not None:           # TTL 判定(任意時間で失効)
        if time.time() - data.get("fetched_at", 0) > max_age_sec:
            return None
    return data.get("matrix")


def _write_cache(cache_path, key, profile, points, matrix):
    parent = os.path.dirname(cache_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump({
            "key": key,
            "profile": profile,
            "fetched_at": time.time(),
            "points": points,          # [[lat, lon], ...] 参考用
            "matrix": matrix,          # 分単位
        }, f, ensure_ascii=False)


def fetch_and_cache(targets, depots, profile="driving", token=None,
                    cache_path=CACHE_PATH):
    """API を叩いて所要時間行列を取得し、キャッシュへ書き出す(常に再取得)。

    データ取得コマンド(fetch_data.py)から呼ぶ。戻り値は書き出した matrix。
    """
    points, _ = _collect_points(targets, depots)
    key = _cache_key(points, profile)
    matrix = build_duration_matrix(points, token=token, profile=profile)
    _write_cache(cache_path, key, profile, points, matrix)
    return matrix


# ---------------------------------------------------------------------------
# 配送先 + 補給地点から travel_time_min(a, b) callable を作る
# ---------------------------------------------------------------------------
def build_travel_time(targets, depots, profile="driving", token=None,
                      cache_path=CACHE_PATH, max_age_sec=None,
                      allow_fetch=True):
    """targets / depots の座標から (lat,lon),(lat,lon) -> 分 の callable を返す。

    キャッシュ優先。以下の順で行列を用意する。
        1. cache_path に有効なキャッシュがあれば、それを使う。
           (max_age_sec を指定すると、それより古いキャッシュは失効扱い)
        2. なければ API から取得し、キャッシュへ書き出す(allow_fetch=True 時)。
        3. allow_fetch=False でキャッシュが無い場合はエラー。

    同一座標(複数在庫の pickup など)は 1 点に集約して行列を作るため、
    未知座標が来た場合は大きな値を返す。
    """
    points, coord_index = _collect_points(targets, depots)
    key = _cache_key(points, profile)

    matrix = _read_cache(cache_path, key, max_age_sec)
    if matrix is None:
        if not allow_fetch:
            raise RuntimeError(
                f"有効なキャッシュ({cache_path})がありません。"
                "先に fetch_data.py でデータ取得してください。")
        matrix = build_duration_matrix(points, token=token, profile=profile)
        _write_cache(cache_path, key, profile, points, matrix)

    def travel_time_min(a, b):
        i = coord_index.get((a[0], a[1]))
        j = coord_index.get((b[0], b[1]))
        if i is None or j is None:
            return 1e9
        return matrix[i][j]

    return travel_time_min
