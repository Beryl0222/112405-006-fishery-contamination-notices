"""仓库根目录便捷入口：等价于 `python3 -m fishery_station`。"""

from fishery_station.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
