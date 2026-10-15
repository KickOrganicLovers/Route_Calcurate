# -*- coding: utf-8 -*-
"""全33地点を対象にした補給地点選定シミュレーション結果の docx 出力。

のぼり／くだりでパスを分けず、点1-33 をまとめて 1 つの対象集合として
補給地点 7 地点を選ぶ(simulate_all33.py と同じ計算)。

実行:
    uv run make_report_all33.py
"""

from docx import Document
from docx.shared import Pt
from docx.enum.text import WD_ALIGN_PARAGRAPH

from waypoints import (waypoints, candidate_supply_points, targets_for,
                       NUM_DEPOTS_TO_SELECT)
from depot_selection import select_depots
from travel_time import build_travel_time
from make_report import add_table
from main import SERVICE_MIN, RELOAD_MIN

K_RANGE = range(3, 11)

# 候補から外す拠点(運用側の都合で使えない拠点をここに列挙する)。
# 移動時間のキャッシュキーは全候補の座標から作るため、除外は
# キャッシュ構築の後(ソルバー入力の段階)で行う。
# W3 = 国立女性教育会館
# E7/E8 = 江戸川河川事務所、A1-A3 = 荒川上流河川事務所(利用不可。利根川上流は可)
EXCLUDE_CODES = {"W3", "E7", "E8", "A1", "A2", "A3"}


def solve(targets, candidates, travel_time_min, k):
    return select_depots(targets, candidates, travel_time_min, num_depots=k,
                         service_min=SERVICE_MIN, reload_min=RELOAD_MIN)


def main():
    all_candidates = list(candidate_supply_points)
    targets = targets_for([w["id"] for w in waypoints])
    # キャッシュを使い回すため、移動時間は除外前の全候補で構築する
    travel_time_min = build_travel_time(targets, all_candidates, profile="driving")
    provider = getattr(travel_time_min, "provider", "?")
    candidates = [c for c in all_candidates if c["code"] not in EXCLUDE_CODES]
    excluded = [c for c in all_candidates if c["code"] in EXCLUDE_CODES]

    sensitivity = {k: solve(targets, candidates, travel_time_min, k)
                   for k in K_RANGE}
    sel = sensitivity[NUM_DEPOTS_TO_SELECT]

    # 拠点ごとの担当地点(遅い順)
    by_depot = {}
    for a in sel["assignments"]:
        by_depot.setdefault(a["depot"], []).append(a)
    for rows in by_depot.values():
        rows.sort(key=lambda x: -x["time_min"])

    doc = Document()
    st = doc.styles["Normal"]
    st.font.name = "Yu Gothic"
    st.font.size = Pt(10.5)

    title = doc.add_heading("補給地点選定シミュレーション（全33地点・7拠点）", level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    sub = doc.add_paragraph(
        f"測定局 {len(targets)} 地点（点1〜33 全件）／補給地点 "
        f"{NUM_DEPOTS_TO_SELECT} か所（候補 {len(candidates)} 件から選定）／"
        f"車両 {sel['num_vehicles']} 台（1局1台）　"
        f"メイクスパン {sel['makespan_min']} 分")
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER

    doc.add_heading("0. 前提条件", level=1)
    for line in [
        "対象地点: 点1〜33 の全 33 地点。のぼり／くだりのパス分割は行わず、"
        "1 つの対象集合として補給地点を選定した。",
        f"移動時間: {provider}（道路ネットワーク上の運転時間、profile=driving）。",
        f"作業時間: 配送作業 {SERVICE_MIN} 分／補給地点での積込 {RELOAD_MIN} 分。",
        "車両: 1 局 1 台。1 台の所要時間 = 積込 + 移動（拠点→担当地点）+ 配送作業。",
        "目的関数: メイクスパン（最も遅い車両の所要時間）の最小化。"
        "同点なら総所要時間が小さいほうを採る。",
        f"探索: 候補 {len(candidates)} 件から {NUM_DEPOTS_TO_SELECT} 件を選ぶ"
        "全組合せの総当たり（厳密解）。"
        "拠点あたりの台数上限は設けないため、各地点は最寄り拠点へ割り当てる。",
        "再現: uv run simulate_all33.py ／ 本書は uv run make_report_all33.py",
    ] + (["候補から除外した拠点: "
          + "、".join(f"[{c['code']}] {c['name']}" for c in excluded)
          + "（運用側の都合により今回は選定対象外）"] if excluded else []):
        doc.add_paragraph(line, style="List Bullet")

    # ===== 5. 候補全件 =====
    adopted = {d["code"] for d in sel["depots"]}
    doc.add_heading("1. 補給地点 候補リスト（全件）", level=1)
    kinds = []
    for c in all_candidates:
        if c["kind"] not in kinds:
            kinds.append(c["kind"])
    doc.add_paragraph(
        "道の駅は「道の駅はにゅう」のみを残し、他の道の駅は候補から除外した。"
        "残りは大学・東京ガス各所などの既存施設に、国土交通省 関東地方整備局の"
        "河川事務所・出張所、駐車場付きレンタルスペース、自治体の貸館を加えたもの。"
        "内訳は "
        + "、".join(f"{k} {sum(1 for c in all_candidates if c['kind'] == k)} 件"
                   for k in kinds)
        + f"（計 {len(all_candidates)} 件）。"
        "座標は住所を国土地理院ジオコーダで変換した値"
        "（利根川上流河川事務所 古河出張所のみ実測値）。")
    add_table(doc,
              ["code", "採否", "区分", "方面", "名称", "緯度", "経度", "所在地"],
              [[c["code"],
                "除外" if c["code"] in EXCLUDE_CODES
                else ("採用" if c["code"] in adopted else "-"),
                c["kind"], c["area"], c["name"],
                f"{c['lat']:.6f}", f"{c['lon']:.6f}", c.get("address", "-")]
               for c in all_candidates])

    # ===== 2. 採用拠点 =====
    doc.add_heading("2. シミュレーション結果：採用した補給地点と車両配分", level=1)
    add_table(doc,
              ["拠点", "区分", "方面", "配置台数", "最長所要(分)", "担当地点"],
              [[f"[{d['code']}] {d['name']}", d["kind"], d["area"],
                f"{d['stock']} 台",
                by_depot[d["name"]][0]["time_min"],
                ", ".join("点" + r["target"] for r in by_depot[d["name"]])]
               for d in sel["depots"]]
              + [["合計", "", "", f"{sel['num_vehicles']} 台",
                  sel["makespan_min"], ""]])
    doc.add_paragraph(
        f"総所要 {sel['total_min']} 分／車両 1 台あたり平均 "
        f"{round(sel['total_min'] / sel['num_vehicles'], 1)} 分。")

    dropped = [c for c in candidates
               if c["kind"] == "実績" and c["code"] not in adopted]
    if dropped:
        doc.add_paragraph(
            "落選した実績拠点: "
            + "、".join(f"[{c['code']}] {c['name']}" for c in dropped))

    # ===== 3. 集合動線 =====
    doc.add_heading("3. 採用拠点の周辺情報（所在地・集合駅・レンタカー）", level=1)
    add_table(doc,
              ["拠点", "所在地", "集合駅", "レンタカー営業所"],
              [[f"[{d['code']}] {d['name']}", d.get("address", "-"),
                d.get("station", "要確認"), d.get("rental", "要確認")]
               for d in sel["depots"]])

    # ===== 4. 地点ごとの担当拠点 =====
    doc.add_heading("4. 地点ごとの担当拠点（所要時間の遅い順）", level=1)
    add_table(doc,
              ["順位", "地点", "担当拠点", "所要(分)"],
              [[i, "点" + a["target"], a["depot"], a["time_min"]]
               for i, a in enumerate(
                   sorted(sel["assignments"], key=lambda x: -x["time_min"]), 1)])

    # ===== 5. 感度分析 =====
    doc.add_heading("5. 採用地点数を変えた場合（感度分析）", level=1)
    add_table(doc,
              ["拠点数", "メイクスパン(分)", "総所要(分)", "採用拠点（台数）"],
              [[k, s["makespan_min"], s["total_min"],
                " / ".join(f"{d['code']}:{d['name']}({d['stock']})"
                           for d in s["depots"])]
               for k, s in sensitivity.items()])

    out = "docs/補給地点選定シミュレーション_全33地点_7拠点.docx"
    doc.save(out)
    print(f"出力: {out}")
    print(f"メイクスパン {sel['makespan_min']} 分 / 総所要 {sel['total_min']} 分")


if __name__ == "__main__":
    main()
