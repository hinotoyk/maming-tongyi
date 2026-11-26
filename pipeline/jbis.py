#!/usr/bin/env python3
"""JBIS 客户端（共用层）：检索页 / 基本情報页 / 5 代血統页，只走 `netguard.Guard`。

三个页面各管一件事，别混用：

| 页面 | URL | 一次给到什么 |
| --- | --- | --- |
| 検索結果 | `/horse/result/?keyword={name}` | 馬名 / 10 位 id / 生年 / 父 / 母 / 調教師（一次可命中多匹，用生年消歧） |
| 基本情報 | `/horse/{id}/` | 性別 / 生年月日 / 毛色 / 産地 / 馬主 / 生産牧場 / 調教師 / 戦績 |
| 5 代血統 | `/horse/{id}/pedigree/` | 父系 31 格 + 母系 31 格（名字 / 性別 / 生年 / 毛色 / id）+ ファミリーNo + クロス |

限速、风控、熔断都在 `netguard` 里，这里只做「请求 + 解析」，不做重试与 sleep。
以前 IMPL 记的「详情页 503 是结构性的」已过期：2026-11-25 复测 `/horse/{id}/` 与
`/horse/{id}/pedigree/` 都是 200，但仍按 10s 地板逐匹串行，不并行。

解析口径：
  · 名字原样保留（日文假名 / 汉字 / 外来马保留 `(USA)` 这类产地后缀），不转写、不猜。
  · 生年取血统格里的 4 位年；生年月日只有基本情報页有，取不到就留空。
  · 血统格按「父系 31 格、母系 31 格」切开，行深 1 / 2 / 4 / 8 / 16，空位用 None 补足。
"""
from __future__ import annotations

import re
from urllib.parse import quote

from bs4 import BeautifulSoup

SEARCH_URL = "https://www.jbis.or.jp/horse/result/?keyword={kw}"
PROFILE_URL = "https://www.jbis.or.jp/horse/{hid}/"
PEDIGREE_URL = "https://www.jbis.or.jp/horse/{hid}/pedigree/"

LEVELS = (1, 2, 4, 8, 16)
COLOR_RE = re.compile(r"[\u4e00-\u9fff]{1,3}毛")
YEAR_RE = re.compile(r"(1[89]\d{2}|20\d{2})")
BIRTH_RE = re.compile(r"(\d{4})\.(\d{1,2})\.(\d{1,2})")
HID_RE = re.compile(r"/horse/(\d{10})/")


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html or "", "lxml")


def _text(node) -> str:
    return node.get_text(" ", strip=True) if node else ""


# ── 検索結果 ────────────────────────────────────────────────────────────
def parse_search(html: str) -> list[dict]:
    """检索结果：`div.data-6-1` 里第一行是表头，之后每行一匹马。

    一行 = 馬名 / 生年 / 性別 / 調教師(所属) / 父馬 / 母馬 / 総賞金，
    父子两栏都是 `a.txt-link.txt-overflow`，按出现顺序取。
    """
    soup = _soup(html)
    out: list[dict] = []
    for box in soup.select("div.data-6-1"):
        rows = [d for d in box.find_all("div", recursive=False)][1:]
        for row in rows:
            a = row.select_one('a.txt-link[href^="/horse/0"]')
            if a is None or not a.get_text(strip=True):
                continue
            hid = HID_RE.search(a.get("href", ""))
            if hid is None:
                continue
            year = None
            for cell in row.select("div.jc-center"):
                m = YEAR_RE.fullmatch(cell.get_text(strip=True) or "")
                if m:
                    year = int(m.group(1))
                    break
            parents = [x.get_text(" ", strip=True)
                       for x in row.select('a.txt-link.txt-overflow[href^="/horse/0"]')]
            trainer = row.select_one('a[href^="/horse/trainer/"]')
            out.append({
                "jbis_id": hid.group(1),
                "jp": a.get_text(" ", strip=True),
                "birth_year": year,
                "trainer_jp": trainer.get_text(" ", strip=True) if trainer else None,
                "sire": parents[0] if len(parents) > 0 else None,
                "dam": parents[1] if len(parents) > 1 else None,
            })
    return out


def is_kana(name: str) -> bool:
    return bool(re.fullmatch(r"[\u30a0-\u30ff\u3040-\u309f\u4e00-\u9fffー・（）()]+", name or "")) and "(" not in (name or "")


def search(g, en: str, want_year: int | None = None) -> dict:
    """按英文名查检索页，用「生年 = 赛期年 − 年龄」消歧（Sixpence vs Sixpence(IRE)）。"""
    r = g.get(SEARCH_URL.format(kw=quote(en)), accept_language="ja")
    if not r.ok:
        return {"status": f"fetch-{r.outcome}"}
    cands = parse_search(r.text)
    if not cands:
        return {"status": "no-match"}
    pick = next((c for c in cands if is_kana(c["jp"]) and c["birth_year"] == want_year), None)
    if pick is None:
        pick = next((c for c in cands if is_kana(c["jp"])), None)
    if pick is None:
        pick = cands[0]
    match = "ok" if (want_year is None or pick["birth_year"] == want_year) else "weak"
    return {**pick, "status": match}


# ── 基本情報 ────────────────────────────────────────────────────────────
def parse_profile(html: str) -> dict:
    soup = _soup(html)
    facts: dict[str, str] = {}
    for item in soup.select("dl .data-4__item, dl .data-4__item-2, dl .data-4__item-4"):
        dt, dd = item.find("dt"), item.find("dd")
        if dt is None or dd is None:
            continue
        k = dt.get_text(strip=True)
        if k and k not in facts:
            facts[k] = dd.get_text(" ", strip=True)

    def birth_date(v: str) -> str | None:
        m = BIRTH_RE.search(v or "")
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else None

    sub = soup.select_one(".hdg1-search__sub")
    name_en = _text(sub)
    m = re.fullmatch(r"(.+?)\s*\(([A-Z]{2,3})\)", name_en)
    return {
        "name_jp": _text(soup.select_one("h1")),
        "name_en": m.group(1).strip() if m else (name_en or None),
        "name_country": m.group(2) if m else None,
        "birth_date": birth_date(facts.get("生年月日", "")),
        "sex_jp": facts.get("性別", "")[:1] or None,          # 牡 / 牝 / 騸
        "color": facts.get("毛色") or None,
        "birth_place": facts.get("産地") or None,
        "breeder": facts.get("生産牧場") or None,
        "owner": facts.get("馬主") or None,
        "trainer_jp": (facts.get("調教師") or "").split("(")[0].strip() or None,
    }


def profile(g, jbis_id: str) -> dict:
    r = g.get(PROFILE_URL.format(hid=jbis_id), accept_language="ja")
    if not r.ok:
        return {"status": f"fetch-{r.outcome}"}
    out = parse_profile(r.text)
    if not out.get("name_jp"):
        return {"status": "empty"}
    return {**out, "status": "ok"}


# ── 5 代血統 ────────────────────────────────────────────────────────────
def _node(item) -> dict | None:
    a = item.select_one(".txt-link")
    if a is None:
        return None
    meta = item.get_text(" ", strip=True)
    hid = HID_RE.search(a.get("href", ""))
    year = YEAR_RE.fullmatch((meta or "").split(" ")[-1] or "")
    color = COLOR_RE.search(meta)
    if year is None:
        m = YEAR_RE.search(meta)
        year = m
    female = "female" in " ".join(item.get("class", []))
    return {
        "name": a.get_text(" ", strip=True),
        "sex": "牝" if female else "牡",
        "year": int(year.group(1)) if year else None,
        "color": color.group(0) if color else None,
        "id": hid.group(1) if hid else None,
    }


def parse_pedigree(html: str) -> dict:
    soup = _soup(html)
    blocks = soup.select(".data-3__items")
    sides: dict[str, list[list[dict | None]]] = {}
    for name, block in zip(("父", "母"), blocks[:2]):
        nodes: list[dict | None] = []
        for item in block.find_all(recursive=False):
            if item.select_one(".txt-link") is None:
                continue
            nodes.append(_node(item))
        rows, cur = [], 0
        for n in LEVELS:
            rows.append(nodes[cur:cur + n])
            cur += n
        sides[name] = rows
    tree = {"父": sides.get("父", []), "母": sides.get("母", [])}

    def at(side: str, gen: int, i: int) -> dict | None:
        rows = tree.get(side) or []
        if gen < len(rows) and i < len(rows[gen]):
            return rows[gen][i]
        return None

    def nm(side: str, gen: int, i: int) -> str | None:
        n = at(side, gen, i)
        return n["name"] if n else None

    fno = cross = ""
    for item in soup.select("dl.data-4-2 .data-4__item, dl.data-4-2 .data-4__item-2"):
        dt, dd = item.find("dt"), item.find("dd")
        if dt is None or dd is None:
            continue
        k, v = dt.get_text(strip=True), dd.get_text(" ", strip=True)
        if "ファミリー" in k:
            fno = v
        elif "クロス" in k:
            cross = v
    sub = soup.select_one(".hdg1-search__sub")
    return {
        "tree": tree,
        "fno": fno,
        "cross": cross,
        "name_jp": _text(soup.select_one("h1")),
        "name_en": _text(sub),
        "sire": nm("父", 0, 0),
        "dam": nm("母", 0, 0),
        "siresire": nm("父", 1, 0),
        "damsire": nm("母", 1, 0),
    }


def pedigree(g, jbis_id: str) -> dict:
    r = g.get(PEDIGREE_URL.format(hid=jbis_id), accept_language="ja")
    if not r.ok:
        return {"status": f"fetch-{r.outcome}"}
    out = parse_pedigree(r.text)
    if not any(out["tree"].values()):
        return {"status": "empty"}
    return {**out, "status": "ok"}
