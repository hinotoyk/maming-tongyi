#!/usr/bin/env python3
"""一次性验证：JBIS 血統页 /horse/{id}/pedigree/ 能否抓、结构是否如 yunji-web 所述。

跑法：python pipeline/_explore/probe_jbis_pedigree.py 0001357816
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "pipeline"))

from netguard import Guard  # noqa: E402

hid = sys.argv[1] if len(sys.argv) > 1 else "0001357816"
url = f"https://www.jbis.or.jp/horse/{hid}/pedigree/"
g = Guard()
r = g.get(url, accept_language="ja")
print("outcome", r.outcome, "status", r.status, "len", len(r.text or ""))
out = ROOT / "cache" / "_probe" / f"ped_{hid}.html"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(r.text or "", encoding="utf-8")
print("→", out)

from bs4 import BeautifulSoup  # noqa: E402

soup = BeautifulSoup(r.text or "", "lxml")
print("title:", soup.title.get_text(strip=True) if soup.title else None)
blocks = soup.select(".data-3__items")
print("blocks", len(blocks))
for i, b in enumerate(blocks[:2]):
    items = [x for x in b.find_all(recursive=False) if x.select_one(".txt-link")]
    print("block", i, "items", len(items))
    print("  first:", items[0].get_text(" ", strip=True) if items else None)
    print("  cls:", items[0].get("class") if items else None)
    print("  href:", items[0].select_one("a").get("href") if items else None)
# 马自身信息
h1 = soup.select_one("h1")
print("h1:", h1.get_text(" ", strip=True) if h1 else None)
for dl in soup.select("dl.data-4-2 .data-4__item, dl.data-4-2 .data-4__item-2"):
    dt, dd = dl.find("dt"), dl.find("dd")
    if dt and dd:
        print(" meta:", dt.get_text(strip=True), "=", dd.get_text(" ", strip=True)[:80])
