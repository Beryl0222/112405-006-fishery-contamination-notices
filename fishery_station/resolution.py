"""实体稳定识别：水域称谓与船只身份。

关键纪律：
- 水域只承认资料 waters 表中的规范名/显式别名；“石湾”“石湾一带”这类
  没有被登记为别名的近名/泛称，一律返回 unresolved，不做子串猜测；
- 船名重复时，优先使用背景登记的 appears_in 编号关联，其次使用“另一船”
  等原文线索 + 当前作业船做相对判断；仍不能确定就返回 ambiguous，
  交由采样人员核实，绝不合并批次。
"""

from __future__ import annotations

from dataclasses import replace

from .models import (
    AreaResolution,
    Boat,
    BoatResolution,
    ParsedRecord,
    WaterArea,
)


class AreaResolver:
    def __init__(self, areas: tuple[WaterArea, ...] | list[WaterArea]):
        self.areas = tuple(areas)
        self._term_index: dict[str, str] = {}
        for a in self.areas:
            self._term_index[a.name] = a.id
            for alias in a.aliases:
                self._term_index[alias] = a.id

    def resolve_surface(self, surface: str) -> tuple[str | None, AreaResolution]:
        if surface in self._term_index:
            return self._term_index[surface], AreaResolution.RESOLVED
        # 只做显式别名的直接匹配；禁止包含/相似度推断
        return None, AreaResolution.UNRESOLVED

    def classify(self, parsed: ParsedRecord) -> ParsedRecord:
        unresolved: list[str] = []
        for s in parsed.area_mentions:
            area_id, status = self.resolve_surface(s)
            if status is AreaResolution.UNRESOLVED:
                unresolved.append(s)
        return replace(parsed, unresolved_area_mentions=tuple(unresolved))

    def primary_area(self, parsed: ParsedRecord) -> tuple[str | None, str | None, AreaResolution]:
        """返回 (area_id, 选中的称谓, 状态)。

        优先选择能在水域表中匹配的称谓；否则取第一个称谓并标记未解析。
        """
        first_surface = parsed.area_mentions[0] if parsed.area_mentions else None
        for surface in parsed.area_mentions:
            area_id, status = self.resolve_surface(surface)
            if status is AreaResolution.RESOLVED:
                return area_id, surface, AreaResolution.RESOLVED
        if first_surface is not None:
            return None, first_surface, AreaResolution.UNRESOLVED
        return None, None, AreaResolution.UNRESOLVED


class BoatResolver:
    def __init__(self, boats: tuple[Boat, ...] | list[Boat]):
        self.boats = tuple(boats)
        self.by_id = {b.id: b for b in self.boats}
        self._pin_index: dict[str, list[str]] = {}
        for b in self.boats:
            for rid in b.appears_in:
                self._pin_index.setdefault(rid, []).append(b.id)

    def pinned_for(self, record_id: str) -> list[str]:
        return list(self._pin_index.get(record_id, ()))

    def resolve_lot_boat(
        self,
        parsed: ParsedRecord,
        context_boat_id: str | None = None,
    ) -> tuple[str | None, BoatResolution, str | None]:
        """为“建立批次的记录”确定唯一船只。

        返回 (boat_id, resolution_code, detail)。
        """
        rid = parsed.record_id
        pinned = self.pinned_for(rid)
        if len(pinned) == 1:
            return pinned[0], BoatResolution.PINNED, "背景登记 appears_in 唯一关联"
        if len(pinned) > 1:
            return None, BoatResolution.AMBIGUOUS, f"appears_in 关联到多艘船：{pinned}"

        # 原文“另一船 X”：在同名候选中取上下文船之外的那一艘
        if parsed.cued_boat_name:
            name = parsed.cued_boat_name
            same_name = [b.id for b in self.boats if b.name == name]
            if parsed.another_boat and len(same_name) >= 2 and context_boat_id in same_name:
                other = [bid for bid in same_name if bid != context_boat_id]
                if len(other) == 1:
                    return other[0], BoatResolution.ANOTHER_BOAT, \
                        f"原文‘另一船{name}’，相对于当前作业船 {context_boat_id}"
            if len(same_name) == 1:
                return same_name[0], BoatResolution.UNIQUE_NAME, f"船名‘{name}’唯一"
            if len(same_name) > 1:
                return None, BoatResolution.AMBIGUOUS, \
                    f"船名‘{name}’对应多艘登记船 {same_name}，缺少‘另一船’等区分线索"
            return None, BoatResolution.UNKNOWN, f"船名‘{name}’不在船只登记表"

        # 没有船名线索时，用 老陈 等别称
        aliased = self.mention_boats(parsed.text)
        if len(aliased) == 1:
            return aliased[0], BoatResolution.OPERATOR_ALIAS, "通过船主别称唯一识别"
        return None, BoatResolution.UNKNOWN, "记录中没有可识别的船名或别称"

    def mention_boats(self, text: str) -> list[str]:
        """识别一段文字中提到的船只，尽量消解重名。

        规则：
        - 别称（老陈、周老大）唯一且强；
        - 登记船名在全表唯一时直接命中；
        - 共享重名（海星）时，在出现位置附近找各船别称来归属；
          找不到别称则不猜（不返回该重名船）。
        """
        out: list[str] = []
        shared_names = {n for n in {b.name for b in self.boats}
                        if sum(1 for b in self.boats if b.name == n) > 1}

        # 强：别称
        for b in self.boats:
            if any(aka and aka in text for aka in b.aka):
                if b.id not in out:
                    out.append(b.id)

        # 唯一船名
        for b in self.boats:
            if b.name not in shared_names and b.name in text and b.id not in out:
                out.append(b.id)

        # 重名 + 邻近别称
        for name in shared_names:
            start = 0
            while True:
                idx = text.find(name, start)
                if idx < 0:
                    break
                window = text[max(0, idx - 8): idx + len(name) + 10]
                candidates = [b for b in self.boats if b.name == name]
                hit = None
                for b in candidates:
                    if any(aka and aka in window for aka in b.aka):
                        hit = b.id
                        break
                if hit and hit not in out:
                    out.append(hit)
                start = idx + len(name)
        return out

    def contact_snapshot(self, boat_id: str | None) -> dict | None:
        if not boat_id or boat_id not in self.by_id:
            return None
        b = self.by_id[boat_id]
        return {
            "boat_id": b.id,
            "name": b.name,
            "registry_no": b.registry_no,
            "operator": b.operator,
            "role": b.role,
            "contact": b.contact,
            "berth": b.berth,
        }
