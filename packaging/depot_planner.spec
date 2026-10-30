# -*- mode: python ; coding: utf-8 -*-
# 配布用の exe(Windows)/ .app(macOS)を作る PyInstaller の設定。
#     uv sync --extra desktop --group build
#     uv run pyinstaller packaging/depot_planner.spec --noconfirm
# 出力: dist/DepotPlanner/DepotPlanner.exe(Windows) / dist/DepotPlanner.app(macOS)
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).parent
NAME = "DepotPlanner"

datas = [
    (str(ROOT / "depot_planner" / "static"), "depot_planner/static"),
    (str(ROOT / "scenarios"), "scenarios"),   # 初回起動時にデータフォルダへコピーするサンプル
]
binaries = []
hiddenimports = collect_submodules("uvicorn")
for pkg in ("ortools", "docx"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    [str(ROOT / "packaging" / "launch.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name=NAME, upx=False)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{NAME}.app",
        bundle_identifier="io.github.kickorganiclovers.depotplanner",
        info_plist={
            "CFBundleDisplayName": "補給地点シミュレーター",
            "CFBundleShortVersionString": "0.2.0",
            "NSHighResolutionCapable": True,
        },
    )
