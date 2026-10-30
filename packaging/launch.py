# PyInstaller の起動スクリプト(パッケージ内の相対 import を通すため、モジュールとして呼ぶ)
from depot_planner.desktop import main

if __name__ == "__main__":
    main()
