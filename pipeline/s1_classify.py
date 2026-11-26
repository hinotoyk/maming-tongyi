# -*- coding: utf-8 -*-
"""S1 · 筛出「出賽馬匹名單」并按赛事所属国家/地区归类。

输入：cache/all_titles.json（由 s0_enumerate_all.py 生成）
输出：data-src/hkjc_entries_index.csv（入库，供后续解析与站点使用）

口径说明：按**标题**含「出賽馬匹名單」严格过滤。2026-11-24 实测全站 19,368 篇中
命中 958 篇（2017-07-07 起），与关键词搜索结果一致——说明标题口径没有漏检。

地区是「赛事所属赛马日」的判断，不是参赛马产地：一篇日本赛马日公告里
也会出现 (USA)、(IRE) 的马匹。判断依据是标题里的赛事/赛马日关键词，
所以规则表需要随新赛事名持续补充；未命中的落到「其他/待核对」人工补。
"""
from __future__ import annotations

import collections
import csv
import html as H
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "cache" / "all_titles.json"
DST = ROOT / "data-src" / "hkjc_entries_index.csv"

KEYWORD = "出賽馬匹名單"

# 顺序敏感：先判具体地区，再判宽泛词（如「日本盃」要在「香港」之前）
RULES: list[tuple[str, list[str]]] = [
    ("韩国", ["韓國"]),
    ("新加坡", ["新加坡", "克蘭芝"]),
    ("南非", ["南非", "德班", "花園省"]),
    ("中东（杜拜/沙特）", ["杜拜", "沙特", "麥通", "金挑戰"]),
    ("南美", ["聖保羅", "拉丁美洲", "卡洛貝格"]),
    (
        "日本",
        [
            "寶塚", "有馬", "日本盃", "菊花", "秋華", "安田", "高松宮", "短途馬錦標",
            "大阪盃", "NHK一哩", "日本二千堅尼", "日本打吡", "日本橡樹", "天皇賞",
            "一哩冠軍", "櫻花賞", "希望錦標", "東寶錦標", "女皇伊利沙伯錦標",
            "女皇伊利沙伯二世盃", "冠軍盃", "人馬錦標", "短途盃錦標", "維多利亞一哩",
            "雌馬一哩",
        ],
    ),
    (
        "澳洲/新西兰",
        [
            "墨爾本盃", "覺士盾", "考菲爾德", "維多利亞打吡", "維多利亞橡樹", "唐加士打",
            "金拖鞋", "珠穆朗瑪峰", "澳洲盃", "澳洲冠軍", "澳洲堅尼", "藍鑽石", "東奔",
            "昆士蘭", "南澳洲", "西澳洲", "紐西蘭", "奧塔奇毛利", "火車路", "肯德百里",
            "新市場讓賽", "閃電錦標", "國王碟", "雲絲仙子", "明星賽馬日", "尚羅萬尼",
            "樂景傑", "基維爾", "史德布克", "土方", "金玫瑰", "太陽戰車", "雍容爾雅",
            "栗馬錦標", "太平洋經典", "英皇查理斯三世", "指標評分", "大都會錦標",
            "鄧格錦標", "羅柏奇勒", "未來錦標", "澳港盃", "諾頓錦標", "全齡錦標",
        ],
    ),
    ("香港", ["香港", "沙田", "跑馬地", "主席錦標", "馬會短途錦標", "中銀香港"]),
    ("中国内地", ["從化"]),
    ("美国/加拿大", ["育馬者", "加州冠冕", "肯塔基", "Del Mar"]),
    (
        "英国/爱尔兰",
        [
            "雅士谷", "葉森", "英皇", "皇家地", "女皇安妮", "日蝕", "鑽禧", "英聯邦盃",
            "約克", "國際錦標", "薩塞克斯", "古活", "蘭秀", "楠索普", "戴花", "七月盃",
            "英國", "愛爾蘭", "達德素", "柯爾錦標", "識價盃", "文星錦標", "威爾斯親王",
            "皇席錦標", "杜何斯特", "禧年", "白金禧", "莫格雅", "高利五股",
            "國家錦標", "聖烈治",
        ],
    ),
    (
        "法国",
        [
            "法國", "凱旋門", "隆尚", "聖格盧", "歌劇錦標", "紅寶錦標", "紀爾斯",
            "傑克莫華", "羅斯齊爾德", "伊斯巴翰", "根利錦標", "莊柏德", "卓亨佐夫",
        ],
    ),
    ("德国/欧洲其他", ["德國", "拜仁", "巴伐利亞", "巴登", "柏林", "卡洛貝格", "歐洲"]),
]

FALLBACK = "其他/待核对"


def clean(t: str) -> str:
    return H.unescape(re.sub(r"<[^>]+>", "", t)).strip()


def region(title: str) -> str:
    for name, kws in RULES:
        if any(k in title for k in kws):
            return name
    return FALLBACK


def main() -> int:
    if not SRC.exists():
        print(f"缺少输入文件：{SRC}\n请先运行 python pipeline/s0_enumerate_all.py")
        return 1

    rows = json.loads(SRC.read_text(encoding="utf-8"))
    seen: set[str] = set()
    picked: list[dict] = []
    for r in rows:
        title = clean(r["title"]["rendered"])
        if KEYWORD not in title:
            continue
        link = r["link"].replace("http://", "https://")
        if link in seen:          # 历史数据里出现过同一 URL 重复
            continue
        seen.add(link)
        picked.append(
            {
                "date": r["date"][:10],
                "region": region(title),
                "title": title,
                "url": link,
            }
        )

    picked.sort(key=lambda x: x["date"], reverse=True)
    DST.parent.mkdir(parents=True, exist_ok=True)
    with DST.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "region", "title", "url"])
        w.writeheader()
        w.writerows(picked)

    cnt = collections.Counter(p["region"] for p in picked)
    print(f"total: {len(picked)}")
    for name, n in cnt.most_common():
        print(f"  {name:18} {n:5}")

    residual = [p for p in picked if p["region"] == FALLBACK]
    if residual:
        print(f"\n== 待核对 {len(residual)} 条 ==")
        for p in residual[:20]:
            print(f"  {p['date']}  {p['title'][:70]}")

    print(f"\nwritten: {DST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
