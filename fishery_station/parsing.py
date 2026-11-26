"""中文来源记录的规则解析。

设计原则：
1. 只做有把握的模式抽取，所有拿不准的内容进入 parse_notes / 由引擎登记 Issue，
   绝不靠模糊相似度猜测实体归属；
2. 每个抽取字段都能回到原记录编号；
3. 相对时间词以 case.json 的 reference_now 为锚点换算，避免使用系统时钟
   造成结果不可复核。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from .models import HaulMention, ParsedRecord, Verdict

# ---------------------------------------------------------------------------
# 常量模式
# ---------------------------------------------------------------------------

REF_RE = re.compile(r"FC-\d+")
LAB_NO_RE = re.compile(r"化验单\s*([A-Za-z0-9]+-[A-Za-z0-9\-]+)")
ANY_LAB_RE = re.compile(r"\b(SYN-LAB-\d+)\b")
METHOD_RE = re.compile(r"按\s*([A-Za-z][A-Za-z0-9\-]*)\s*方法|方法[为是：:]\s*([A-Za-z][A-Za-z0-9\-]*)")
BOX_RE = re.compile(r"箱\s*(\d+)(?:\s*[-–—~～到至]\s*(\d+))?")
HAUL_RE = re.compile(r"第\s*([一二三四五六七八九十\d]+)\s*网")
CHINESE_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
               "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

SPECIES_RE = re.compile(r"[一-龥]{1,2}鱼")

TIME_DAY_WORDS = ("前天", "昨天", "昨日", "昨晚", "今晨", "今早", "今天", "今日")
TIME_PART_WORDS = ("凌晨", "早上", "早晨", "上午", "中午", "下午", "傍晚", "晚上", "晚间")

# 水域表面形式：先匹配带通名的，再处理“石湾一带”
AREA_DAI_RE = re.compile(r"[一-龥]{2,6}?一带")
AREA_SUFFIX_RE = re.compile(r"[一-龥]{2,6}?(?:外口|内澳|礁|湾|港|澳|洋)")
AREA_KOU_RE = re.compile(r"[一-龥]{2,4}口")
AREA_PREFIX_STOP = (
    "前天", "昨天", "昨日", "昨晚", "今晨", "今早", "今天", "今日",
    "凌晨", "早上", "早晨", "上午", "中午", "下午", "傍晚", "晚上", "晚间",
    "我们", "他们", "记得", "记不清", "具体", "还在", "还", "在", "的",
    "那", "这个", "那个", "是", "到", "去", "于", "和", "与",
)

TISSUE_TERMS = ("鳃组织", "肌肉组织", "肝胰腺", "可食部", "内脏组织", "鱼皮")

ANALYTE_TERMS = ("目标残留",)

CLAIM_CUES = ("那片网", "也有", "同一潮", "同一片水", "同潮")


def chinese_to_int(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    if token in CHINESE_NUM:
        return CHINESE_NUM[token]
    # 简单处理“十一”..“十九”
    if len(token) == 2 and token[0] == "十":
        return 10 + CHINESE_NUM.get(token[1], 0)
    if len(token) == 2 and token[1] == "十":
        return CHINESE_NUM.get(token[0], 1) * 10
    return None


def parse_reference_now(value: str | None) -> date:
    if not value:
        return date(2026, 11, 26)
    return datetime.fromisoformat(value).date()


def _resolve_day(word: str | None, ref_day: date) -> tuple[str | None, str | None]:
    """返回 (ISO 日期, 时段标签)。"""
    if not word:
        return None, None
    part: str | None = None
    if word == "前天":
        d = ref_day - timedelta(days=2)
    elif word in ("昨天", "昨日", "昨晚"):
        d = ref_day - timedelta(days=1)
        if word == "昨晚":
            part = "夜间"
    elif word in ("今晨", "今早"):
        d = ref_day
        part = "早晨"
    elif word in ("今天", "今日"):
        d = ref_day
    else:
        return None, None
    return d.isoformat(), part


def _haul_near(match: re.Match, text: str, ref_day: date) -> HaulMention:
    window = text[max(0, match.start() - 16):match.start()]

    # 在窗口内取“最靠近第N网”的时间词，避免更早出现的“昨天”盖住紧邻的“昨晚”
    def _rightmost(words: tuple[str, ...]) -> str | None:
        best_word = None
        best_pos = -1
        for w in words:
            pos = window.rfind(w)
            if pos > best_pos:
                best_pos = pos
                best_word = w
        return best_word

    day_word = _rightmost(TIME_DAY_WORDS)
    part_word = _rightmost(TIME_PART_WORDS)
    iso_day, part = _resolve_day(day_word, ref_day)
    if part_word:
        part = part_word
    seq = chinese_to_int(match.group(1))
    phrase = "".join(w for w in (day_word, part_word) if w) or None
    return HaulMention(
        seq=seq,
        time_phrase=phrase,
        event_date=iso_day,
        part_of_day=part,
        raw=match.group(0),
    )


def _clean_area_token(token: str) -> str:
    changed = True
    while changed:
        changed = False
        for stop in AREA_PREFIX_STOP:
            if token.startswith(stop) and len(token) > len(stop):
                token = token[len(stop):]
                changed = True
    return token


def extract_area_surfaces(text: str) -> list[str]:
    """抽取可能的水域称谓（尚未与水域表核对）。

    各后缀正则会给出不同切分，这里统一清洗前缀时间词/介词，再按
    “短称谓若是另一称谓的严格前缀则丢弃短的”去噪，例如同时得到
    “石湾”和“石湾外口”时保留后者。
    """
    raw: list[str] = []
    for rx in (AREA_DAI_RE, AREA_SUFFIX_RE, AREA_KOU_RE):
        for m in rx.finditer(text):
            token = _clean_area_token(m.group(0))
            if 2 <= len(token) <= 10 and token not in raw:
                raw.append(token)

    surfaces: list[str] = []
    for token in raw:
        if any(other != token and other.startswith(token) for other in raw):
            continue
        if token not in surfaces:
            surfaces.append(token)
    return surfaces


def _extract_boxes(text: str) -> list[int]:
    boxes: list[int] = []
    for m in BOX_RE.finditer(text):
        lo = int(m.group(1))
        if m.group(2):
            hi = int(m.group(2))
            boxes.extend(range(lo, hi + 1))
        else:
            boxes.append(lo)
    # 去重保序
    seen: set[int] = set()
    out = []
    for b in boxes:
        if b not in seen:
            seen.add(b)
            out.append(b)
    return out


def _extract_verdict(text: str) -> Verdict | None:
    if "未检出" in text or "阴性" in text:
        return Verdict.NOT_DETECTED
    if "检出" in text or "阳性" in text:
        return Verdict.DETECTED
    return None


def _extract_tissue_for_box(text: str, box: int | None) -> tuple[str | None, tuple[str, ...]]:
    """优先取“该箱号之后窗口内”的组织名，避免被后文补充说明误导。"""
    notes: list[str] = []
    window = text
    if box is not None:
        m = re.search(rf"箱\s*{box}(?!\d)", text)
        if m:
            window = text[m.end():m.end() + 16]
    for term in TISSUE_TERMS:
        if term in window:
            return term, tuple(notes)
    m = re.search(r"([一-龥]{1,5}组织)", window)
    if m:
        notes.append(f"组织名称由宽松模式识别：{m.group(1)}")
        return m.group(1), tuple(notes)
    return None, tuple(notes)


def _extract_method(text: str) -> str | None:
    m = METHOD_RE.search(text)
    if m:
        return m.group(1) or m.group(2)
    return None


def _extract_analyte(text: str) -> str | None:
    for term in ANALYTE_TERMS:
        if term in text:
            return term
    m = re.search(r"未检出\s*([一-龥A-Za-z0-9]{2,12}?)(?:[，,。；;\s]|$)", text)
    if m:
        return m.group(1)
    m = re.search(r"([一-龥]{2,10}?)(?:结果为)?检出", text)
    if m:
        return m.group(1)
    return None


def _extract_market_place(text: str) -> tuple[str | None, str | None]:
    place = None
    m = re.search(r"已进[入]?\s*([一-龥A-Za-z0-9]{2,20}?市场(?:\s*[A-Za-z]\d+\s*摊位)?)", text)
    if m:
        place = re.sub(r"\s+", "", m.group(1))
    dist = None
    m2 = re.search(r"货在([一-龥A-Za-z0-9]{2,12}?)(?:[。；;]|$)", text)
    if m2:
        dist = "货在" + m2.group(1) + "待售" if "待售" in text[m2.end():m2.end() + 4] else "货在" + m2.group(1)
    return place, dist


def _extract_boat_cue(text: str) -> tuple[str | None, bool]:
    another = ("另一船" in text) or ("另一条船" in text) or ("另一艘" in text)
    m = re.search(r"(?:另一船|另一条船|另一艘|船名)\s*([一-龥A-Za-z0-9]{1,10}?)(?=[：:，,。；;\s]|$)", text)
    return (m.group(1) if m else None), another


def parse_record(record: dict, reference_now: str | None = None) -> ParsedRecord:
    rid = record["id"]
    kind = record.get("kind") or record.get("source") or "unknown"
    text = record["text"]
    ref_day = parse_reference_now(reference_now)

    refs = tuple(REF_RE.findall(text))
    lab_no = None
    m = LAB_NO_RE.search(text)
    if m:
        lab_no = m.group(1)
    else:
        m2 = ANY_LAB_RE.search(text)
        if m2:
            lab_no = m2.group(1)

    cued_boat, another = _extract_boat_cue(text)
    boxes = _extract_boxes(text)
    verdict = _extract_verdict(text)
    tissue, tissue_notes = _extract_tissue_for_box(text, boxes[0] if boxes else None)
    method = _extract_method(text)
    analyte = _extract_analyte(text)
    place, distribution = _extract_market_place(text)
    species = tuple(dict.fromkeys(SPECIES_RE.findall(text)))

    haul_matches = list(HAUL_RE.finditer(text))
    haul = _haul_near(haul_matches[0], text, ref_day) if haul_matches else None
    extra = tuple(_haul_near(m, text, ref_day) for m in haul_matches[1:])

    surfaces = extract_area_surfaces(text)

    claim_quote = None
    if any(c in text for c in CLAIM_CUES):
        if kind == "voice-transcript":
            # 保留转写正文完整原话，而不是只取感叹号前的半句
            body = re.sub(r"^\s*语音转写(?:（[^）]*）)?\s*[：:]\s*", "", text).strip()
            claim_quote = body or text
        else:
            # 取包含线索词的整句（可能多句）
            sentences = re.split(r"(?<=[。！!？?；;])", text)
            hits = [s.strip() for s in sentences if any(c in s for c in CLAIM_CUES)]
            claim_quote = " ".join(hits) if hits else text

    v_lead = v_confirmed = v_excluded = None
    if kind == "verification":
        m = re.search(r"(FC-\d+)\s*所说", text)
        v_lead = m.group(1) if m else (refs[0] if refs else None)
        m = re.search(r"确认是\s*(FC-\d+)", text)
        v_confirmed = m.group(1) if m else None
        m = re.search(r"与\s*(FC-\d+)[^。；;]*?无关", text)
        v_excluded = m.group(1) if m else None

    notes = list(tissue_notes)
    if kind == "lab-result" and method is None:
        notes.append("原文未载明检测方法")
    if kind in ("catch-note", "market-update") and not boxes:
        notes.append("未识别到箱号")

    return ParsedRecord(
        record_id=rid,
        kind=kind,
        text=text,
        refs=refs,
        lab_no=lab_no,
        cued_boat_name=cued_boat,
        another_boat=another,
        area_mentions=tuple(surfaces),
        haul=haul,
        extra_hauls=extra,
        species=species,
        boxes=tuple(boxes),
        verdict=verdict,
        analyte=analyte,
        tissue=tissue,
        method=method,
        market_place=place,
        distribution=distribution,
        claim_quote=claim_quote,
        verification_lead_ref=v_lead,
        confirmed_ref=v_confirmed,
        excluded_ref=v_excluded,
        parse_notes=tuple(notes),
    )
