# -*- coding: utf-8 -*-
"""所要時間データの取得・キャッシュコマンド。

配送先/補給地点候補の全対の運転時間を Mapbox Matrix API(使えない場合は
OSRM)から取得し、cache/mapbox_matrix.json へ保存する。main.py /
make_report.py はこのキャッシュを参照して動くため、実行前に一度これを
走らせておく。

使い方:
    uv run fetch_data.py                # キャッシュが最新(同一地点)なら再取得しない
    uv run fetch_data.py --force        # 中身に関わらず強制再取得
    uv run fetch_data.py --max-age 3600 # キャッシュが 3600 秒より古ければ再取得
    uv run fetch_data.py --profile driving-traffic  # プロファイル指定

いつでも実行してよく、任意のタイミングでキャッシュを更新できる。
"""

import argparse
import sys

from mapbox import _read_cache, _collect_points
from travel_time import CACHE_PATH, fetch_and_cache, _cache_key
from main import build_inputs


def main():
    ap = argparse.ArgumentParser(description="所要時間データを取得しキャッシュする")
    ap.add_argument("--force", action="store_true",
                    help="キャッシュの有無に関わらず強制的に再取得する")
    ap.add_argument("--max-age", type=float, default=None, metavar="SEC",
                    help="キャッシュがこの秒数より古い場合のみ再取得する")
    ap.add_argument("--profile", default="driving",
                    help="Mapbox プロファイル(driving / driving-traffic / walking など)")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    targets, depots = build_inputs()

    # 再取得が必要か判定(--force 指定時は無条件で取得)
    if not args.force:
        points, _ = _collect_points(targets, depots)
        for provider in ("mapbox", "osrm"):
            key = _cache_key(points, args.profile, provider)
            if _read_cache(CACHE_PATH, key, args.max_age) is not None:
                print(f"キャッシュは有効です(再取得しません): {CACHE_PATH} "
                      f"[取得元: {provider}]")
                print("強制的に更新するには --force を付けてください。")
                return

    print(f"所要時間を取得中(profile={args.profile}) ...")
    matrix = fetch_and_cache(targets, depots, profile=args.profile)
    print(f"取得完了: {len(matrix)}x{len(matrix)} 地点 -> {CACHE_PATH} に保存しました。")


if __name__ == "__main__":
    main()
