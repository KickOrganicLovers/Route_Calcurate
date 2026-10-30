# -*- coding: utf-8 -*-
"""GUI のバックエンド(FastAPI)。127.0.0.1 でのみ待ち受ける前提のローカルアプリ。

Mapbox の検索はこのサーバ経由で呼ぶ。地図表示(Mapbox GL JS)だけは
ブラウザ側でトークンが必要なため /api/config で渡す(pk. の公開トークン)。
"""

import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, mapbox_api, report, runner
from . import scenario as scn
from .solver import Cancelled

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="depot-planner")


# ---------------------------------------------------------------------------
# 設定・トークン
# ---------------------------------------------------------------------------
class TokenIn(BaseModel):
    token: str


@app.get("/api/config")
def get_config():
    token = config.get_token()
    return {"token_configured": bool(token), "token": token or "",
            "defaults": scn.new_scenario(), "statuses": scn.STATUS_LABEL}


@app.post("/api/token")
def set_token(body: TokenIn):
    token = body.token.strip()
    ok, msg = config.validate_token(token)
    if not ok:
        raise HTTPException(400, msg)
    config.save_token(token)
    return {"ok": True, "message": f"{config.ENV_PATH} に保存しました", "token": token}


# ---------------------------------------------------------------------------
# 検索(Mapbox Search Box API のプロキシ)
# ---------------------------------------------------------------------------
def _mapbox(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except mapbox_api.MapboxError as e:
        raise HTTPException(502, str(e))


@app.get("/api/search/suggest")
def search_suggest(q: str, session: str, lat: float | None = None, lon: float | None = None,
                   country: str = "jp", language: str = "ja", limit: int = 8):
    prox = (lat, lon) if lat is not None and lon is not None else None
    return _mapbox(mapbox_api.suggest, q, session, proximity=prox, country=country or None,
                   language=language, limit=limit)


@app.get("/api/search/retrieve")
def search_retrieve(id: str, session: str, language: str = "ja"):
    return _mapbox(mapbox_api.retrieve, id, session, language=language)


@app.get("/api/search/reverse")
def search_reverse(lat: float, lon: float, language: str = "ja"):
    return _mapbox(mapbox_api.reverse, lat, lon, language=language)


# ---------------------------------------------------------------------------
# シナリオの保存・読込
# ---------------------------------------------------------------------------
@app.get("/api/scenarios")
def list_scenarios():
    return scn.list_scenarios()


@app.get("/api/scenarios/{file}")
def get_scenario(file: str):
    path = scn.scenario_path(file)
    if not path.exists():
        raise HTTPException(404, "シナリオが見つかりません")
    return {"file": path.name, "scenario": scn.load(path)}


@app.put("/api/scenarios/{file}")
def put_scenario(file: str, body: dict):
    try:
        path = scn.save(body, scn.scenario_path(file))
    except (KeyError, TypeError, ValueError) as e:
        raise HTTPException(400, f"シナリオの形式が不正です: {e}")
    return {"file": path.name, "scenario": scn.load(path)}


@app.post("/api/validate")
def validate(body: dict):
    try:
        return {"errors": scn.validate(scn.normalize(body))}
    except (KeyError, TypeError, ValueError) as e:
        return {"errors": [f"シナリオの形式が不正です: {e}"]}


# ---------------------------------------------------------------------------
# 実行(バックグラウンドのジョブとして走らせ、進捗をポーリングで返す)
# ---------------------------------------------------------------------------
JOBS = {}
_jobs_lock = threading.Lock()


def _run_job(job, sc):
    def progress(frac, msg):
        job["progress"], job["message"] = round(frac, 4), msg

    try:
        result = runner.run_scenario(sc, progress=progress, cancel=lambda: job["cancel"])
        job["result"] = result
        job["run_dir"] = str(report.save_all(result, docx=False))
        job["status"], job["progress"], job["message"] = "done", 1.0, "完了"
    except Cancelled:
        job["status"], job["message"] = "cancelled", "中止しました"
    except runner.ScenarioError as e:
        job["status"], job["error"] = "error", str(e)
    except Exception as e:  # API エラーなども画面に出す
        job["status"], job["error"] = "error", f"{type(e).__name__}: {e}"
        job["trace"] = traceback.format_exc()
    job["finished_at"] = time.time()


@app.post("/api/runs")
def start_run(body: dict):
    try:
        sc = scn.normalize(body)
    except (KeyError, TypeError, ValueError) as e:
        raise HTTPException(400, f"シナリオの形式が不正です: {e}")
    errors = scn.validate(sc)
    if errors:
        raise HTTPException(400, "\n".join(errors))
    job_id = uuid.uuid4().hex[:12]
    job = {"id": job_id, "status": "running", "progress": 0.0, "message": "開始",
           "cancel": False, "result": None, "error": None, "started_at": time.time()}
    with _jobs_lock:
        JOBS[job_id] = job
    threading.Thread(target=_run_job, args=(job, sc), daemon=True).start()
    return {"id": job_id}


def _job(job_id):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "ジョブが見つかりません")
    return job


@app.get("/api/runs/{job_id}")
def get_run(job_id: str):
    job = _job(job_id)
    return {k: v for k, v in job.items() if k not in ("cancel", "trace", "geometry", "geometry_lock")}


@app.post("/api/runs/{job_id}/cancel")
def cancel_run(job_id: str):
    _job(job_id)["cancel"] = True
    return {"ok": True}


def _vehicle_line(v, profile):
    points = [(v["start_lat"], v["start_lon"])] + [(s["lat"], s["lon"]) for s in v["stops"]]
    try:
        return v["no"], mapbox_api.directions_line(points, profile=profile), None
    except mapbox_api.MapboxError as e:
        return v["no"], None, str(e)


@app.get("/api/runs/{job_id}/geometry")
def run_geometry(job_id: str):
    """車両ごとの順路を道路沿いの線で返す(Directions API)。取れなかった車両は null。

    結果ごとに 1 回だけ取得してジョブに覚えておく(地図の描画専用。保存はしない)。
    """
    job = _job(job_id)
    result = job.get("result")
    if not result:
        raise HTTPException(409, "結果がありません")
    with job.setdefault("geometry_lock", threading.Lock()):
        if "geometry" not in job:
            vehicles = (result.get("routing") or {}).get("vehicles") or []
            profile = result["params"]["profile"]
            with ThreadPoolExecutor(max_workers=4) as pool:
                rows = list(pool.map(lambda v: _vehicle_line(v, profile), vehicles))
            errors = sorted({e for _, _, e in rows if e})
            job["geometry"] = {"vehicles": {str(no): line for no, line, _ in rows},
                               "errors": errors}
    return job["geometry"]


@app.get("/api/runs/{job_id}/export/{fmt}")
def export_run(job_id: str, fmt: str):
    job = _job(job_id)
    result = job.get("result")
    if not result:
        raise HTTPException(409, "結果がありません")
    stem = scn.safe_filename(result["scenario_name"])[:-5]
    if fmt == "json":
        return Response(report.to_json(result), media_type="application/json",
                        headers=_attachment(f"{stem}_result.json"))
    if fmt == "csv":
        return Response(report.to_csv(result), media_type="text/csv; charset=utf-8",
                        headers=_attachment(f"{stem}_assignments.csv"))
    if fmt == "routes":
        text = report.to_routes_csv(result)
        if text is None:
            raise HTTPException(404, "ルート最適化の結果がありません")
        return Response(text, media_type="text/csv; charset=utf-8",
                        headers=_attachment(f"{stem}_routes.csv"))
    if fmt == "docx":
        path = Path(job["run_dir"]) / "report.docx"
        report.to_docx(result, path)
        return FileResponse(path, filename=f"{stem}_report.docx",
                            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    raise HTTPException(404, "形式は json / csv / routes / docx のいずれか")


def _attachment(filename):
    from urllib.parse import quote
    return {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"}


# ---------------------------------------------------------------------------
# 静的ファイル(画面)
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
