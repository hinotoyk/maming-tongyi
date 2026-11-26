#!/usr/bin/env python3
"""S3 · 解析公告 → cache/races.csv + cache/entries.csv

输入  cache/docs/{race_uid}/（S2 产物：article.html + attach.docx|attach.doc + meta.json）
输出  cache/races.csv（一公告一行）、cache/entries.csv（一行一匹马一场比赛）

三种正文形态（各约 1/3，只处理一种会漏 2/3）：
  · attach.docx            —— ZIP，直接读第一张表的列头
  · attach.doc             —— OLE2，先 soffice --convert-to docx 再解析
  · article.html 内联表格  —— 页面 <table> 里就是名单
三者最后都归到同一个结构：`(表前文字, 表)`，列按表头语义定位（不按固定下标）。

三个归一（IMPL.md §7）：
  race_uid   {region_prefix}-{year}-{赛事名 slug}
  horse_uid  {产地小写}-{slug(en)}，产地取 `Name (JPN)` 后缀，缺后缀不猜
  person_uid j-{slug} / t-{slug}，带角色前缀防同名撞键

一张公告里可能有多场比赛（「賽馬日」型，实测最多 8 张表）。本项目一行公告 = 一场赛事，
所以只取其中**正赛**那张表（优先带级别的，其次与公告标题最贴合的），其余的场次只记数目，
不硬塞进同一场（否则马号会重复、磅重区间校验也会失真）。

用法
  python pipeline/s3_parse.py
  python pipeline/s3_parse.py --uid jra-2026-一哩冠軍賽 --verbose
  python pipeline/s3_parse.py --soffice "C:/Program Files/LibreOffice/program/soffice.exe"
"""
from __future__ import annotations

import argparse
import base64
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common as C

RACE_FIELDS = [
    "race_uid", "announce_date", "region", "title", "article_url", "source_kind",
    "table_count", "table_index", "sheet_name", "race_date", "race_time", "session",
    "grade", "grade_cn", "venue", "venue_country", "course", "distance", "surface",
    "entries_count", "parsed_at",
]
ENTRY_FIELDS = [
    "race_uid", "saddle_no", "draw", "gear", "horse_uid", "horse_en", "horse_zh", "origin",
    "sex", "sex_cn", "age", "lb", "rating",
    "jockey_uid", "jockey_en", "jockey_zh", "claim_lb", "trainer_uid", "trainer_en", "trainer_zh",
    "scraped_from", "doc_url",
]

# 中文名后缀 → 产地（附件里 `宇宙碧寶 (日)` / `藍勁先 (美)` 这种写法）
ZH_ORIGIN = {
    "日": "JPN", "美": "USA", "法": "FR", "英": "GB", "愛": "IRE", "澳": "AUS", "紐": "NZ",
    "德": "GER", "意": "ITY", "加": "CAN", "南": "SAF", "韓": "KOR", "新": "SIN", "西": "SPA",
    "巴": "BRZ", "智": "CHI", "秘": "PER", "烏": "URU", "阿": "UAE", "土": "TUR", "俄": "RUS",
    "波": "POL", "瑞": "SWE", "丹": "DEN", "挪": "NOR", "荷": "HOL", "比": "BEL", "奧": "AUT",
    "捷": "CZE", "匈": "HUN", "印": "IND", "馬": "MAL",
}
CN_SUFFIX_RE = re.compile(r"[（(]\s*([^\s()（）]{1,2})\s*[)）]\s*$")

SOFFICE_CANDIDATES = [
    "soffice", "libreoffice",
    "/usr/bin/soffice", "/usr/local/bin/soffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "C:/Program Files/LibreOffice/program/soffice.exe",
    "C:/Program Files (x86)/LibreOffice/program/soffice.exe",
]


# ── 小工具 ──────────────────────────────────────────────────────────────
def to_int(v) -> int | None:
    m = re.search(r"-?\d+", str(v or ""))
    return int(m.group()) if m else None


def to_float(v) -> float | None:
    m = re.search(r"\d+(?:\.\d+)?", str(v or ""))
    return float(m.group()) if m else None


def strip_cn_suffix(raw: str) -> tuple[str, str | None]:
    """'宇宙碧寶 (日)' → ('宇宙碧寶', 'JPN')。只认 1–2 个字的产地缩写。"""
    s = (raw or "").strip()
    m = CN_SUFFIX_RE.search(s)
    if not m:
        return s, None
    code = ZH_ORIGIN.get(m.group(1))
    if not code:
        return s, None
    return s[: m.start()].strip(), code


def pick_names(a: str, b: str) -> tuple[str | None, str | None, str | None]:
    """两列里分出（拉丁名, 汉字名, 汉字后缀给出的产地）。列序不可靠，按字符集判断。"""
    a_clean, a_cn = strip_cn_suffix(a)
    b_clean, b_cn = strip_cn_suffix(b)
    a_latin = bool(a_clean) and not C.has_cjk(a_clean)
    b_latin = bool(b_clean) and not C.has_cjk(b_clean)
    if C.has_cjk(a_clean) and b_latin:
        return b_clean, a_clean, a_cn
    if a_latin and C.has_cjk(b_clean):
        return a_clean, b_clean, b_cn
    if a_latin and not b_clean:
        return a_clean, None, None
    if C.has_cjk(a_clean):
        return None, a_clean, a_cn
    return (a_clean or None), (b_clean or None), (a_cn or b_cn)


def find_soffice(explicit: str | None) -> str | None:
    if explicit:
        return explicit if Path(explicit).exists() else None
    for cand in SOFFICE_CANDIDATES:
        if Path(cand).exists():
            return cand
        found = shutil.which(cand)
        if found:
            return found
    return None


def convert_doc(doc: Path, soffice: str | None) -> Path | None:
    """OLE2 的 .doc → docx（python 读不了老二进制格式）。

    两条路：先 soffice --headless，没装 LibreOffice 就用 Windows 自带的 Word COM。
    两条都失败就返回 None（记 need-soffice，不猜内容）。
    """
    out = doc.with_suffix(".converted")
    out.mkdir(parents=True, exist_ok=True)
    cand = out / (doc.stem + ".docx")
    if cand.exists():
        return cand

    if soffice:
        try:
            p = subprocess.run(
                [soffice, "--headless", "--convert-to", "docx", "--outdir", str(out), str(doc)],
                capture_output=True, timeout=180,
            )
            if p.returncode == 0 and cand.exists():
                return cand
        except (OSError, subprocess.TimeoutExpired):
            pass

    return convert_doc_word(doc, cand)


def convert_doc_word(doc: Path, cand: Path) -> Path | None:
    """Word COM 兜底（Windows）。脚本走 -EncodedCommand，避免 .ps1 编码毁掉中文路径。"""
    if sys.platform != "win32":
        return None
    script = (
        "$ErrorActionPreference='Stop';"
        "$w=New-Object -ComObject Word.Application;"
        "$w.Visible=$false;$w.DisplayAlerts=0;"
        "try{"
        f"$d=$w.Documents.Open('{str(doc).replace(chr(39), chr(39) * 2)}',$false,$true);"
        f"$d.SaveAs2('{str(cand).replace(chr(39), chr(39) * 2)}',16);"
        "$d.Close(0)"
        "}finally{$w.Quit()}"
    )
    b64 = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", b64],
            capture_output=True, timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return cand if p.returncode == 0 and cand.exists() else None


# ── 解析一张表 → 缓存行 ──────────────────────────────────────────────────
def rows_to_entries(recs: list[dict], race_uid: str, scraped_from: str, doc_url: str) -> list[dict]:
    entries: list[dict] = []
    seen: set[str] = set()
    for rec in recs:
        en, zh, origin_cn = pick_names(rec.get("horse_en", ""), rec.get("horse_zh", ""))
        if not en and not zh:
            continue
        origin = None
        if en:
            en, origin = C.split_en(en)
        origin = origin or origin_cn
        prefix, _, _ = C.origin_meta(origin)
        uid = f"{prefix}-{C.slug(en)}" if en and C.slug(en) else f"{prefix}-{C.short_hash(en or zh or '', 8)}"
        if uid in seen:
            continue
        seen.add(uid)

        j_en, j_zh, _ = pick_names(rec.get("jockey_en", ""), rec.get("jockey_zh", ""))
        # 骑师名末尾的「(-3)」「(a)」是标记不是名字；剥完若只剩占位符「-」则视作未知。
        # 同一骑师不同场次减磅不同，剥干净后才会并成一个实体。
        claim = None
        if j_en:
            j_en, claim = C.strip_claim(j_en)
        if j_zh:
            j_zh, zh_claim = C.strip_claim(j_zh)
            claim = claim if claim is not None else zh_claim
        t_en, t_zh, _ = pick_names(rec.get("trainer_en", ""), rec.get("trainer_zh", ""))
        if t_en:
            t_en, _ = C.strip_claim(t_en)
        if t_zh:
            t_zh, _ = C.strip_claim(t_zh)
        sex = C.norm_sex(rec.get("sex"))
        entries.append({
            "race_uid": race_uid,
            "saddle_no": to_int(rec.get("saddle_no")),
            "draw": to_int(rec.get("draw")),
            "gear": (rec.get("gear") or "").strip() or None,
            "horse_uid": uid,
            "horse_en": en,
            "horse_zh": zh,
            "origin": origin or "",
            "sex": sex,
            "sex_cn": C.SEX_CN.get(sex or "", ""),
            "age": to_int(rec.get("age")),
            "lb": num_out(to_float(rec.get("lb"))),
            "rating": to_int(rec.get("rating")),
            "jockey_uid": C.person_uid_of("jockey", j_en) if j_en else "",
            "jockey_en": j_en,
            "jockey_zh": j_zh,
            "claim_lb": num_out(claim),
            "trainer_uid": C.person_uid_of("trainer", t_en) if t_en else "",
            "trainer_en": t_en,
            "trainer_zh": t_zh,
            "scraped_from": scraped_from,
            "doc_url": doc_url,
        })
    return entries


NAV_MARKS = ("所有新聞消息", "選擇其他新聞", "賽事資料更新", "馬場盛事", "本地賽馬新聞")


def clean_info(info: str) -> str:
    """表前文字未必是这张表的表头信息行。

    实测：内联表（HTML）没有信息行时，`html_tables_with_ctx` 给的上下文是整页导航
    （「所有新聞消息 本地賽馬新聞 … -- 選擇其他新聞 --」）。这种文字一旦喂给
    `parse_header` 会解出假的途程（墨爾本盃被解成 1650 米）。宁可留空，不猜。
    """
    t = (info or "").strip()
    if not t or len(t) > 240 or any(k in t for k in NAV_MARKS):
        return ""
    return t


def sheet_name(info: str, title_zh: str | None) -> str | None:
    """名单表自己的赛事名（去掉「XX賽馬日」「越洋轉播賽事」这类前缀）。

    一天多场时，公告标题是「某某賽馬日」，表头里才是这一场叫什么；
    与标题同名就返回 None（单场公告不需要重复）。
    """
    t = re.split(r"\s+[-–—]\s+", info or "")[0]
    t = re.sub(r"[（(][一二三四五]級賽[)）].*$", "", t).strip()
    for _ in range(3):
        if title_zh and t.startswith(title_zh):
            t = t[len(title_zh):]
        t = re.sub(r"^(?:賽馬日|賽日|日|越洋轉播賽事|海外賽事|轉播賽事)", "", t).strip(" -–—")
    if not t or t == title_zh:
        return None
    return t


def table_score(info: str, recs: list[dict], want: str, index: int) -> tuple:
    """多表公告里挑「正赛」：先看有没有级别，再看与公告标题的贴合度，最后取靠前的表。"""
    grade = bool(C.parse_header(info).get("grade"))
    overlap = len(set(want) & set(re.sub(r"\s+", "", info))) if want else 0
    return (grade, overlap, -index, len(recs))


def parse_doc(uid: str, meta: dict, soffice: str | None, verbose: bool = False,
              used: set[str] | None = None) -> tuple[list[dict], list[dict], list[str]]:
    """一份公告 → (race 行列表, entry 行, 问题列表)。

    一份公告里可能有好几张名单表：单场公告是 1 张，「XX賽馬日」这类越洋转播公告
    是整个赛马日（实测 4–10 张，各表途程/班次不同，是不同场次）。所以「一张表 = 一场」：
    正赛那张（与公告标题同名）沿用公告 uid，其余各表用表内赛事名另起 uid。
    """
    d = C.DOCS / uid
    problems: list[str] = []
    kind = meta.get("source_kind") or ""
    article = d / "article.html"
    html = article.read_text(encoding="utf-8", errors="ignore") if article.exists() else ""

    if kind in ("docx", "doc"):
        src = d / f"attach.{'docx' if kind == 'docx' else 'doc'}"
        if not src.exists():
            problems.append("attach-missing")
            return [], [], problems
        if kind == "doc":
            conv = convert_doc(src, soffice)
            if not conv:
                problems.append("doc-convert-failed")
                return [], [], problems
            src = conv
        blocks = C.docx_tables_with_ctx(src)
        scraped = "hkjc_docx" if kind == "docx" else "hkjc_doc"
    else:
        blocks = C.html_tables_with_ctx(html)
        scraped = "hkjc_table"

    parsed = []
    for i, (ctx, table) in enumerate(blocks):
        info, recs = C.parse_entry_rows(table, ctx)
        if recs:
            parsed.append({"i": i, "info": clean_info(info), "recs": recs})
    if not parsed:
        problems.append(f"no-entry-table(tables={len(blocks)})")
        return [], [], problems

    title_zh = C.race_title_zh(meta.get("title", "")) or ""
    want = title_zh
    best = max(parsed, key=lambda p: table_score(p["info"], p["recs"], want, p["i"]))

    # 全文兜底：表头信息行缺字段时（个别公告的信息行被拆成两格），从整篇文字里再抓一遍
    # 全文兜底只对附件（docx/doc）做：那是整份文件，正文可信。
    # 内联型若拿整页文字兜底，会和上面一样把导航/页脚当数据。
    if kind in ("docx", "doc"):
        fallback = C.parse_header(C.docx_text(Path(src)))
    else:
        fallback = {}

    region = meta.get("region", "")
    announce_date = meta.get("announce_date", "")
    races: list[dict] = []
    entries: list[dict] = []
    if used is None:
        used = set()

    # 同一份公告 = 同一个赛马日：日期/马场/场次在各表之间可以互补
    # （「暫定宣佈」类公告的表常只有赛名，没有信息行）。途程绝不互补——每场不同。
    per: list[dict] = []
    for p in parsed:
        facts = C.parse_header(p["info"])
        if not facts.get("race_date") or not facts.get("distance"):
            for k, v in fallback.items():
                facts.setdefault(k, v)
        per.append({"p": p, "sub": sheet_name(p["info"], title_zh), "facts": facts})

    doc_ctx = {}
    for k in ("race_date", "venue", "venue_country", "session"):
        for item in per:
            v = item["facts"].get(k)
            if v:
                doc_ctx[k] = v
                break
    for item in per:
        for k, v in doc_ctx.items():
            if not item["facts"].get(k):
                item["facts"][k] = v

    for item in per:
        p, sub, facts = item["p"], item["sub"], item["facts"]

        suffixed = False
        if len(parsed) == 1 or (p is best and not sub):
            rid = uid                       # 正赛：沿用公告 uid，对外 URL 不变
        else:
            rid = C.race_uid_sub(facts.get("race_date") or announce_date, region, sub or title_zh)
            # 同一场赛事常被两份公告各覆盖一次（「XX賽馬日」含正赛表 + 单独的「XX」）。
            # 两份产出同名 uid，这里先加后缀避免主键撞车，去重阶段再并成一场。
            # 后缀优先用开跑时间（同日同名场次靠时间区分，比哈希好读）。
            if rid in used or rid == uid:
                tm = re.sub(r"\D", "", str(facts.get("race_time") or ""))[:4]
                cand = f"{rid}-{tm}" if tm else ""
                rid = cand if cand and cand not in used else f"{rid}-{C.short_hash(p['info'] + str(p['i']), 4)}"
                suffixed = True
        used.add(rid)

        race = {
            "race_uid": rid,
            "uid_suffixed": suffixed,
            "announce_date": announce_date,
            "region": region,
            "title": sub or title_zh or meta.get("title", ""),
            "article_url": meta.get("article_url", ""),
            "source_kind": kind or "inline",
            "table_count": len(parsed),
            "table_index": p["i"],
            "sheet_name": sub,
            "race_date": facts.get("race_date"),
            "race_time": facts.get("race_time"),
            "session": facts.get("session"),
            "grade": facts.get("grade"),
            "grade_cn": facts.get("grade_cn"),
            "venue": facts.get("venue"),
            "venue_country": facts.get("venue_country") or C.REGION_COUNTRY.get(region),
            "course": facts.get("course"),
            "distance": facts.get("distance"),
            "surface": facts.get("surface"),
            "entries_count": 0,
            "parsed_at": C.now_iso(),
        }
        ents = rows_to_entries(p["recs"], rid, scraped, meta.get("article_url", ""))
        race["entries_count"] = len(ents)
        races.append(race)
        entries.extend(ents)

        if not ents:
            problems.append(f"zero-entries({rid})")
        if not race["race_date"]:
            problems.append(f"no-race-date({rid})")
        if not race["distance"]:
            problems.append(f"no-distance({rid})")

    if len(parsed) > 1:
        problems.append(f"multi-table({len(parsed)} 场，已全部展开)")
    if verbose:
        print(f"  {uid}: {len(parsed)} 表 → {len(races)} 场 / {len(entries)} 匹")
    return races, entries, problems


def num_out(v):
    """CSV 里 126.0 读着像小数，整值就写成 126；空就空着。"""
    if v is None or v == "":
        return ""
    f = float(v)
    return int(f) if f.is_integer() else f


def dedupe_same_race(races: list[dict], entries: list[dict], report: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """同一场赛事被两份公告覆盖时只留一份。

    实测：HKJC 先发「XX賽馬日 – 名單」（一天多場，含正赛那张表），再发「XX – 名單」（单场）。
    两份公告的赛期 / 场次 / 途程 / 马场完全相同，就是同一场。留单场那份（表格更纯），
    另一份记进 parse_report，不删证据。
    """
    groups: dict[tuple, list[dict]] = {}
    for r in races:
        if r.get("race_date") and r.get("session") and r.get("distance"):
            groups.setdefault((r["race_date"], r["session"], r["distance"], r.get("venue")), []).append(r)

    drop: set[str] = set()
    for group in groups.values():
        if len(group) < 2:
            continue
        best = max(group, key=lambda r: (not r.get("uid_suffixed"),
                                         r["table_count"] == 1,
                                         r["entries_count"] or 0,
                                         r["announce_date"]))
        for r in group:
            if r is best:
                continue
            drop.add(r["race_uid"])
            rep = report.get(r["race_uid"])
            if rep:
                rep["problems"] = ";".join(filter(None, [rep["problems"], f"duplicate-of:{best['race_uid']}"]))
                rep["entries"] = 0
    if not drop:
        return races, entries
    kept = [r for r in races if r["race_uid"] not in drop]
    kept_entries = [e for e in entries if e["race_uid"] not in drop]
    print(f"同一赛事双公告：合并 {len(drop)} 场（保留单场公告）")
    return kept, kept_entries


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="S3 · 解析公告出赛事与名单")
    ap.add_argument("--uid", default=None, help="只解析这一场")
    ap.add_argument("--limit", type=int, default=None, help="只解析前 N 场（调试用）")
    ap.add_argument("--soffice", default=None, help="LibreOffice 可执行文件路径（解析 .doc 需要）")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args(argv)

    manifest = C.read_csv(C.DOCS / "manifest.csv")
    if not manifest:
        print(f"缺少 {C.DOCS / 'manifest.csv'}（先跑 python pipeline/s2_fetch_docs.py）")
        return 1
    if a.uid:
        manifest = [m for m in manifest if m["race_uid"] == a.uid]
    manifest = [m for m in manifest if m.get("status") == "ok"]
    if a.limit:
        manifest = manifest[: a.limit]

    soffice = find_soffice(a.soffice)
    has_doc = any(m.get("source_kind") == "doc" for m in manifest)
    if has_doc and not soffice and sys.platform != "win32":
        print("⚠ 未找到 LibreOffice：.doc 附件无法转换，会被记为 need-soffice（不猜内容）")

    races: list[dict] = []
    entries: list[dict] = []
    report: list[dict] = []          # 一篇公告一行（审计用）
    race_report: dict[str, dict] = {}  # 一场赛事一条（去重时记问题用）
    used_uids: set[str] = set()
    for n, meta in enumerate(manifest, 1):
        race_rows, ents, problems = parse_doc(meta["race_uid"], meta, soffice, a.verbose, used_uids)
        races.extend(race_rows)
        entries.extend(ents)
        for r in race_rows:
            race_report[r["race_uid"]] = {"problems": ";".join(problems), "entries": r["entries_count"]}
        report.append({"race_uid": meta["race_uid"], "races": len(race_rows),
                       "entries": len(ents), "problems": ";".join(problems)})
        if n % 100 == 0:
            print(f"  … 已解析 {n}/{len(manifest)} 篇（{len(races)} 场）", flush=True)

    races, entries = dedupe_same_race(races, entries, race_report)

    C.write_csv(C.RACES_CSV, races, RACE_FIELDS)
    C.write_csv(C.ENTRIES_CSV, entries, ENTRY_FIELDS)
    C.write_csv(C.CACHE / "parse_report.csv", report, ["race_uid", "races", "entries", "problems"])

    no_date = sum(1 for r in races if not r.get("race_date"))
    no_dist = sum(1 for r in races if not r.get("distance"))
    thin = sum(1 for r in races if (r.get("entries_count") or 0) < 8)

    def lb_out(e):
        try:
            return not 100 <= float(e["lb"]) <= 140
        except (TypeError, ValueError):
            return False

    lb_bad = sum(1 for e in entries if lb_out(e))
    sex_bad = sum(1 for e in entries if e.get("sex") not in (None, "", "M", "F", "G"))
    print(f"\n赛事 {len(races)} 场 / 名单 {len(entries)} 行")
    print(f"缺赛期 {no_date} · 缺途程 {no_dist} · 名单 <8 匹 {thin} · 磅重越界 {lb_bad} · 性别异常 {sex_bad}")
    print(f"→ {C.RACES_CSV}\n→ {C.ENTRIES_CSV}")
    if thin or lb_bad or sex_bad:
        print("（有问题先看 cache/parse_report.csv，再抽原文核对，不要靠改阈值过检）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
