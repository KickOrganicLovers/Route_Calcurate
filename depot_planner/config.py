# -*- coding: utf-8 -*-
"""パスと Mapbox アクセストークンの管理。

トークンは 環境変数 → データフォルダの .env の順に探す。
キー名は MAPBOX_ACCESS_TOKEN(旧プロジェクト互換で NEXT_PUBLIC_MAPBOX_ACCESS_TOKEN も読む)。
見つからなければ CLI はターミナルで、GUI は画面上のダイアログで入力させ、
検証してから .env に保存する。

データ(.env / scenarios / runs / cache)の置き場所:
    ソースから実行  … プロジェクト直下(従来どおり)
    パッケージ版    … ユーザーのデータフォルダ
                      Windows: %APPDATA%\\depot-planner
                      macOS:   ~/Library/Application Support/depot-planner
    環境変数 DEPOT_PLANNER_HOME を指定するとそちらを優先する。
"""

import getpass
import json
import os
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

APP_NAME = "depot-planner"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# PyInstaller などで固めた実行ファイルか(同梱ファイルは sys._MEIPASS 配下に展開される)
FROZEN = bool(getattr(sys, "frozen", False))
BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", PROJECT_ROOT))


def user_data_dir():
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / APP_NAME


DATA_ROOT = Path(os.environ.get("DEPOT_PLANNER_HOME") or (user_data_dir() if FROZEN else PROJECT_ROOT))
ENV_PATH = DATA_ROOT / ".env"
SCENARIO_DIR = DATA_ROOT / "scenarios"
RUNS_DIR = DATA_ROOT / "runs"
CACHE_PATH = DATA_ROOT / "cache" / "travel_times.json"
BUNDLED_SCENARIO_DIR = BUNDLE_ROOT / "scenarios"


def ensure_data_dir():
    """データフォルダを作る。シナリオが 1 つも無ければ同梱のサンプルをコピーする。"""
    SCENARIO_DIR.mkdir(parents=True, exist_ok=True)
    if BUNDLED_SCENARIO_DIR.resolve() == SCENARIO_DIR.resolve() or any(SCENARIO_DIR.glob("*.json")):
        return
    for src in BUNDLED_SCENARIO_DIR.glob("*.json"):
        shutil.copy2(src, SCENARIO_DIR / src.name)

TOKEN_KEY = "MAPBOX_ACCESS_TOKEN"
TOKEN_KEYS = (TOKEN_KEY, "NEXT_PUBLIC_MAPBOX_ACCESS_TOKEN")
TOKEN_CHECK_URL = "https://api.mapbox.com/tokens/v2"


# ---------------------------------------------------------------------------
# .env の読み書き
# ---------------------------------------------------------------------------
def read_env_file(path=ENV_PATH):
    """KEY=VALUE 形式の .env を dict で返す(無ければ空)。"""
    values = {}
    path = Path(path)
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip().strip('"').strip("'")
    return values


def get_token(env_path=ENV_PATH):
    """環境変数 → .env の順にトークンを探す。無ければ None。"""
    for key in TOKEN_KEYS:
        if os.environ.get(key):
            return os.environ[key].strip()
    values = read_env_file(env_path)
    for key in TOKEN_KEYS:
        if values.get(key):
            return values[key]
    return None


def save_token(token, env_path=ENV_PATH):
    """.env の MAPBOX_ACCESS_TOKEN 行を書き換える(他の行はそのまま残す)。"""
    env_path = Path(env_path)
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    out, replaced = [], False
    for line in lines:
        if line.split("=", 1)[0].strip() == TOKEN_KEY:
            if not replaced:
                out.append(f"{TOKEN_KEY}={token}")
                replaced = True
            continue
        out.append(line)
    if not replaced:
        out.append(f"{TOKEN_KEY}={token}")
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    # 同じプロセス内の以降の呼び出しにも反映させる
    os.environ[TOKEN_KEY] = token


# ---------------------------------------------------------------------------
# 検証
# ---------------------------------------------------------------------------
def validate_token(token):
    """Mapbox にトークンを問い合わせる。(ok, message) を返す。"""
    if not token:
        return False, "トークンが空です"
    url = TOKEN_CHECK_URL + "?" + urllib.parse.urlencode({"access_token": token})
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read().decode("utf-8"))
        except Exception:
            return False, f"検証に失敗しました (HTTP {e.code})"
    except OSError as e:
        return False, f"Mapbox に接続できません: {e}"
    code = data.get("code")
    if code == "TokenValid":
        return True, "有効なトークンです"
    return False, f"無効なトークンです ({code})"


def ensure_token_interactive(env_path=ENV_PATH):
    """トークンを返す。未設定/無効ならターミナルで入力させて .env に保存する。

    標準入力が端末でない(パイプ実行など)場合は入力できないので例外にする。
    """
    token = get_token(env_path)
    if token:
        ok, msg = validate_token(token)
        if ok:
            return token
        print(f"[depot-planner] 設定済みの Mapbox トークンが使えません: {msg}")
    else:
        print(f"[depot-planner] Mapbox アクセストークンが {env_path} にありません。")

    if not sys.stdin.isatty():
        raise RuntimeError(
            f"Mapbox トークンが未設定です。{env_path} に {TOKEN_KEY}=pk.... を書くか、"
            "端末から `uv run depot-planner token` を実行してください。")

    print("https://account.mapbox.com/access-tokens/ で発行した pk. から始まるトークンを入力してください。")
    while True:
        token = getpass.getpass("MAPBOX_ACCESS_TOKEN (入力は表示されません): ").strip()
        ok, msg = validate_token(token)
        if ok:
            save_token(token, env_path)
            print(f"[depot-planner] {env_path} に保存しました。")
            return token
        print(f"  {msg}。もう一度入力してください(中断は Ctrl+C)。")
