"""补齐 fetch_jbis_horses.py 漏掉的马（JBIS 偶发超时 / 检索抖动）。

只处理 jbis_horses.json 里 origin=JPN 但没有 jp 的条目，每个最多试 2 次，
成功后合并回同一份 JSON。不动已经拿到的结果。

用法：python pipeline/_explore/fetch_jbis_retry.py
"""
from __future__ import annotations

import importlib.util
import json
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("fetch_jbis", ROOT / "pipeline/_explore/fetch_jbis_horses.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

JBIS_JSON = ROOT / "cache/_probe/jbis_horses.json"
ENTRIES = ROOT / "cache/_probe/race_entries.json"

SLEEP = 8.0
ANNOUNCE_YEAR = 2026


def main() -> None:
    data = json.loads(JBIS_JSON.read_text(encoding="utf-8"))
    ages = {}
    for r in json.loads(ENTRIES.read_text(encoding="utf-8")):
        m = re.search(r"\(([A-Z]{2,3})\)\s*$", r["en"])
        name = r["en"][: m.start()].strip() if m else r["en"].strip()
        ages[name] = r["age"]

    todo = [k for k, v in data.items() if v.get("origin") == "JPN" and not v.get("jp")]
    print(f"待补 {len(todo)} 匹：{', '.join(todo)}")

    for i, name in enumerate(todo, 1):
        want_year = ANNOUNCE_YEAR - ages[name]
        pick = None
        for attempt in (1, 2):
            url = f"https://www.jbis.or.jp/horse/result/?keyword={name.replace(' ', '+')}"
            html = mod.get(url, timeout=60)
            if html is None:
                print(f"  [{i}] {name:20s} 第 {attempt} 次取页失败")
                time.sleep(SLEEP)
                continue
            cands = mod.parse(html)
            jp = [c for c in cands if re.fullmatch(r"[\u30a0-\u30ff\u30fc\u4e00-\u9fffー・]+", c["jp"] or "")]
            pick = next((c for c in jp if c["birth_year"] == want_year), None)
            if pick is None and jp:
                pick = jp[0]
                print(f"  [{i}] {name:20s} 仅弱匹配（生年 {pick['birth_year']} ≠ {want_year}）")
            if pick:
                break
            time.sleep(SLEEP)

        if pick:
            data[name] = {
                "origin": "JPN",
                "jbis_id": pick["jbis_id"],
                "jp": pick["jp"],
                "birth_year": pick["birth_year"],
                "birth_year_expect": want_year,
                "match": "exact" if pick["birth_year"] == want_year else "weak",
                "sex": pick.get("sex"),
                "trainer_jp": pick.get("trainer_jp"),
                "sire": pick.get("sire"),
                "dam": pick.get("dam"),
            }
            print(f"  [{i}] {name:20s} → {pick['jp']}  父={pick['sire']} 母={pick['dam']}")
        else:
            print(f"  [{i}] {name:20s} 仍无命中")
        time.sleep(SLEEP)

    JBIS_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    ok = sum(1 for v in data.values() if v.get("jp"))
    print(f"\n现在 {ok}/{len(data)} 有日文名")


if __name__ == "__main__":
    main()
