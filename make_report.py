# -*- coding: utf-8 -*-
"""発表用レポート(docx)生成。

ロジックの大枠の説明 + 実行結果(経由地点名付き)を 1 つの docx にまとめる。

実行:
    uv run make_report.py
"""

from docx import Document
from docx.shared import Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH

from waypoints import waypoints, supply_points
from distribute_solver import solve_distribution
from mapbox import build_travel_time
from main import build_inputs


def add_heading(doc, text, level):
    h = doc.add_heading(text, level=level)
    return h


def main():
    # ---- 実行結果を取得 ----
    targets, depots = build_inputs()
    params = dict(num_vehicles=6, speed_kmph=40, service_min=10,
                  reload_min=5, time_limit_sec=15)
    travel_time_min = build_travel_time(targets, depots, profile="driving")
    result = solve_distribution(targets, depots,
                                travel_time_min=travel_time_min, **params)

    doc = Document()

    # 既定フォント(日本語)
    style = doc.styles["Normal"]
    style.font.name = "Yu Gothic"
    style.font.size = Pt(10.5)

    # ===== タイトル =====
    title = doc.add_heading("配分ルート最適化プログラム", level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    sub = doc.add_paragraph("— ロジック概要と実行結果 —")
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER

    # ===== 1. 何をするプログラムか =====
    add_heading(doc, "1. プログラムの目的", level=1)
    doc.add_paragraph(
        "複数の配送先へ、複数の補給地点を拠点とする車両群で物資を配り切るときの"
        "最適な巡回ルートを求めるプログラム。"
        "「最も時間のかかる車両の所要時間(メイクスパン)」を最小化し、"
        "全車両がなるべく同時に・早く作業を終えられる配車を計算する。")

    # ===== 2. 前提(モデル) =====
    add_heading(doc, "2. 前提となるモデル", level=1)
    for t in [
        "車1台が一度に運べるのは1地点分(容量1)。"
        "「補給地点で積む → 1地点へ配る → 補給地点へ戻る」を繰り返すシャトル動作。",
        "車両と補給地点に固定の所属はない。在庫が尽きない限り、"
        "どの車両もどの補給地点からでも積んでよい。",
        "各補給地点は在庫(stock)個ぶんだけ供給できる。",
    ]:
        doc.add_paragraph(t, style="List Bullet")

    # ===== 3. ロジックの大枠 =====
    add_heading(doc, "3. ロジックの大枠", level=1)
    doc.add_paragraph(
        "Google OR-Tools の経路最適化(Routing)を用いる。"
        "問題を「ノードの連鎖」に変換し、制約と目的を与えて解かせる。")
    steps = [
        ("入力の検証", "補給地点の在庫合計が配送先数を満たすか確認する。"),
        ("ノード化", "配送先・補給ユニット(在庫1個=1ノード)・"
                     "全車両共通の仮想スタートを一列のノードに並べる。"),
        ("時間次元", "移動時間(Mapbox Matrix API による道路上の実測運転時間)に"
                     "作業・積込時間を加え、各ルートの所要時間を定義する。"),
        ("メイクスパン最小化", "ルート所要時間の最大値に大きな重みを掛け、"
                     "最遅ルートの短縮を最優先で最小化する。"),
        ("容量制約", "容量を1に固定し、積み直さずに連続配送することを禁止する。"),
        ("任意訪問", "使わない在庫(補給ユニット)はスキップ可能にする。"),
        ("探索", "初期解を生成し、Guided Local Search で改善する(時間制限つき)。"),
        ("解の出力", "ノードの連鎖を人間が読める順路へ復元する。"),
    ]
    for i, (name, desc) in enumerate(steps, 1):
        p = doc.add_paragraph(style="List Number")
        run = p.add_run(f"{name}: ")
        run.bold = True
        p.add_run(desc)

    # ===== 4. 入力データ =====
    add_heading(doc, "4. 入力データ", level=1)
    doc.add_paragraph(
        f"配送先: {len(targets)} 地点 / "
        f"補給地点: {len(depots)} か所(在庫合計 {sum(d['stock'] for d in depots)}) / "
        f"車両: {params['num_vehicles']} 台")
    doc.add_paragraph(
        f"速度 {params['speed_kmph']} km/h、"
        f"配送作業 {params['service_min']} 分/件、"
        f"積込 {params['reload_min']} 分/回、"
        f"計算時間上限 {params['time_limit_sec']} 秒。")

    tbl = doc.add_table(rows=1, cols=3)
    tbl.style = "Light Grid Accent 1"
    hdr = tbl.rows[0].cells
    hdr[0].text, hdr[1].text, hdr[2].text = "補給地点", "在庫", "参照地点"
    for s in supply_points:
        c = tbl.add_row().cells
        c[0].text = s["name"]
        c[1].text = str(s["stock"])
        c[2].text = f"地点{s['ref_waypoint']}"

    # ===== 5. 実行結果 =====
    add_heading(doc, "5. 実行結果", level=1)
    if result is None:
        doc.add_paragraph("解なし(条件を確認してください)。")
    else:
        p = doc.add_paragraph()
        r = p.add_run(f"メイクスパン: {result['makespan_min']} 分")
        r.bold = True
        r.font.size = Pt(12)
        r.font.color.rgb = RGBColor(0xC0, 0x00, 0x00)

        # 経由地点名の対応表
        name_by = {w["name"]: w for w in waypoints}

        for route in result["routes"]:
            used = "、".join(route["depots_used"]) if route["depots_used"] else "-"
            add_heading(doc,
                        f"車両{route['vehicle']}  "
                        f"(配送 {route['deliveries']} 件 / 所要 {route['time_min']} 分)",
                        level=2)
            doc.add_paragraph(f"使用した補給地点: {used}")

            if not route["steps"]:
                doc.add_paragraph("(未使用)")
                continue

            # 順路テーブル(経由地点名付き)
            rt = doc.add_table(rows=1, cols=3)
            rt.style = "Light List Accent 1"
            h = rt.rows[0].cells
            h[0].text, h[1].text, h[2].text = "順番", "種別", "地点"
            order = 0
            for step in route["steps"]:
                order += 1
                c = rt.add_row().cells
                c[0].text = str(order)
                if step.startswith("配"):
                    wp_name = step[step.index("[") + 1: step.index("]")]
                    wp = name_by.get(wp_name)
                    c[1].text = "配送"
                    if wp:
                        c[2].text = (f"地点{wp['name']} "
                                     f"(緯度{wp['lat']:.4f}, 経度{wp['lon']:.4f})")
                    else:
                        c[2].text = f"地点{wp_name}"
                else:  # 補(...)
                    depot_name = step[step.index("(") + 1: step.index(")")]
                    c[1].text = "補給"
                    c[2].text = depot_name

            # 1行表記の順路も併記
            arrow = " → ".join(
                ("配:地点" + s[s.index("[") + 1:s.index("]")]) if s.startswith("配")
                else ("補:" + s[s.index("(") + 1:s.index(")")])
                for s in route["steps"])
            doc.add_paragraph(arrow)

    out = "docs/配分ルート最適化_レポート.docx"
    doc.save(out)
    print(f"出力: {out}")


if __name__ == "__main__":
    main()
