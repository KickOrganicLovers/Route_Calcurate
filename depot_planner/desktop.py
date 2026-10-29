# -*- coding: utf-8 -*-
"""デスクトップアプリとしての入口(パッケージ版の exe / .app はここから起動する)。

GUI のサーバ(server.py)を空いているポートでバックグラウンドに立て、
pywebview の専用ウィンドウで開く。ウィンドウを閉じるとサーバも止まる。
画面は OS 標準の WebView(Windows: Edge WebView2 / macOS: WKWebView)で描く。
"""

import socket
import threading
import time

import uvicorn
import webview

from . import config
from .server import app

TITLE = "補給地点シミュレーター"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    config.ensure_data_dir()
    port = _free_port()
    # log_config=None: ウィンドウアプリでは標準出力が無いので uvicorn にログ設定させない
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="warning", log_config=None))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        if not thread.is_alive():
            raise RuntimeError("GUI のサーバを起動できませんでした")
        time.sleep(0.05)

    webview.settings["ALLOW_DOWNLOADS"] = True                 # 報告書・CSV のダウンロード
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True  # トークン発行ページなど
    webview.create_window(TITLE, f"http://127.0.0.1:{port}/", width=1440, height=900,
                          min_size=(960, 600))
    webview.start()
    server.should_exit = True
    thread.join(timeout=5)


if __name__ == "__main__":
    main()
