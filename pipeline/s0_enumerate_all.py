# -*- coding: utf-8 -*-
"""S0-1 · 全量枚举香港赛马会赛马新闻（中文站）文章标题。

为什么不按关键词搜：WP 的 `search` 参数匹配正文，命中数会虚高，且无法保证覆盖
各种异写（「出賽名單」「排位」等）。全量枚举 194 页约 6.5 分钟，口径确定。

用法：python pipeline/s0_enumerate_all.py
产物：cache/all_titles.json（gitignore，可随时重跑）

限速：HKJC 全域 ≥2s/请求（AGENTS.md §2.5）。站点在连续快请求时会返回 400，
不要为了快把 SLEEP 调小。
"""
from __future__ import annotations

import json
import pathlib
import random
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
CACHE = ROOT / "cache"
OUT = CACHE / "all_titles.json"

BASE = "https://racingnews.hkjc.com/chinese/wp-json/wp/v2/posts"
FIELDS = "_fields=id,date,link,title"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
SLEEP = 2.0    # AGENTS.md §2.5：HKJC 全域 ≥2s/请求，不要调小
RETRY = 6


def get(url: str, retry: int = RETRY):
    last = None
    for i in range(retry):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.load(r), r.headers.get("X-WP-TotalPages")
        except Exception as e:  # noqa: BLE001 - 400/429/超时统一退避
            last = e
            time.sleep(1.5 * (i + 1) + random.random())
    raise last  # type: ignore[misc]


def main() -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    first, pages = get(f"{BASE}?per_page=100&page=1&orderby=id&order=asc&{FIELDS}")
    pages = int(pages or 1)
    print(f"total pages: {pages}", flush=True)

    rows = list(first)
    failed: list[int] = []
    for p in range(2, pages + 1):
        try:
            data, _ = get(f"{BASE}?per_page=100&page={p}&orderby=id&order=asc&{FIELDS}")
            rows.extend(data)
        except Exception as e:  # noqa: BLE001
            failed.append(p)
            print(f"page {p} failed: {e}", flush=True)
        if p % 25 == 0:
            print(f"page {p} rows {len(rows)}", flush=True)
        time.sleep(SLEEP)

    OUT.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    span = (
        (rows[0]["date"][:10], max(r["date"] for r in rows)[:10]) if rows else ("-", "-")
    )
    print(f"done rows: {len(rows)}  span: {span[0]} ~ {span[1]}")
    if failed:
        print(f"WARNING failed pages: {failed}")


if __name__ == "__main__":
    main()
