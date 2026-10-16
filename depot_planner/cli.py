# -*- coding: utf-8 -*-
"""コマンドライン入口。

    uv run depot-planner gui                      GUI を起動(ブラウザが開く)
    uv run depot-planner run scenarios/x.json     シナリオを実行して runs/ に保存
    uv run depot-planner run x --capacity 3       積載量を一時的に変えて実行
    uv run depot-planner run x --vehicles 10      車両の総数を 10 台にして実行
    uv run depot-planner search "目吹出張所"       Mapbox で地点検索(座標付き)
    uv run depot-planner add scenarios/x.json --candidate "目吹出張所" --group 国交省
    uv run depot-planner new "新しいシナリオ"      空のシナリオを作成
    uv run depot-planner list                     保存済みシナリオ一覧
    uv run depot-planner token                    Mapbox トークンを(再)設定
"""

import argparse
import json
import sys
import time
import webbrowser
from pathlib import Path

from . import config, mapbox_api, report, runner
from . import scenario as scn


def _utf8():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass


def _resolve(path_or_name):
    """パスとして存在すればそれを、無ければ scenarios/ 配下の名前として解決する。"""
    p = Path(path_or_name)
    if p.exists():
        return p
    q = scn.scenario_path(path_or_name)
    if q.exists():
        return q
    raise SystemExit(f"シナリオが見つかりません: {path_or_name}")


def _latlon(text):
    if not text:
        return None
    lat, lon = (float(x) for x in text.split(","))
    return lat, lon


def cmd_gui(a):
    # GUI ではターミナルで入力させず、画面のダイアログで入力・検証・保存する
    token = config.get_token()
    if not token:
        print(f"[depot-planner] Mapbox トークンが {config.ENV_PATH} にありません。GUI の画面で入力してください。")
    else:
        ok, msg = config.validate_token(token)
        if not ok:
            print(f"[depot-planner] 設定済みの Mapbox トークンが使えません({msg})。GUI の画面で入力し直してください。")
    import uvicorn
    from .server import app
    url = f"http://{a.host}:{a.port}/"
    print(f"[depot-planner] GUI: {url}  (終了は Ctrl+C)")
    if not a.no_browser:
        webbrowser.open(url)
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


def cmd_run(a):
    path = _resolve(a.scenario)
    sc = scn.load(path)
    if a.k:
        sc["params"]["num_depots"] = a.k
    rt = sc["params"]["routing"]
    if a.no_routing:
        rt["enabled"] = False
    if a.capacity is not None:
        rt["vehicle_capacity"] = a.capacity
    if a.max_deliveries is not None:
        rt["max_deliveries"] = a.max_deliveries
    if a.vehicles is not None:
        rt["num_vehicles"] = a.vehicles or None      # 0 なら自動に戻す
    if a.time_limit is not None:
        rt["time_limit_sec"] = a.time_limit
    if sc["params"]["provider"] in ("mapbox", "auto") and not a.no_fetch:
        config.ensure_token_interactive()

    show = not a.quiet and sys.stderr.isatty()
    last = [0.0]

    def progress(frac, msg):
        # 端末のときだけ 1 行を上書き表示する(パイプ時はログを汚さない)
        now = time.monotonic()
        if show and (now - last[0] > 0.2 or frac >= 1.0):
            last[0] = now
            print(f"\r[{frac * 100:5.1f}%] {msg:<50}", end="", file=sys.stderr, flush=True)

    try:
        result = runner.run_scenario(sc, progress=progress, allow_fetch=not a.no_fetch)
    except runner.ScenarioError as e:
        if show:
            print(file=sys.stderr)
        raise SystemExit(f"シナリオに問題があります:\n{e}")
    if show:
        print(file=sys.stderr)
    if a.json:
        print(report.to_json(result))
    else:
        print(runner.format_text(result))
    if not a.no_save:
        d = report.save_all(result, a.out, docx=not a.no_docx)
        print(f"\n保存先: {d}", file=sys.stderr)


def cmd_search(a):
    config.ensure_token_interactive()
    places = mapbox_api.forward(a.query, proximity=_latlon(a.near), limit=a.limit)
    if a.json:
        print(json.dumps(places, ensure_ascii=False, indent=2))
        return
    if not places:
        print("見つかりませんでした")
    for i, pl in enumerate(places, 1):
        print(f"{i}. {pl['name']}  ({pl['lat']}, {pl['lon']})")
        print(f"   {pl['address']}  [{pl['feature_type']}{' / ' + pl['category'] if pl['category'] else ''}]")


def cmd_add(a):
    path = _resolve(a.scenario)
    sc = scn.load(path)
    kind = "targets" if a.target else "candidates"
    query = a.target or a.candidate
    if a.latlon:
        lat, lon = _latlon(a.latlon)
        place = {"name": query, "address": "", "lat": lat, "lon": lon, "mapbox_id": None}
    else:
        config.ensure_token_interactive()
        places = mapbox_api.forward(query, proximity=_latlon(a.near), limit=max(a.pick, 5))
        if len(places) < a.pick:
            raise SystemExit(f"検索結果が {len(places)} 件しかありません: {query}")
        place = places[a.pick - 1]
    item = {"name": a.name or place["name"], "lat": place["lat"], "lon": place["lon"],
            "address": place.get("address", ""), "mapbox_id": place.get("mapbox_id"),
            "note": a.note or ""}
    if kind == "candidates":
        item["group"] = a.group or ""
        item["status"] = a.status
    sc[kind].append(item)
    scn.save(sc, path)
    added = scn.load(path)[kind][-1]
    print(f"追加しました [{added['id']}] {added['name']} ({added['lat']}, {added['lon']}) {added['address']}")


def cmd_new(a):
    path = scn.scenario_path(a.name)
    if path.exists() and not a.force:
        raise SystemExit(f"既に存在します: {path}(上書きは --force)")
    scn.save(scn.new_scenario(a.name), path)
    print(f"作成しました: {path}")


def cmd_list(a):
    for s in scn.list_scenarios():
        print(f"{s['file']:<40} {s['name']}  目的地 {s['targets']} / 補給地点 {s['candidates']}")


def cmd_token(a):
    token = config.get_token()
    if token and not a.force:
        ok, msg = config.validate_token(token)
        print(f"設定済みのトークン: {msg}")
        if ok:
            print("置き換える場合は --force を付けてください。")
            return
    if a.force and config.get_token():
        # 置き換え時は既存値を無視して入力させる
        import os
        for k in config.TOKEN_KEYS:
            os.environ.pop(k, None)
        config.save_token("")
    config.ensure_token_interactive()


def main(argv=None):
    _utf8()
    ap = argparse.ArgumentParser(prog="depot-planner", description="補給地点選定シミュレーション")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gui", help="GUI を起動")
    g.add_argument("--host", default="127.0.0.1")
    g.add_argument("--port", type=int, default=8765)
    g.add_argument("--no-browser", action="store_true")
    g.set_defaults(func=cmd_gui)

    r = sub.add_parser("run", help="シナリオを実行")
    r.add_argument("scenario", help="シナリオの JSON パス、または scenarios/ 内の名前")
    r.add_argument("--k", type=int, help="採用数を一時的に上書き")
    r.add_argument("--capacity", type=int, metavar="N", help="1 台に積み込める機材の数を一時的に上書き")
    r.add_argument("--max-deliveries", type=int, metavar="N",
                   help="1 台あたりの配送件数上限(積載量より大きくすると積み直しを許す)")
    r.add_argument("--vehicles", type=int, metavar="N",
                   help="車両の総数を一時的に上書き(0 で自動 = 各拠点で 1 回に積み切れる台数)")
    r.add_argument("--time-limit", type=int, metavar="SEC", help="ルート探索時間[秒]を一時的に上書き")
    r.add_argument("--no-routing", action="store_true", help="ルート最適化を行わず選定だけ")
    r.add_argument("--out", help="保存先ディレクトリ(既定: runs/<日時_名前>)")
    r.add_argument("--json", action="store_true", help="結果を JSON で標準出力")
    r.add_argument("--no-save", action="store_true", help="runs/ に保存しない")
    r.add_argument("--no-docx", action="store_true", help="docx を作らない")
    r.add_argument("--no-fetch", action="store_true", help="API を呼ばずキャッシュだけで計算")
    r.add_argument("-q", "--quiet", action="store_true")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("search", help="Mapbox で地点を検索")
    s.add_argument("query")
    s.add_argument("--near", help="この付近を優先 (lat,lon)")
    s.add_argument("--limit", type=int, default=5)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_search)

    ad = sub.add_parser("add", help="検索結果をシナリオに追加")
    ad.add_argument("scenario")
    grp = ad.add_mutually_exclusive_group(required=True)
    grp.add_argument("--target", metavar="QUERY", help="目的地として追加")
    grp.add_argument("--candidate", metavar="QUERY", help="補給地点として追加")
    ad.add_argument("--pick", type=int, default=1, help="検索結果の何番目を使うか")
    ad.add_argument("--near", help="この付近を優先 (lat,lon)")
    ad.add_argument("--latlon", help="検索せず座標を直接指定 (lat,lon)。QUERY は名称になる")
    ad.add_argument("--name", help="表示名を上書き")
    ad.add_argument("--group", help="補給地点の区分")
    ad.add_argument("--status", choices=scn.STATUSES, default="candidate")
    ad.add_argument("--note")
    ad.set_defaults(func=cmd_add)

    n = sub.add_parser("new", help="空のシナリオを作成")
    n.add_argument("name")
    n.add_argument("--force", action="store_true")
    n.set_defaults(func=cmd_new)

    ls = sub.add_parser("list", help="保存済みシナリオ一覧")
    ls.set_defaults(func=cmd_list)

    t = sub.add_parser("token", help="Mapbox トークンを設定")
    t.add_argument("--force", action="store_true", help="設定済みでも入力し直す")
    t.set_defaults(func=cmd_token)

    a = ap.parse_args(argv)
    a.func(a)


if __name__ == "__main__":
    main()
