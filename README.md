# route_calcurate — 補給地点選定・配分ルート最適化シミュレーション

目的地（機材と人員のセットを届ける地点）と補給地点の候補から、

1. **補給地点の選定** … 最も遅い車両の所要時間が最小になる補給地点の組合せを選び、各拠点に置く機材の数を決める
2. **配分ルート最適化** … 1 台に積める機材の数（積載量）を踏まえ、Google OR-Tools で車両ごとの順路を決める

を計算するツール。GUI と CLI の両方から、同じシナリオファイル（`scenarios/*.json`）を実行できる。

## セットアップ

```
uv sync
```

Mapbox のアクセストークン（`pk.` から始まるもの）は `.env` の `MAPBOX_ACCESS_TOKEN`
（旧名 `NEXT_PUBLIC_MAPBOX_ACCESS_TOKEN` も可）に置く。
未設定なら起動時に入力を求められ（GUI は画面のダイアログ、CLI はターミナル）、検証したうえで `.env` に保存される。
後から差し替えるときは `uv run depot-planner token --force`。

## GUI

```
uv run depot-planner gui          # http://127.0.0.1:8765/ がブラウザで開く
```

- **目的地 / 補給地点タブ**: 検索ボックスに施設名や住所を入れ、Mapbox の候補から選んで追加する。
  地図をクリックしてその地点を追加することもできる。マーカーはドラッグで位置を微調整できる。
  補給地点には「区分」（報告書の色分け）と「状態」（候補 / 必須 / 除外）とメモを付けられる。
- **条件タブ**: 採用数、作業時間・積込時間、移動手段、目的関数、感度分析の範囲、
  **ルート最適化（1 台に積める機材の数・1 台の配送上限・車両の総数・探索時間）**。
- **結果タブ**: 最長所要時間、採用拠点と機材数・車両台数、**車両ごとの順路**（行を押すと地図でその車両を強調）、
  採用数ごとの最長所要時間のグラフ、目的地ごとの担当。Word 報告書 / CSV / JSON をダウンロードできる。
  地図には車両ごとの順路を Mapbox Directions API で取った道路沿いの線で描く（取得できない車両は立ち寄り順の直線）。
- シナリオは「保存」で `scenarios/<名前>.json` に書き出される（CLI と共通）。

## デスクトップアプリ（Windows exe / macOS .app）

ブラウザではなく専用のウィンドウで GUI を開く。中身は上の GUI と同じ（pywebview で表示）。

```
uv sync --extra desktop
uv run depot-planner-app                                   # ソースから専用ウィンドウで起動

uv sync --extra desktop --group build
uv run pyinstaller packaging/depot_planner.spec --noconfirm # dist/ に exe / .app を作る
```

PyInstaller は動かした OS 向けにしか作れないので、配布物は GitHub Actions（`.github/workflows/package.yml`）で作る。
アプリに関わるファイルを変えて push するか、Actions タブの「package」→ Run workflow で Windows / macOS の zip が Artifacts に出る。
`v0.3.0` のようなタグを push すると Releases にも添付される。

- 動作には**インターネット接続が必要**（地図・検索・所要時間はすべて Mapbox）。
- パッケージ版のデータ（`.env` のトークン・シナリオ・実行結果・キャッシュ）は次のフォルダに置く。
  初回起動時に同梱のサンプルシナリオをコピーする。環境変数 `DEPOT_PLANNER_HOME` で変更できる。
  - Windows: `%APPDATA%\depot-planner`
  - macOS: `~/Library/Application Support/depot-planner`
- 署名していないので、初回は警告が出る。
  - Windows: SmartScreen の「詳細情報」→「実行」
  - macOS: アプリを右クリック →「開く」（それでも開けなければ `xattr -dr com.apple.quarantine DepotPlanner.app`）
- Windows は Edge WebView2 ランタイムを使う（Windows 10/11 には標準で入っている）。

## CLI

```
uv run depot-planner list                                   # 保存済みシナリオ
uv run depot-planner run saitama_all33                      # 実行 → runs/<日時_名前>/ に保存
uv run depot-planner run saitama_all33 --capacity 3         # 1 台に機材 3 個まで積める場合
uv run depot-planner run saitama_all33 --capacity 2 --max-deliveries 4   # 2 個ずつ、積み直して最大 4 か所
uv run depot-planner run saitama_all33 --vehicles 10        # 車両の総数を 10 台に固定（0 で自動）
uv run depot-planner run saitama_all33 --k 6 --no-routing   # 採用数だけ変えて選定のみ
uv run depot-planner run scenarios/x.json --json --no-save  # 結果を JSON で標準出力
uv run depot-planner search "利根川上流河川事務所 目吹"       # 地点検索（座標付き）
uv run depot-planner add saitama_all33 --candidate "川越市役所" --group 公共施設
uv run depot-planner add saitama_all33 --target "点34" --latlon 36.0,139.5
uv run depot-planner new "新しいシナリオ"
```

`run` の `--capacity` / `--max-deliveries` / `--vehicles` / `--time-limit` / `--no-routing` / `--k` はその実行だけの上書きで、
シナリオファイルは書き換えない。

`runs/<日時_名前>/` には `result.json`（全結果）、`assignments.csv`（担当一覧）、
`routes.csv`（車両ごとの立ち寄り順）、`report.docx`（報告書）が入る。

### 同梱シナリオ

| ファイル | 内容 |
|---|---|
| `saitama_all33.json` | 埼玉 全 33 地点、K=7。利用不可の W3 / E7 / E8 / A1-A3 は除外 |
| `saitama_up23.json` | 点 1-23 のみ（点 24-33 は無効化）。旧 `main.py` の対象 |

## シナリオ JSON

```json
{
  "version": 1,
  "name": "埼玉 全33地点",
  "description": "",
  "targets":    [{"id": "T01", "name": "点1", "lat": 36.22, "lon": 139.49, "address": "", "enabled": true, "note": ""}],
  "candidates": [{"id": "P1", "name": "東洋大学(川越)", "lat": 35.93, "lon": 139.45, "address": "...",
                  "group": "実績", "status": "candidate", "note": ""}],
  "params": {
    "num_depots": 7, "service_min": 10, "reload_min": 5,
    "profile": "driving", "provider": "mapbox", "objective": "makespan",
    "sensitivity": {"enabled": true, "min": 3, "max": 10},
    "max_exact_combinations": 5000000,
    "routing": {"enabled": true, "vehicle_capacity": 1, "num_vehicles": null, "max_deliveries": null, "time_limit_sec": 30}
  }
}
```

| 項目 | 意味 |
|---|---|
| `targets[].enabled` | false なら計算から外す（削除せずに一時的に外したいとき） |
| `candidates[].status` | `candidate`=選定対象 / `required`=必ず採用 / `excluded`=選定しない |
| `profile` | `driving` / `driving-traffic` / `walking` / `cycling` |
| `provider` | `mapbox` / `osrm`（公開デモサーバ、driving のみ）/ `auto`（Mapbox が失敗したら OSRM） |
| `objective` | `makespan`=最も遅い車両を最小化 / `total`=全車両の合計を最小化（選定段階） |
| `max_exact_combinations` | 組合せ数がこれ以下なら総当たりで厳密解、超えたら貪欲法＋入替え探索の近似解 |
| `routing.enabled` | false ならルート最適化をせず選定だけ |
| `routing.vehicle_capacity` | 1 台に積み込める機材の数（＝1 回の積込で配れる地点数） |
| `routing.num_vehicles` | 車両の総数。`null` なら各拠点の機材を 1 回で積み切れる台数（自動）。指定すると機材数に比例して各拠点へ割り振る |
| `routing.max_deliveries` | 1 台あたりの配送件数上限。`null` なら台数自動のとき積載量と同じ（積み直しなし）、台数指定のとき上限なし。大きくすると補給地点へ戻って積み直す往復を許す |
| `routing.time_limit_sec` | ルート探索にかける秒数 |

## モデル

**選定（`depot_planner/solver.py`）**
- 各目的地へ補給地点から直行する前提で、所要時間 = 積込 + 移動（補給地点 → 目的地）+ 作業。
- 補給地点あたりの上限はないので、選んだ拠点集合に対しては各目的地を最寄りの拠点へ割り当てるのが最適。
  各拠点に置く機材の数 = その拠点が担当する目的地の数。
- 「下限」は、全候補を使っても最長所要がそれ未満にならない値（最も遠い目的地と、その最寄り候補の所要時間）。

**ルート最適化（`depot_planner/routing.py`）**
- 車両の総数が未指定なら、各拠点には機材を 1 回で積み切れる台数 `ceil(機材数 / 積載量)` の車両が待機する。積載量 1 なら 1 目的地 1 台。
- 総数を指定した場合は、機材のある拠点へまず 1 台ずつ（機材の多い順）、残りを機材数に比例して（ドント方式）配置する。
  台数が足りなければ、車両は補給地点へ戻って積み直しながら複数回に分けて配る。
- 車両は「補給地点で最大 C 個積む → 順に配る →（上限が許せば）補給地点へ戻って積み直す」を繰り返す。
  車両と補給地点の所属は固定しない（在庫が残っていればどの拠点から積んでもよい）。
- 目的: 最も遅い車両の所要時間を最小化。補給地点への帰路は所要に含めない。
- 時間制限つきの局所探索（Guided Local Search）なので、厳密な最適とは限らない。
- 積載量が 2 以上のとき、選定段階の最長所要（直行前提）は参考値になる。実際の所要はルート最適化の結果を見る。

## 所要時間のキャッシュ

`cache/travel_times.json` に「地点 → 地点」の座標ペア単位で保存する。
地点を追加しても、その地点に関わるペアだけを Mapbox Matrix API から取得する。
ルート最適化では「目的地 → 目的地」「目的地 → 補給地点」も必要なので、採用拠点と目的地の全ペアを使う。
`--no-fetch` を付けるとキャッシュだけで計算する（足りないペアがあればエラー）。

## ファイル構成

| パス | 役割 |
|---|---|
| `depot_planner/cli.py` | CLI 入口（`uv run depot-planner ...`） |
| `depot_planner/desktop.py`, `packaging/` | デスクトップアプリの入口と PyInstaller の設定 |
| `depot_planner/server.py`, `static/` | GUI（FastAPI + Mapbox GL JS。見た目は IBM Carbon Design System、`static/vendor/carbon/` に `@carbon/styles` のビルド済み CSS を同梱） |
| `depot_planner/runner.py` | シナリオ 1 本の実行（選定 → ルート最適化）。GUI / CLI 共通 |
| `depot_planner/solver.py` | 補給地点の選定（p-center / p-median） |
| `depot_planner/routing.py` | 配分ルート最適化（OR-Tools） |
| `depot_planner/travel_times.py`, `mapbox_api.py` | 所要時間の取得とキャッシュ、Mapbox 検索・道路沿いの線 |
| `depot_planner/scenario.py` | シナリオ JSON の読み書きと検証 |
| `depot_planner/report.py` | JSON / CSV / docx の書き出し |
| `scenarios/` | シナリオファイル |
| `docs/` | 仕様書・解説・候補地の調査メモ・提出済み報告書 |

## テスト

```
uv run pytest
```
