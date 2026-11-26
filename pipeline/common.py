#!/usr/bin/env python3
"""管线共用层：路径、CSV / JSON 读写、slug 与 uid 规则、简体派生、公告表头与名单表格解析。

阶段脚本（s2 / s3 / s4 / s5）只共享**纯函数与常量**，不互相 import 私有逻辑；
阶段之间仍然只通过 `cache/*.csv` 与 `cache/maming.sqlite` 传递数据（见 docs/IMPL.md §4）。

三条硬规则在本文件里只有一份实现，别的脚本不许重写：
  1. `simplified()` —— 港译 → 简体，逐字，长度必须不变（SCHEMA.md §2.3）
  2. `parse_header()` —— 公告表头零推断（IMPL.md §1）
  3. `read_override()` / `norm_alias()` —— 人工图层只读，简称全量替换（SCHEMA.md §3）
"""
from __future__ import annotations

import csv
import html as H
import json
import re
import unicodedata
import zipfile
from datetime import date, datetime, timezone
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree as ET

# ── 路径 ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "cache"
DOCS = CACHE / "docs"
DATA_SRC = ROOT / "data-src"
PUBLIC_DATA = ROOT / "public" / "data"
DB_DIR = ROOT / "db"

INDEX_CSV = DATA_SRC / "hkjc_entries_index.csv"
OVERRIDES = DATA_SRC / "name-overrides.json"

RACES_CSV = CACHE / "races.csv"
ENTRIES_CSV = CACHE / "entries.csv"
NAMES_CSV = CACHE / "names.csv"
ALIASES_CSV = CACHE / "aliases.csv"
ARCHIVE_CSV = CACHE / "archive.csv"
JBIS_CSV = CACHE / "jbis.csv"
SQLITE = CACHE / "maming.sqlite"

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


# ── 时间 / 文件 ─────────────────────────────────────────────────────────
def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def today() -> str:
    return date.today().isoformat()


def sha256_bytes(b: bytes) -> str:
    return sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    h = sha256()
    with Path(p).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(p: Path, default=None):
    p = Path(p)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def write_json(p: Path, obj, *, indent: int = 1) -> Path:
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=indent), encoding="utf-8")
    return p


# ── CSV ─────────────────────────────────────────────────────────────────
def read_csv(p: Path) -> list[dict]:
    p = Path(p)
    if not p.exists():
        return []
    with p.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(p: Path, rows: list[dict], fields: list[str] | None = None, *, bom: bool = False) -> Path:
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        fields = list(rows[0].keys()) if rows else []
    enc = "utf-8-sig" if bom else "utf-8"
    with p.open("w", encoding=enc, newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in fields})
    return p


# ── slug / uid ──────────────────────────────────────────────────────────
def slug(text: str) -> str:
    """ASCII slug（IMPL.md §7）：NFKD → ascii → 非字母数字转 - → 小写 → 压连字符。"""
    text = unicodedata.normalize("NFKD", text or "")
    text = text.encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return re.sub(r"-{2,}", "-", text)


_CJK = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"


def cjk_slug(text: str) -> str:
    """保留汉字的 slug：赛事名多为中文，转 ascii 会得到空串；保留汉字既稳定又可读。"""
    text = unicodedata.normalize("NFKC", text or "")
    text = text.lower()
    text = re.sub(rf"[^a-z0-9{_CJK}]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text


def short_hash(text: str, n: int = 8) -> str:
    return sha256(text.encode("utf-8")).hexdigest()[:n]


# 地区 → race_uid 前缀。地区只决定 uid 前缀与队列顺序，**不决定抓不抓**（AGENTS.md §2.8）
REGION_PREFIX = {
    "日本": "jra",
    "香港": "hkjc",
    "澳洲/新西兰": "aus",
    "英国/爱尔兰": "gb",
    "法国": "fr",
    "美国/加拿大": "us",
    "中东（杜拜/沙特）": "me",
    "韩国": "kor",
    "新加坡": "sin",
    "南非": "saf",
    "南美": "sam",
    "德国/欧洲其他": "eur",
    "中国内地": "chn",
}
FALLBACK_PREFIX = "misc"

REGION_COUNTRY = {
    "日本": "日本",
    "香港": "中国香港",
    "澳洲/新西兰": "澳洲",
    "英国/爱尔兰": "英国",
    "法国": "法国",
    "美国/加拿大": "美国",
    "中东（杜拜/沙特）": "阿联酋",
    "韩国": "韩国",
    "新加坡": "新加坡",
    "南非": "南非",
    "南美": "南美",
    "德国/欧洲其他": "德国",
    "中国内地": "中国内地",
}


def region_prefix(region: str) -> str:
    return REGION_PREFIX.get(region, FALLBACK_PREFIX)


_TITLE_TAIL = re.compile(
    r"\s*(?:[–—\-]\s*)?(?:最後宣佈出賽馬匹名單及排位|最後宣佈出賽馬匹名單|出賽馬匹名單及排位|"
    r"出賽馬匹名單|排位表|宣佈出賽馬匹).*$"
)
_TITLE_SEP = re.compile(r"\s+[–—\-]\s+")


def race_title_zh(title: str) -> str | None:
    """公告标题 → 赛事中文名（去掉「– 最後宣佈出賽馬匹名單及排位」这类尾巴）。"""
    t = unicodedata.normalize("NFKC", title or "").strip()
    t = _TITLE_SEP.split(t)[0]
    t = _TITLE_TAIL.sub("", t).strip(" -–—")
    return t or None


def race_uid_of(announce_date: str, region: str, title: str) -> str:
    """{prefix}-{year}-{race_slug}（IMPL.md §7）。中文赛事名保留汉字，slug 为空时退哈希。"""
    year = (announce_date or "")[:4] or "0000"
    name = race_title_zh(title) or title or ""
    s = cjk_slug(name)
    if not s:
        s = "r-" + short_hash(title or announce_date)
    return f"{region_prefix(region)}-{year}-{s}"


def race_uid_sub(race_date: str, region: str, name: str) -> str:
    """派生场次（赛马日公告里的副场）的 uid：{prefix}-{year}-{mmdd}-{slug}。

    副场名重复度极高（「三歲以上一捷馬賽」几乎每个赛马日都有），只带年份必撞，
    所以带上月日；同日同名再靠开跑时间消歧。
    """
    d = re.sub(r"\D", "", race_date or "")
    year = d[:4] or "0000"
    mmdd = d[4:8]
    s = cjk_slug(name or "")
    if not s:
        s = "r-" + short_hash(f"{race_date}{name}")
    return f"{region_prefix(region)}-{year}-{mmdd + '-' if mmdd else ''}{s}"


# 产地后缀 → (uid 前缀, 中文名, alpha-3)
ORIGIN = {
    "JPN": ("jp", "日本", "JPN"),
    "USA": ("us", "美国", "USA"),
    "CAN": ("can", "加拿大", "CAN"),
    "GB": ("gb", "英国", "GBR"),
    "IRE": ("ire", "爱尔兰", "IRL"),
    "FR": ("fr", "法国", "FRA"),
    "GER": ("ger", "德国", "DEU"),
    "ITY": ("ity", "意大利", "ITA"),
    "SPA": ("spa", "西班牙", "ESP"),
    "AUS": ("au", "澳洲", "AUS"),
    "NZ": ("nz", "新西兰", "NZL"),
    "SAF": ("saf", "南非", "ZAF"),
    "BRZ": ("brz", "巴西", "BRA"),
    "ARG": ("arg", "阿根廷", "ARG"),
    "CHI": ("chi", "智利", "CHL"),
    "PER": ("per", "秘鲁", "PER"),
    "URU": ("uru", "乌拉圭", "URY"),
    "KOR": ("kor", "韩国", "KOR"),
    "SIN": ("sin", "新加坡", "SGP"),
    "HK": ("hk", "中国香港", "HKG"),
    "CHN": ("chn", "中国内地", "CHN"),
    "UAE": ("uae", "阿联酋", "ARE"),
    "TUR": ("tur", "土耳其", "TUR"),
    "SWE": ("swe", "瑞典", "SWE"),
    "DEN": ("den", "丹麦", "DNK"),
    "NOR": ("nor", "挪威", "NOR"),
    "FIN": ("fin", "芬兰", "FIN"),
    "POL": ("pol", "波兰", "POL"),
    "CZE": ("cze", "捷克", "CZE"),
    "HUN": ("hun", "匈牙利", "HUN"),
    "AUT": ("aut", "奥地利", "AUT"),
    "SWI": ("swi", "瑞士", "CHE"),
    "BEL": ("bel", "比利时", "BEL"),
    "HOL": ("hol", "荷兰", "NLD"),
    "RUS": ("rus", "俄罗斯", "RUS"),
    "IND": ("ind", "印度", "IND"),
    "MAL": ("mal", "马来西亚", "MYS"),
}


def origin_meta(code: str | None) -> tuple[str, str, str]:
    """产地后缀 → (uid 前缀, 中文名, alpha-3)。未知后缀原样保留，不猜国籍。"""
    c = (code or "").upper().strip()
    if c in ORIGIN:
        return ORIGIN[c]
    if not c:
        return "unk", "未知", "UNK"
    return c.lower(), c, c


def horse_uid_of(en: str, origin: str | None) -> str:
    prefix, _, _ = origin_meta(origin)
    return f"{prefix}-{slug(en) or short_hash(en)}"


def person_uid_of(role: str, en: str) -> str:
    """骑手 / 练马师必须带角色前缀，否则同名骑练撞键（IMPL.md §7）。"""
    p = "j" if role == "jockey" else "t"
    return f"{p}-{slug(en) or short_hash(en)}"


# ── 名称 / 性别 ─────────────────────────────────────────────────────────
_SUFFIX_RE = re.compile(r"\(([A-Za-z]{2,3})\)\s*$")

# 骑师名末尾的减磅标注：「Adam Caffrey (-3)」「喬福仁（-3）」。它是告示板上的让磅信息，
# 不是名字的一部分——同一骑师不同场次减磅不同，按名字存会把一个人拆成好几个。
_CLAIM_RE = re.compile(r"[\s\u3000]*[\(\（][\s\u3000]*([-−+＋]?)[\s\u3000]*(\d{1,2})[\s\u3000]*[\)\）][\s\u3000]*$")
# 澳洲告示板还会在名字后标「(a)」= apprentice（見習騎師），同样是标记不是名字。
_APPRENTICE_RE = re.compile(r"[\s\u3000]*[\(\（][\s\u3000]*(?:a|app|appr|apprentice|見習|见习)[\s\u3000]*[\)\）][\s\u3000]*$", re.I)

# 告示板上表示「没有」的占位符。它们是表格填空，不是名字，按名字存会造出空壳实体。
_PLACEHOLDER = {"", "-", "–", "—", "－", "/", "\\", "n/a", "na", "nil", "無", "无"}


def strip_claim(raw: str) -> tuple[str, float | None]:
    """剥掉骑师名末尾的标记 → (干净名字, claim)。

    末尾可能连着出现两个标记（`Logan Bates (a) (-3)`），从右往左剥干净。
    claim 为负数表示减磅（`(-3)` 与无符号的 `(3)` 在告示板里都指减 3 磅），正数表示超磅。
    只剥末尾、只剥括号里的 1–2 位数字，避免误伤真名里的括号。
    """
    s = (raw or "").strip()
    claim: float | None = None
    for _ in range(3):                      # 最多剥三层，防「(a) (-3) (H)」这种叠加
        m = _CLAIM_RE.search(s)
        if m:
            sign, num = m.group(1), int(m.group(2))
            # `(-3)` = 减 3 磅；无符号 `(3)` 在告示板上也是减磅；`(+2)` 才是超磅。
            if claim is None:
                claim = float(num) if sign in ("+", "＋") else -float(num)
            s = s[: m.start()].strip()
            continue
        m = _APPRENTICE_RE.search(s)
        if m:
            s = s[: m.start()].strip()
            continue
        break
    return ("" if s.lower() in _PLACEHOLDER else s), claim


def split_en(raw: str) -> tuple[str, str | None]:
    """'Embroidery (JPN)' → ('Embroidery', 'JPN')。后缀即产地，origin_basis = docx_suffix。"""
    raw = (raw or "").strip()
    m = _SUFFIX_RE.search(raw)
    if m:
        return raw[: m.start()].strip(), m.group(1).upper()
    return raw, None


SEX_MAP = {"C": "M", "H": "M", "M": "F", "F": "F", "G": "G"}
SEX_CN = {"M": "雄", "F": "雌", "G": "骟"}


def norm_sex(raw: str | None) -> str | None:
    """公告性别归一：C/H（雄）→ M，M/F（雌）→ F，G（骟）→ G；其它留空不猜。"""
    s = (raw or "").strip().upper()
    return SEX_MAP.get(s)


# ── 简体（SCHEMA.md §2.3，禁止变动）────────────────────────────────────
def _load_t2s():
    try:
        from zhconv import convert as zc
    except ImportError as e:  # pragma: no cover
        raise ImportError("缺少 zhconv：pip install zhconv（简体列必须逐字派生，不能用 zh-cn）") from e
    return lambda s: zc(s, "zh-hans")


_to_simplified = _load_t2s()


def simplified(text: str | None) -> str | None:
    """港译 → 简体。逐字转换，长度必须不变，否则视为「改了词」，直接抛错。"""
    if not text:
        return None
    out = _to_simplified(text)
    if len(out) != len(text):
        raise ValueError(f"简体转换改变了长度（{text!r} → {out!r}），拒绝写入")
    return out


def norm_key(text: str) -> str:
    """检索归一化：lower + 去空白（SCHEMA.md §4.3）。"""
    return re.sub(r"\s+", "", (text or "").lower())


def norm_variants(text: str) -> list[str]:
    """同一名称的检索键：原样、简体、繁体各一份（繁简双向展开）。"""
    if not text:
        return []
    keys = {norm_key(text)}
    try:
        from zhconv import convert as zc

        keys.add(norm_key(zc(text, "zh-hans")))
        keys.add(norm_key(zc(text, "zh-hant")))
    except ImportError:  # pragma: no cover
        pass
    return [k for k in keys if k]


# ── 人工图层（只读）────────────────────────────────────────────────────
def load_overrides(p: Path = OVERRIDES) -> dict[str, dict]:
    """data-src/name-overrides.json → {uid: patch}。下划线开头的键是元数据，不参与套用。"""
    doc = read_json(p, {}) or {}
    entries = doc.get("entries", doc) if isinstance(doc, dict) else {}
    return {k: v for k, v in entries.items() if not str(k).startswith("_") and isinstance(v, dict)}


def norm_alias(raw) -> list[dict]:
    """简称：`["M.傲"]` 与 `[{"text": "...", "note": "..."}]` 都收；去空、去重、保序。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            text, note = item.strip(), None
        elif isinstance(item, dict):
            text, note = str(item.get("text") or "").strip(), item.get("note")
        else:
            continue
        if not text or text in seen:
            continue
        seen.add(text)
        out.append({"text": text, "note": note or None})
    return out


def apply_name_override(uid: str, names: dict, overrides: dict[str, dict]) -> dict:
    """套用 hk / own 覆盖 → 重算 sc → 套 alias（全量替换）。空值 = 不覆盖。"""
    ov = overrides.get(uid) or {}
    for field in ("hk", "own"):
        val = ov.get(field)
        if isinstance(val, str) and val.strip():
            names[field] = val.strip()
    names["sc"] = simplified(names.get("hk"))
    names["alias"] = norm_alias(ov.get("alias"))
    return names


# ── 公告表头（零推断，IMPL.md §1）────────────────────────────────────────
_NUM = {"〇": 0, "○": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
        "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_DIGITS = "".join(_NUM)
_CN = "一二三四五六七八九十"
GRADE_MAP = {
    "一級賽": "G1", "二級賽": "G2", "三級賽": "G3", "四級賽": "G3", "五級賽": "G3",
    "表列賽": "Listed", "讓賽": "Handicap", "錦標賽": "Handicap",
}


def cn_num(s: str) -> int:
    """逐位数字（一六○○）与数词（二十二、三十一）都能读。"""
    if not re.search(r"[十百]", s):
        return int("".join(str(_NUM[c]) for c in s))
    section = num = 0
    for c in s:
        if c == "十":
            section += (num or 1) * 10
            num = 0
        elif c == "百":
            section += (num or 1) * 100
            num = 0
        else:
            num = _NUM[c]
    return section + num


def parse_header(text: str) -> dict:
    """公告表头 → 级别 / 途程 / 场地 / 赛期 / 马场 / 场次 / 开跑时间。全部来自原文。

    原文形如：
      「一哩冠軍賽 (一級賽) - 一六○○米 草地二○二六年十一月二十二日星期日日本京都競馬場
        海外賽事第一組 第一場  下午2:40   最後宣佈出賽馬匹名單及排位」
    认不出的字段就是没有，不补默认值。
    """
    out: dict = {}
    text = unicodedata.normalize("NFKC", text or "")

    m = re.search(r"[（(]([一二三四五]級賽|表列賽|讓賽)[)）]", text)
    if m:
        out["grade_cn"] = m.group(1)
        out["grade"] = GRADE_MAP.get(m.group(1), m.group(1))

    m = re.search(rf"([{_DIGITS}]{{3,4}})\s*米", text) or re.search(r"(\d{3,4})\s*米", text)
    if m:
        v = m.group(1)
        out["distance"] = f"{(cn_num(v) if v[0] in _NUM else int(v))} 米"

    m = re.search(r"米\s*(草地|泥地|全天候|膠沙地|胶沙地|障礙賽|障碍赛)", text)
    if m:
        out["surface"] = {"胶沙地": "膠沙地", "障碍赛": "障礙賽"}.get(m.group(1), m.group(1))

    m = re.search(rf"([{_DIGITS}]{{4}})年([{_CN}]{{1,3}})月([{_CN}]{{1,3}})日", text)
    if m:
        out["race_date"] = (
            f"{cn_num(m.group(1)):04d}-{cn_num(m.group(2)):02d}-{cn_num(m.group(3)):02d}"
        )
    else:
        m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
        if m:
            out["race_date"] = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

    m = re.search(r"星期[一二三四五六日天]\s*([^\s]{2,24}?(?:競馬場|馬場|跑馬地))", text)
    if not m:
        m = re.search(r"([^\s]{2,24}?(?:競馬場|馬場))", text)
    if m:
        venue = m.group(1)
        out["venue"] = venue
        if venue.startswith("日本"):
            out["venue_country"], out["course"] = "日本", venue[2:]
        elif "香港" in venue:
            out["venue_country"], out["course"] = "中国香港", re.sub(r"^中國?", "", venue)
        else:
            out["course"] = venue

    m = re.search(rf"(海外賽事第[{_CN}\d]+組\s*第[{_CN}\d]+場)", text)
    if not m:
        m = re.search(rf"(第[{_CN}\d]+場)", text)
    if m:
        out["session"] = re.sub(r"\s+", " ", m.group(1))

    m = re.search(r"(上午|下午)\s*(\d{1,2})\s*[:：]\s*(\d{2})", text)
    if m:
        h = int(m.group(2)) % 12 + (12 if m.group(1) == "下午" else 0)
        out["race_time"] = f"{h:02d}:{m.group(3)}"
    return out


# ── 名单表格解析（docx / 内联 HTML 共用同一套列语义）──────────────────────
def _cell_text(s: str) -> str:
    return re.sub(r"\s+", " ", H.unescape(s or "")).strip()


def classify_header(cell: str) -> str | None:
    """单个表头格 → 列语义。

    同一格常写双语（`Horse Name/馬名`、`Jockey/騎師`），此时先认英文词：
    英文列里放的是拉丁名，中文列里放的才是港译，S4 用「有没有汉字」做最后一道筛。
    """
    c = _cell_text(cell).lower()
    if not c:
        return None
    if re.search(r"saddle|馬號|马号", c):
        return "saddle_no"
    if re.search(r"^gear|配備|配备", c):
        return "gear"
    if re.search(r"horse\s*name", c):
        return "horse_en"
    if re.search(r"馬名|马名", c):
        return "horse_zh"
    if re.search(r"draw|排位|檔位|档位", c):
        return "draw"
    if re.search(r"rating|評分|评分", c):
        return "rating"
    if re.search(r"wt\.?|weight|配磅|負磅|负磅|磅", c):
        return "lb"
    if re.search(r"sex|性別|性别", c):
        return "sex"
    if re.search(r"age|年齡|年龄", c):
        return "age"
    if re.search(r"jockey|騎師|骑师", c):
        return "jockey_en" if "jockey" in c else "jockey_zh"
    if re.search(r"trainer|練馬師|练马师", c):
        return "trainer_en" if "trainer" in c else "trainer_zh"
    return None


def looks_like_header(cells: list[str]) -> bool:
    """列头行：同时出现「马名」与「骑/练」两类列，且信息行（整行一个大格子）不算。

    实测两种表：最後宣佈是 11–13 列（含马号/档位/磅重/评分），
    較早的「宣佈出賽馬匹名單」只有 4 列（馬名 / 練馬師，中英各一列），都要认。
    """
    filled = [c for c in cells if _cell_text(c)]
    if len(filled) < 2:
        return False
    roles = {classify_header(c) for c in filled}
    return bool(roles & {"horse_en", "horse_zh"}) and bool(
        roles & {"jockey_en", "jockey_zh", "trainer_en", "trainer_zh"}
    )


_NAME_ROLES = {"horse", "jockey", "trainer"}


def header_roles(cells: list[str]) -> dict[str, int]:
    """表头 → 列下标。合并表头（`馬名` 后面跟一个空格子）按「英文左、中文右」拆成两列。"""
    idx: dict[str, int] = {}
    n = len(cells)
    for i, raw in enumerate(cells):
        role = classify_header(raw)
        if not role or role in idx:
            continue
        base = role.split("_")[0]
        if base in _NAME_ROLES and i + 1 < n and not _cell_text(cells[i + 1]):
            if f"{base}_en" not in idx and f"{base}_zh" not in idx:
                idx[f"{base}_en"], idx[f"{base}_zh"] = i, i + 1
                continue
        idx[role] = i
    # 只有英文列没有中文列（或反之）：让两栏共用同一格，值是不是汉字由 S3 判定
    for base in _NAME_ROLES:
        en, zh = f"{base}_en", f"{base}_zh"
        if en in idx and zh not in idx:
            idx[zh] = idx[en]
        elif zh in idx and en not in idx:
            idx[en] = idx[zh]
    return idx


def parse_entry_rows(table: list[list[str]], ctx_text: str = "") -> tuple[str, list[dict]]:
    """一张名单表 → (表头信息行, 行字典列表)。列按表头语义定位，不按固定下标。"""
    info, hdr_at = "", -1
    for i, row in enumerate(table):
        if looks_like_header(row):
            hdr_at = i
            break
    if hdr_at < 0:
        return info, []
    if hdr_at > 0:
        prev = [_cell_text(c) for c in table[hdr_at - 1]]
        filled_prev = [c for c in prev if c]
        if len(filled_prev) == 1:      # 信息行：整行只有一个大格子（可能带 gridSpan 空格）
            info = filled_prev[0]
    if not info and ctx_text:
        info = ctx_text

    roles = header_roles(table[hdr_at])

    out: list[dict] = []
    for row in table[hdr_at + 1:]:
        filled = [_cell_text(c) for c in row if _cell_text(c)]
        if len(filled) < 2:
            continue
        rec = {role: (_cell_text(row[i]) if i < len(row) else "") for role, i in roles.items()}
        if not (rec.get("horse_en") or "").strip() and not (rec.get("horse_zh") or "").strip():
            continue
        out.append(rec)
    return info, out


def has_cjk(text: str | None) -> bool:
    return bool(re.search(rf"[{_CJK}]", text or ""))


class _TableCollector(HTMLParser):
    """收集 HTML 里所有 <table>，不做 DOM 树；嵌套表各自成一张。

    同时记「上一张表之后到这张表之前」的文字（ctx）：内联表常把赛事信息放在表格外的 <p> 里，
    没有它就只能靠整页文本猜，容易串到前一场。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[tuple[str, list[list[list[str]]]]] = []
        self._stack: list[list[list[list[str]]]] = []
        self._row: list[list[str]] | None = None
        self._cell: list[str] | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            t: list[list[list[str]]] = []
            ctx = re.sub(r"\s+", " ", " ".join(self._text)).strip()[-500:]
            self.tables.append((ctx, t))
            self._text = []
            self._stack.append(t)
        elif tag == "tr" and self._stack:
            self._row = []
            self._stack[-1].append(self._row)
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
            self._row.append(self._cell)

    def handle_endtag(self, tag):
        if tag == "table" and self._stack:
            self._stack.pop()
            self._row = self._cell = None
            self._text = []
        elif tag in ("td", "th"):
            self._cell = None
        elif tag == "tr":
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)
        elif not self._stack:
            self._text.append(data)


def html_tables_with_ctx(html: str) -> list[tuple[str, list[list[str]]]]:
    p = _TableCollector()
    try:
        p.feed(html or "")
    except Exception:  # 坏 HTML 不阻断整篇
        pass
    return [(ctx, [["".join(c) for c in row] for row in t]) for ctx, t in p.tables]


def html_tables(html: str) -> list[list[list[str]]]:
    return [t for _, t in html_tables_with_ctx(html)]


_SPAN_RE = re.compile(r"<w:gridSpan[^>]*w:val=\"(\d+)\"")


def docx_tables(path: Path) -> list[list[list[str]]]:
    """docx → 与 html_tables 同形的三层结构。合并单元格按 gridSpan 补空格，保证列不串。"""
    return [t for _, t in docx_tables_with_ctx(path)]


def docx_tables_with_ctx(path: Path) -> list[tuple[str, list[list[str]]]]:
    """每张表 → (表前文字, 表)。表前文字 = 表内信息行；没有就把上一张表之后的段落拼起来。

    实测：多数公告把「賽事名 - 途程 場地 日期 馬場 場次 時間」写进表格第一行（gridSpan 合并）；
    少数公告（如女皇伊利沙伯二世盃賽馬日的第四場）把它拆成表格前面的 5 个段落。
    """
    p = Path(path)
    if not p.exists():
        return []
    try:
        with zipfile.ZipFile(p) as z:
            root = ET.fromstring(z.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ET.ParseError):
        return []

    body = root.find(W_NS + "body")
    if body is None:
        return []

    out: list[tuple[str, list[list[str]]]] = []
    pending: list[str] = []
    for el in list(body):
        tag = el.tag.split("}")[-1]
        if tag == "p":
            txt = re.sub(r"\s+", " ", "".join(t.text or "" for t in el.iter(W_NS + "t"))).strip()
            if txt:
                pending.append(txt)
            continue
        if tag != "tbl":
            continue
        rows: list[list[str]] = []
        for tr in el.iter(W_NS + "tr"):
            cells: list[str] = []
            for tc in tr.findall(W_NS + "tc"):
                cells.append("".join(t.text or "" for t in tc.iter(W_NS + "t")))
                span = 1
                tcpr = tc.find(W_NS + "tcPr")
                if tcpr is not None:
                    gs = tcpr.find(W_NS + "gridSpan")
                    if gs is not None:
                        try:
                            span = max(1, int(gs.get(W_NS + "val", "1")))
                        except ValueError:
                            span = 1
                cells.extend([""] * (span - 1))
            rows.append(cells)
        out.append((" ".join(pending), rows))
        pending = []
    return out


def visible_text(html: str, limit: int = 40000) -> str:
    """正文可见文本（去 script/style/标签），用于表头兜底解析。"""
    h = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html or "")
    h = re.sub(r"(?is)<br\s*/?>|</p>|</div>|</tr>", " ", h)
    h = re.sub(r"<[^>]+>", " ", h)
    h = H.unescape(h)
    h = re.sub(r"\s+", " ", h)
    return h[:limit]


def docx_text(path: Path, limit: int = 40000) -> str:
    p = Path(path)
    if not p.exists():
        return ""
    try:
        with zipfile.ZipFile(p) as z:
            root = ET.fromstring(z.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ET.ParseError):
        return ""
    txt = " ".join(t.text or "" for t in root.iter(W_NS + "t"))
    return re.sub(r"\s+", " ", txt)[:limit]


def sniff_attach_kind(head: bytes) -> str | None:
    """附件魔数：.docx 是 ZIP（504b0304），.doc 是 OLE2（d0cf11e0）。"""
    if head[:4] == b"PK\x03\x04":
        return "docx"
    if head[:4] == b"\xd0\xcf\x11\xe0":
        return "doc"
    if head[:4] == b"%PDF":
        return "pdf"
    if head[:2] == b"PK":
        return "zip"
    return None
