"""把已解析的一场公告整理成站点演示数据（v3：名称档案 + 简体派生）。

来源全部真实、可回溯：
  cache/_probe/race_entries.json   ← 2026-11-20 一哩冠軍賽 FinalDecl_C.docx（已归档）
  cache/_probe/jbis_horses.json    ← JBIS 检索页（日文原名 / 父 / 母 / 生年）
  data-src/hkjc_entries_index.csv  ← S0/S1 产出的 956 条公告索引

本轮口径变化（与用户确认后的模型）：
  · 只记录「抓取到的比赛页 + 名称」，不做马匹生涯/赛果/名次维护。
  · 每个实体统一四栏：原文 / 英文 / 港译 / 自译。缺失就留 null，页面显示待接入，不猜。
  · 每个实体另带 sc（简体中文名）：由**港译逐字转简体**，只换字形，不改词、
    不补字、不拟名；转换后长度必须与原串一致，否则直接报错。
  · 日本籍马关联血统（JBIS）；其他国籍 pedigree = null，但字段结构完全一致，
    以后只需回填数据，页面不用改。

用法：python pipeline/_explore/make_demo_json.py
产出：src/data/demo.json
"""
from __future__ import annotations

import csv
import io
import json
import re
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENTRIES = ROOT / "cache/_probe/race_entries.json"
JBIS = ROOT / "cache/_probe/jbis_horses.json"
INDEX_CSV = ROOT / "data-src/hkjc_entries_index.csv"
OVERRIDES = ROOT / "data-src/name-overrides.json"
OUT_DEMO = ROOT / "src/data/demo.json"

ANNOUNCE_DATE = "2026-11-20"
RACE_UID = "jra-2026-mile-championship"
ARTICLE_URL = (
    "https://racingnews.hkjc.com/chinese/2026/11/20/"
    "%e4%b8%80%e5%93%a9%e5%86%a0%e8%bb%8d%e8%b3%bd%e6%9c%80%e5%be%8c%e5%ae%a3%e4%bd%88"
    "%e5%87%ba%e8%b3%bd%e9%a6%ac%e5%8c%b9%e5%90%8d%e5%96%ae%e5%8f%8a%e6%8e%92%e4%bd%8d/"
)
ARTICLE_ARCHIVE = "https://web.archive.org/web/20261124081540/" + ARTICLE_URL
DOCX_URL = (
    "https://res.hkjc.com/racingnews/wp-content/uploads/sites/2/2026/11/"
    "20261120_MileChampionship_FinalDecl_C.docx"
)
DOCX_ARCHIVE = "https://web.archive.org/web/20261124080654/" + DOCX_URL

SEX_CN = {"C": "雄", "H": "雄", "G": "骟", "F": "雌", "M": "雌"}
COUNTRY = {
    "JPN": "日本", "FR": "法国", "GB": "英国", "IRE": "爱尔兰", "USA": "美国",
    "AUS": "澳洲", "NZ": "新西兰", "GER": "德国", "CAN": "加拿大", "HK": "中国香港",
    "SAF": "南非", "KOR": "韩国", "SIN": "新加坡", "CHN": "中国内地",
}
PREFIX = {"JPN": "jp", "FR": "fr", "GB": "gb", "IRE": "ire", "USA": "us", "AUS": "au", "NZ": "nz"}

# 非日本籍的骑练：原文即拉丁拼写。国籍按公开资料人工标注（公告本身只有姓名，
# 不给国籍），仅用于「国籍」列，不参与任何译名判断。可扩充。
FOREIGN_PERSONS = {
    "j-mickael-barzalona": ("FR", "法国"),
    "j-christophe-lemaire": ("FR", "法国"),
    "j-cristian-demuro": ("IT", "意大利"),
    "j-joao-moreira": ("BR", "巴西"),
    "j-alexis-pouchin": ("FR", "法国"),
    "t-francis-henri-graffard": ("FR", "法国"),
}

# ── 港译 → 简体（逐字，禁止变动）──────────────────────────────
# 规则只有一条：繁 → 简的字形替换，不改词、不补字、不拟名，转换前后长度必须相等。
# 首选 zhconv 的 zh-hans 表（MediaWiki 字形级转换，不含 zh-cn 的词汇替换）；
# 缺库时退回下面这张最小字表（只覆盖当前语料里出现过的繁体字，新增语料请装 zhconv）。
_TRAD = "軍賽龍門繡沖運銀幣嬌艷滿輝懸絕預約綻夢實現後衛來將華樂濱貫貴貝紀誠學彌橋"
_SIMP = "军赛龙门绣冲运银币娇艳满辉悬绝预约绽梦实现后卫来将华乐滨贯贵贝纪诚学弥桥"
_TRAD += "馬會體歲練騎師級場館"
_SIMP += "马会体岁练骑师级场馆"
_FALLBACK_T2S = {t: s for t, s in zip(_TRAD, _SIMP)}
assert len(_TRAD) == len(_SIMP)


def _load_converter():
    try:
        from zhconv import convert as zc

        return lambda s: zc(s, "zh-hans"), "zhconv/zh-hans"
    except ImportError:  # pragma: no cover - 仅缺库时触发
        import warnings

        warnings.warn("未安装 zhconv，简体列退回内置字表（可能漏转新字）", RuntimeWarning)
        return (lambda s: "".join(_FALLBACK_T2S.get(c, c) for c in s)), "builtin"


_to_simplified, S2S_ENGINE = _load_converter()


def simplified(text: str | None) -> str | None:
    """港译 → 简体。逐字转换，长度必须不变，否则视为「改了词」，直接失败。"""
    if not text:
        return None
    out = _to_simplified(text)
    if len(out) != len(text):
        raise ValueError(f"简体转换改变了长度（{text!r} → {out!r}），拒绝写入")
    return out


# ── 人工图层：data-src/name-overrides.json ──────────────────
# 脚本只读不写。_ 开头的键是元数据，不参与套用。值留空视为「不覆盖」，不是「清空」。
def load_overrides() -> dict[str, dict]:
    if not OVERRIDES.exists():
        return {}
    doc = json.loads(OVERRIDES.read_text(encoding="utf-8"))
    entries = doc.get("entries", doc)
    return {k: v for k, v in entries.items() if not k.startswith("_") and isinstance(v, dict)}


def norm_alias(raw) -> list[dict]:
    """简称：允许 ["M.傲"] 或 [{"text":"M.傲","note":"…"}] 两种写法，去空去重保序。"""
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


def apply_override(uid: str, names: dict, overrides: dict[str, dict]) -> dict:
    """套用人工改动。顺序固定：先改港译/自译，再重算简体（简体永远跟着港译走）。"""
    ov = overrides.get(uid) or {}
    for field in ("hk", "own"):
        val = ov.get(field)
        if isinstance(val, str) and val.strip():
            names[field] = val.strip()
    if names.get("hk"):
        names["sc"] = simplified(names["hk"])
    names["alias"] = norm_alias(ov.get("alias"))
    return names


def slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return re.sub(r"-{2,}", "-", text)


def split_en(raw: str) -> tuple[str, str | None]:
    m = re.search(r"\(([A-Z]{2,3})\)\s*$", raw)
    if m:
        return raw[: m.start()].strip(), m.group(1)
    return raw.strip(), None


# ── 公告表头解析（真实字段，不靠推断）──────────────────────────
_NUM = {"〇": 0, "○": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
        "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_DIGITS = "".join(_NUM)
_CN = "一二三四五六七八九十"


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


GRADE_MAP = {"一級賽": "G1", "二級賽": "G2", "三級賽": "G3", "表列賽": "Listed", "讓賽": "Handicap"}


def parse_header(text: str) -> dict:
    """「一哩冠軍賽 (一級賽) - 一六○○米 草地 二○二六年十一月二十二日星期日 日本京都競馬場…」

    拆成级别 / 途程 / 场地 / 赛期 / 马场 / 场次 / 开跑时间。全部来自公告原文。
    """
    out: dict = {}
    m = re.search(r"[（(]([一二三四五六七八九]級賽|表列賽|讓賽)[)）]", text)
    if m:
        out["grade"] = GRADE_MAP.get(m.group(1), m.group(1))
        out["grade_cn"] = m.group(1)

    m = re.search(rf"([{_DIGITS}]{{3,4}})米", text)
    if m:
        out["distance"] = f"{cn_num(m.group(1))} 米"

    m = re.search(r"米\s*(草地|泥地|全天候|膠沙地)", text)
    if m:
        out["surface"] = m.group(1)

    m = re.search(rf"([{_DIGITS}]{{4}})年([{_CN}]{{1,3}})月([{_CN}]{{1,3}})日", text)
    if m:
        out["race_date"] = f"{cn_num(m.group(1)):04d}-{cn_num(m.group(2)):02d}-{cn_num(m.group(3)):02d}"

    m = re.search(r"星期[一二三四五六日天]\s*([^\s]+?(?:競馬場|馬場))", text)
    if m:
        venue = m.group(1)
        out["venue"] = venue
        out["venue_country"] = "日本" if venue.startswith("日本") else ("中国香港" if "香港" in venue else None)
        out["course"] = re.sub(r"^日本", "", venue)

    m = re.search(rf"(海外賽事第[{_CN}\d]+組\s*第[{_CN}\d]+場)", text)
    if m:
        out["session"] = m.group(1)

    m = re.search(r"(上午|下午)(\d{1,2}):(\d{2})", text)
    if m:
        h = int(m.group(2)) % 12 + (12 if m.group(1) == "下午" else 0)
        out["race_time"] = f"{h:02d}:{m.group(3)}"
    return out


def read_header() -> dict:
    """从已归档 docx 的第一行取表头文本。"""
    import zipfile
    from xml.etree import ElementTree as ET

    p = ROOT / "cache/_probe/snap.docx"
    if not p.exists():
        return {}
    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    root = ET.fromstring(zipfile.ZipFile(p).read("word/document.xml"))
    tables = list(root.iter(W + "tbl"))
    if not tables:
        return {}
    first = next(iter(tables[0].iter(W + "tr")), None)
    if first is None:
        return {}
    text = "".join(t.text or "" for t in first.iter(W + "t")).strip()
    return parse_header(text)


def main() -> None:
    rows = json.loads(ENTRIES.read_text(encoding="utf-8"))
    jbis = json.loads(JBIS.read_text(encoding="utf-8")) if JBIS.exists() else {}
    overrides = load_overrides()

    horses: list[dict] = []
    persons: dict[str, dict] = {}
    entries: list[dict] = []

    for row in rows:
        en, origin = split_en(row["en"])
        origin = origin or "UNK"
        uid = f"{PREFIX.get(origin, origin.lower())}-{slug(en)}"
        jb = jbis.get(en) or {}
        has_pedigree = bool(jb.get("jp"))

        jockey_uid = "j-" + slug(row["jockey_en"])
        trainer_uid = "t-" + slug(row["trainer_en"])

        horses.append(
            {
                "uid": uid,
                "no": row["no"],
                "draw": row["draw"],
                "gear": row["gear"],
                "origin": origin,
                "country": COUNTRY.get(origin, origin),
                "sex": row["sex"],
                "sex_cn": SEX_CN.get(row["sex"], row["sex"]),
                "age": row["age"],
                "rating": row["rating"],
                "lb": row["lb"],
                "names": apply_override(
                    uid,
                    {
                        # 日本马取 JBIS カタカナ；其他国籍的「原文」就是拉丁名本身
                        "original": jb.get("jp") if has_pedigree else en,
                        "en": en,
                        "hk": row["zh"],
                        "sc": simplified(row["zh"]),
                        "own": None,
                    },
                    overrides,
                ),
                "pedigree": (
                    {
                        "source": "JBIS",
                        "jbis_id": jb.get("jbis_id"),
                        "birth_year": jb.get("birth_year"),
                        "sire": jb.get("sire"),
                        "dam": jb.get("dam"),
                        "damsire": None,
                        "trainer_jp": jb.get("trainer_jp"),
                        "fetched_at": "2026-11-24",
                    }
                    if has_pedigree
                    else None
                ),
                "status": "maintained" if origin == "JPN" else "pending",
                "jockey_uid": jockey_uid,
                "trainer_uid": trainer_uid,
                "race_uid": RACE_UID,
            }
        )

        for role, zh, en_name in (
            ("jockey", row["jockey_zh"], row["jockey_en"]),
            ("trainer", row["trainer_zh"], row["trainer_en"]),
        ):
            p_uid = ("j-" if role == "jockey" else "t-") + slug(en_name)
            if p_uid in persons:
                persons[p_uid]["horses"].append(uid)
                continue
            japanese = p_uid not in FOREIGN_PERSONS
            nat_code, nat_cn = ("JPN", "日本") if japanese else FOREIGN_PERSONS[p_uid]
            persons[p_uid] = {
                "uid": p_uid,
                "role": role,
                "role_cn": "骑手" if role == "jockey" else "练马师",
                "nationality": nat_code,
                "nationality_cn": nat_cn,
                "names": apply_override(
                    p_uid,
                    {
                        # 日本籍人名的原文就是汉字；非日本籍原文即拉丁拼写
                        "original": zh if japanese else en_name,
                        "en": en_name,
                        "hk": zh,
                        "sc": simplified(zh),
                        "own": None,
                    },
                    overrides,
                ),
                "horses": [uid],
            }

        entries.append(
            {
                "no": row["no"],
                "draw": row["draw"],
                "gear": row["gear"],
                "horse_uid": uid,
                "jockey_uid": jockey_uid,
                "trainer_uid": trainer_uid,
                "lb": row["lb"],
                "rating": row["rating"],
                "sex": row["sex"],
                "age": row["age"],
            }
        )

    horses.sort(key=lambda h: h["no"])
    persons_list = sorted(persons.values(), key=lambda p: (p["role"] != "jockey", p["names"]["hk"]))

    # 地区台账：只用于「维护范围」说明，不改抓取口径
    regions: dict[str, int] = {}
    if INDEX_CSV.exists():
        for r in csv.DictReader(io.open(INDEX_CSV, encoding="utf-8-sig")):
            regions[r["region"]] = regions.get(r["region"], 0) + 1
    region_rows = [
        {
            "region": k,
            "announcements": v,
            "status": "curated" if k == "日本" else "pending",
        }
        for k, v in sorted(regions.items(), key=lambda kv: -kv[1])
    ]

    head = read_header()

    data = {
        "_meta": {
            "schema": "names/v3",
            "updated": "2026-11-24",
            "note": "单场公告演示数据；全量 956 篇解析后结构不变。sc = 港译逐字转简体（派生列），alias = 人工简称",
        },
        "race": {
            "uid": RACE_UID,
            "names": apply_override(
                RACE_UID,
                {
                    "original": "マイルチャンピオンシップ",
                    "en": "Mile Championship",
                    "hk": "一哩冠軍賽",
                    "sc": simplified("一哩冠軍賽"),
                    "own": None,
                },
                overrides,
            ),
            "announce_date": ANNOUNCE_DATE,
            "race_date": head.get("race_date"),
            "race_time": head.get("race_time"),
            "session": head.get("session"),
            "grade": head.get("grade"),
            "grade_cn": head.get("grade_cn"),
            "venue": head.get("venue"),
            "venue_country": head.get("venue_country"),
            "course": head.get("course"),
            "distance": head.get("distance"),
            "surface": head.get("surface"),
            "entries": entries,
        },
        "horses": horses,
        "persons": persons_list,
        "sources": {
            "article": {"url": ARTICLE_URL, "archive_url": ARTICLE_ARCHIVE, "kind": "article"},
            "attachment": {"url": DOCX_URL, "archive_url": DOCX_ARCHIVE, "kind": "attachment"},
            "jbis": {"url": "https://www.jbis.or.jp/", "kind": "pedigree"},
        },
        "regions": region_rows,
        "index_total": sum(regions.values()) if regions else 0,
    }

    OUT_DEMO.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    got = sum(1 for h in horses if h["pedigree"])
    print(f"horses={len(horses)} persons={len(persons_list)} 血统已接入={got}/{len(horses)}")
    all_names = [h["names"] for h in horses] + [p["names"] for p in persons_list] + [data["race"]["names"]]
    changed = sum(1 for n in all_names if n["sc"] and n["sc"] != n["hk"])
    print(f"简体引擎={S2S_ENGINE} 逐字有变化={changed}/{len(all_names)}（余下与港译同形）")
    with_alias = [n for n in all_names if n["alias"]]
    print(f"人工图层：override {len(overrides)} 条，其中带简称 {len(with_alias)} 个实体")
    miss = [h["names"]["en"] for h in horses if h["status"] == "maintained" and not h["pedigree"]]
    if miss:
        print("日本马缺 JBIS：", ", ".join(miss))
    print(f"→ {OUT_DEMO}")


if __name__ == "__main__":
    main()
