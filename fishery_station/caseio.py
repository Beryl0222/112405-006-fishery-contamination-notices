"""读取合成案例 JSON。"""

from __future__ import annotations

import json
from pathlib import Path


def load_case(path: str | Path) -> dict:
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8"))
