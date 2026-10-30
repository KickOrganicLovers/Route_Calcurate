# -*- coding: utf-8 -*-
"""Mapbox API クライアント(Search Box API / Matrix API / Directions API)。

依存は標準ライブラリのみ。トークンは config.get_token() から取る。
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

SEARCH_BASE = "https://api.mapbox.com/search/searchbox/v1"
MATRIX_URL = "https://api.mapbox.com/directions-matrix/v1/mapbox/{profile}/{coords}"
DIRECTIONS_URL = "https://api.mapbox.com/directions/v5/mapbox/{profile}/{coords}"

# Directions API の 1 リクエストあたり座標上限
DIRECTIONS_COORD_LIMIT = 25

# Matrix API の 1 リクエストあたり座標上限(driving-traffic だけ 10)
MATRIX_COORD_LIMIT = {"driving": 25, "walking": 25, "cycling": 25, "driving-traffic": 10}
PROFILES = tuple(MATRIX_COORD_LIMIT)


class MapboxError(RuntimeError):
    pass


def _token(token=None):
    token = token or config.get_token()
    if not token:
        raise MapboxError("Mapbox アクセストークンが未設定です")
    return token


def _get_json(url, params, retries=4):
    """GET して JSON を返す。429 / 5xx は待って再試行する。"""
    full = url + "?" + urllib.parse.urlencode(params)
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(full, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if (e.code == 429 or e.code >= 500) and attempt < retries:
                wait = float(e.headers.get("Retry-After") or 2 ** attempt)
                time.sleep(min(wait, 30))
                continue
            try:
                msg = json.loads(body).get("message", body)
            except ValueError:
                msg = body
            raise MapboxError(f"Mapbox API エラー (HTTP {e.code}): {msg}") from None
        except urllib.error.URLError as e:
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise MapboxError(f"Mapbox に接続できません: {e.reason}") from None


# ---------------------------------------------------------------------------
# Search Box API
#   suggest(候補一覧) → retrieve(選んだ 1 件の座標) を同じ session_token で呼ぶと
#   1 セッションとして課金される。GUI の検索ボックスはこの流れを使う。
# ---------------------------------------------------------------------------
def clean_address(addr):
    """日本の住所で "A, A" のように同じ部分が重複して返るのをまとめる。"""
    parts = []
    for part in (addr or "").split(", "):
        part = part.strip()
        if part and part not in parts:
            parts.append(part)
    return ", ".join(parts)


def _feature_to_place(f):
    p = f.get("properties", {})
    lon, lat = f["geometry"]["coordinates"]
    return {
        "name": p.get("name") or clean_address(p.get("full_address")) or "",
        "address": clean_address(p.get("full_address") or p.get("place_formatted")),
        "lat": round(lat, 7),
        "lon": round(lon, 7),
        "mapbox_id": p.get("mapbox_id"),
        "feature_type": p.get("feature_type"),
        "category": ", ".join(p.get("poi_category") or []),
    }


def suggest(q, session_token, proximity=None, country="jp", language="ja",
            limit=8, types=None, token=None):
    params = {"q": q, "session_token": session_token, "language": language,
              "limit": limit, "access_token": _token(token)}
    if country:
        params["country"] = country
    if proximity:
        params["proximity"] = f"{proximity[1]},{proximity[0]}"   # lon,lat
    if types:
        params["types"] = types
    data = _get_json(f"{SEARCH_BASE}/suggest", params)
    return [{
        "mapbox_id": s.get("mapbox_id"),
        "name": s.get("name", ""),
        "address": clean_address(s.get("full_address") or s.get("place_formatted") or s.get("address")),
        "feature_type": s.get("feature_type"),
        "category": ", ".join(s.get("poi_category") or []),
        "distance_m": s.get("distance"),
    } for s in data.get("suggestions", [])]


def retrieve(mapbox_id, session_token, language="ja", token=None):
    data = _get_json(f"{SEARCH_BASE}/retrieve/{urllib.parse.quote(mapbox_id)}",
                     {"session_token": session_token, "language": language,
                      "access_token": _token(token)})
    feats = data.get("features") or []
    if not feats:
        raise MapboxError("地点の詳細を取得できませんでした")
    return _feature_to_place(feats[0])


def forward(q, proximity=None, country="jp", language="ja", limit=5, token=None):
    """1 回の呼び出しで座標付きの検索結果を返す(CLI 用)。"""
    params = {"q": q, "language": language, "limit": limit,
              "access_token": _token(token)}
    if country:
        params["country"] = country
    if proximity:
        params["proximity"] = f"{proximity[1]},{proximity[0]}"
    data = _get_json(f"{SEARCH_BASE}/forward", params)
    return [_feature_to_place(f) for f in data.get("features", [])]


def reverse(lat, lon, language="ja", token=None):
    """地図クリック地点の名称・住所を返す(見つからなければ座標のみ)。"""
    data = _get_json(f"{SEARCH_BASE}/reverse",
                     {"latitude": lat, "longitude": lon, "language": language,
                      "limit": 1, "access_token": _token(token)})
    feats = data.get("features") or []
    place = _feature_to_place(feats[0]) if feats else {"name": "", "address": ""}
    # 座標はクリックした位置を優先する(逆ジオコーディング結果の点ではなく)
    place.update({"lat": round(lat, 7), "lon": round(lon, 7)})
    return place


# ---------------------------------------------------------------------------
# Matrix API
# ---------------------------------------------------------------------------
def matrix_block(sources, destinations, profile="driving", token=None):
    """sources × destinations の所要時間[分]を 1 リクエストで取得する。

    sources / destinations: [(lat, lon), ...]  合計が座標上限以内であること。
    到達不能のペアは None。
    """
    coords = list(sources) + list(destinations)
    limit = MATRIX_COORD_LIMIT[profile]
    if len(coords) > limit:
        raise ValueError(f"座標数 {len(coords)} が上限 {limit} を超えています")
    coord_str = ";".join(f"{lon:.7f},{lat:.7f}" for lat, lon in coords)
    ns = len(sources)
    data = _get_json(MATRIX_URL.format(profile=profile, coords=coord_str), {
        "annotations": "duration",
        "sources": ";".join(str(i) for i in range(ns)),
        "destinations": ";".join(str(ns + j) for j in range(len(destinations))),
        "access_token": _token(token),
    })
    if data.get("code") != "Ok":
        raise MapboxError(f"Matrix API エラー: {data.get('code')} {data.get('message', '')}")
    return [[(sec / 60.0) if sec is not None else None for sec in row]
            for row in data["durations"]]


# ---------------------------------------------------------------------------
# Directions API(地図に描く道路沿いの線)
# ---------------------------------------------------------------------------
def directions_line(points, profile="driving", token=None):
    """points を順に通る道路沿いの線を [[lon, lat], ...] で返す。

    points: [(lat, lon), ...]  連続する同一地点はまとめる。座標上限を超える分は
    区間の端点を重ねて分割リクエストし、つなげて返す。地点が 1 つ以下なら None。
    """
    pts = []
    for p in points:
        if not pts or pts[-1] != tuple(p):
            pts.append(tuple(p))
    if len(pts) < 2:
        return None
    line = []
    step = DIRECTIONS_COORD_LIMIT - 1
    for i in range(0, len(pts) - 1, step):
        chunk = pts[i:i + DIRECTIONS_COORD_LIMIT]
        coord_str = ";".join(f"{lon:.7f},{lat:.7f}" for lat, lon in chunk)
        data = _get_json(DIRECTIONS_URL.format(profile=profile, coords=coord_str), {
            "geometries": "geojson", "overview": "full", "steps": "false",
            "access_token": _token(token),
        })
        if data.get("code") != "Ok" or not data.get("routes"):
            raise MapboxError(f"Directions API エラー: {data.get('code')} {data.get('message', '')}")
        coords = data["routes"][0]["geometry"]["coordinates"]
        line.extend(coords[1:] if line else coords)   # 区間のつなぎ目は重複させない
    return line
