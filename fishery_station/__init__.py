"""沿岸检验站批次归属与放行范围工作台（合成演练资料）。

模块划分：
- models:   不可变领域对象与状态码
- parsing:  中文来源记录的规则解析（保留出处，不做无依据猜测）
- resolution: 水域别名 / 重名船只的稳定实体识别
- engine:   批次、样本适用范围、只追加通知台账与待办
- present:  可复核 JSON 视图与中文 Markdown 报告
- cli:      命令行入口
"""

from .engine import StationEngine, build_case
from .models import CaseState

__all__ = ["StationEngine", "build_case", "CaseState"]
__version__ = "0.2.0"
