# -*- coding: utf-8 -*-
"""Google Maps Routes API の matrix・道路沿いルート取得クライアント。"""

import json
import re
import time
import urllib.error
import urllib.request

from . import config

ROUTE_MATRIX_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
COMPUTE_ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
MATRIX_ORIGIN_LIMIT = 25
MATRIX_DESTINATION_LIMIT = 25
MAX_ROUTE_INTERMEDIATES = 25
TRAFFIC_SPEEDS = ("NORMAL", "SLOW", "TRAFFIC_JAM")
RETRY_WAITS_SEC = (10, 20, 30)   # 429/503 のときの待ち時間。上限は 1 分単位なので合計 1 分ほど待つ
TRAVEL_MODES = {
    "driving": ("DRIVE", "TRAFFIC_AWARE"),
    "driving-traffic": ("DRIVE", "TRAFFIC_AWARE"),
    "walking": ("WALK", None),
    "cycling": ("BICYCLE", None),
}


class GoogleMapsError(RuntimeError):
    pass


def _api_key(api_key=None):
    api_key = api_key or config.get_google_maps_api_key()
    if not api_key:
        raise GoogleMapsError(
            "Google Maps API キーが未設定です。.env に GOOGLE_MAPS_API_KEY を設定してください")
    return api_key


def _waypoints(points):
    return [{
        "waypoint": {"location": {"latLng": {"latitude": lat, "longitude": lon}}}
    } for lat, lon in points]


def _post_json(url, body, field_mask, api_key=None):
    """POST して JSON を返す。利用上限(429)や一時障害(503)は少し待って取り直す。"""
    for wait in RETRY_WAITS_SEC:
        try:
            return _post_json_once(url, body, field_mask, api_key)
        except _Retryable:
            time.sleep(wait)
    try:
        return _post_json_once(url, body, field_mask, api_key)
    except _Retryable as e:
        raise GoogleMapsError(str(e)) from None


class _Retryable(GoogleMapsError):
    pass


def _post_json_once(url, body, field_mask, api_key=None):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": _api_key(api_key),
            "X-Goog-FieldMask": field_mask,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8")).get("error", {}).get("message", "")
        except (ValueError, AttributeError):
            detail = ""
        hint = ""
        if e.code in (401, 403):
            hint = " Google Cloud で Routes API が有効か、API キーと請求先設定を確認してください。"
        if e.code == 429:
            hint = " 1 分あたりの利用上限に達しました。しばらく待ってから再実行してください。"
        error = _Retryable if e.code in (429, 503) else GoogleMapsError
        raise error(
            f"Google Maps Routes API エラー (HTTP {e.code}): {detail or e.reason}.{hint}") from None
    except (OSError, ValueError) as e:
        raise GoogleMapsError(f"Google Maps Routes API に接続できません: {e}") from None


def _decode_polyline(encoded):
    """Google encoded polyline を [経度, 緯度] の配列へ変換する。"""
    coordinates = []
    index = latitude = longitude = 0

    def decode_component(offset):
        result = shift = 0
        while True:
            if offset >= len(encoded):
                raise GoogleMapsError("Google Maps Routes API から不完全なポリラインが返されました")
            value = ord(encoded[offset]) - 63
            offset += 1
            if value < 0:
                raise GoogleMapsError("Google Maps Routes API から不正なポリラインが返されました")
            result |= (value & 0x1F) << shift
            if value < 0x20:
                break
            shift += 5
        delta = ~(result >> 1) if result & 1 else result >> 1
        return delta, offset

    while index < len(encoded):
        delta, index = decode_component(index)
        latitude += delta
        delta, index = decode_component(index)
        longitude += delta
        coordinates.append([longitude / 1e5, latitude / 1e5])
    return coordinates


def matrix_block(sources, destinations, profile="driving", api_key=None):
    """座標列間の所要時間[分]を Routes API から取得する。"""
    if profile not in TRAVEL_MODES:
        raise GoogleMapsError(f"Google Maps Routes API が未対応の移動手段です: {profile}")
    if not sources or not destinations:
        raise GoogleMapsError("所要時間を取得する地点がありません")
    if len(sources) > MATRIX_ORIGIN_LIMIT or len(destinations) > MATRIX_DESTINATION_LIMIT:
        raise GoogleMapsError("Google Maps Route Matrix の1回あたりの地点数上限を超えています")

    travel_mode, routing_preference = TRAVEL_MODES[profile]
    body = {
        "origins": _waypoints(sources),
        "destinations": _waypoints(destinations),
        "travelMode": travel_mode,
    }
    if routing_preference:
        body["routingPreference"] = routing_preference
    data = _post_json(
        ROUTE_MATRIX_URL, body, "originIndex,destinationIndex,duration,condition,status", api_key)

    missing = object()
    matrix = [[missing for _ in destinations] for _ in sources]
    for element in data:
        i, j = element.get("originIndex"), element.get("destinationIndex")
        if not isinstance(i, int) or not isinstance(j, int) or not (0 <= i < len(sources) and 0 <= j < len(destinations)):
            raise GoogleMapsError("Google Maps Routes API から不正な行列要素が返されました")
        status = element.get("status") or {}
        if status.get("code", 0):
            raise GoogleMapsError(f"Google Maps Routes API の経路計算エラー: {status.get('message', status)}")
        if element.get("condition") == "ROUTE_NOT_FOUND":
            matrix[i][j] = None
            continue
        duration = element.get("duration")
        match = re.fullmatch(r"(\d+(?:\.\d+)?)s", duration or "")
        if not match:
            raise GoogleMapsError("Google Maps Routes API の応答に所要時間がありません")
        matrix[i][j] = float(match.group(1)) / 60.0
    if any(value is missing for row in matrix for value in row):
        raise GoogleMapsError("Google Maps Routes API から一部の所要時間が返されませんでした")
    return matrix


def route_geometry(points, profile="driving", api_key=None):
    """立ち寄り地点を通る道路沿いルートを交通速度区間つきで返す。"""
    if profile not in TRAVEL_MODES:
        raise GoogleMapsError(f"Google Maps Routes API が未対応の移動手段です: {profile}")
    waypoints = []
    for point in points:
        point = tuple(point)
        if not waypoints or waypoints[-1] != point:
            waypoints.append(point)
    if len(waypoints) < 2:
        return []

    travel_mode, routing_preference = TRAVEL_MODES[profile]
    traffic_aware = travel_mode == "DRIVE"
    segments = []
    chunk_step = MAX_ROUTE_INTERMEDIATES + 1
    for offset in range(0, len(waypoints) - 1, chunk_step):
        chunk = waypoints[offset:offset + chunk_step + 1]
        body = {
            "origin": {"location": {"latLng": {
                "latitude": chunk[0][0], "longitude": chunk[0][1]}}},
            "destination": {"location": {"latLng": {
                "latitude": chunk[-1][0], "longitude": chunk[-1][1]}}},
            "intermediates": [
                {"location": {"latLng": {"latitude": lat, "longitude": lon}}}
                for lat, lon in chunk[1:-1]
            ],
            "travelMode": travel_mode,
            "polylineQuality": "HIGH_QUALITY",
        }
        field_mask = "routes.polyline.encodedPolyline"
        if routing_preference:
            body["routingPreference"] = routing_preference
        if traffic_aware:
            body["extraComputations"] = ["TRAFFIC_ON_POLYLINE"]
            field_mask += ",routes.travelAdvisory.speedReadingIntervals"
        data = _post_json(COMPUTE_ROUTES_URL, body, field_mask, api_key)
        routes = data.get("routes") if isinstance(data, dict) else None
        if not routes:
            raise GoogleMapsError("Google Maps Routes API から道路沿いルートが返されませんでした")
        route = routes[0]
        encoded = (route.get("polyline") or {}).get("encodedPolyline")
        if not encoded:
            raise GoogleMapsError("Google Maps Routes API の応答にポリラインがありません")
        coordinates = _decode_polyline(encoded)
        if len(coordinates) < 2:
            raise GoogleMapsError("Google Maps Routes API のポリラインが短すぎます")

        if not traffic_aware:
            segments.append({"speed": None, "coordinates": coordinates})
            continue

        intervals = ((route.get("travelAdvisory") or {}).get("speedReadingIntervals") or [])
        segments.extend(_traffic_segments(coordinates, intervals))
    return segments


def _traffic_segments(coordinates, intervals):
    """ポリラインを交通速度ごとの区間に分ける。

    Google の区間は抜けや重なりがあり得るので、点と点の間 1 つずつに速度を割り当ててから
    同じ速度の並びをまとめる。区間の無いところは UNKNOWN(情報なし)で埋め、線が途切れないようにする。
    """
    edges = len(coordinates) - 1
    speeds = ["UNKNOWN"] * edges
    for interval in intervals:
        start = interval.get("startPolylinePointIndex", 0)
        end = interval.get("endPolylinePointIndex", edges)
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        speed = interval.get("speed") or "UNKNOWN"
        if speed not in TRAFFIC_SPEEDS:
            speed = "UNKNOWN"
        for k in range(max(start, 0), min(end, edges)):
            speeds[k] = speed
    out = []
    begin = 0
    for k in range(1, edges + 1):
        if k == edges or speeds[k] != speeds[begin]:
            out.append({"speed": speeds[begin], "coordinates": coordinates[begin:k + 1]})
            begin = k
    return out
