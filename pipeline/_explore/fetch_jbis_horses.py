"""从 JBIS 检索页补「日文原名 + 父 + 母 + 生年 + 調教師」。

为什么只打检索页：`/horse/{id}/` 详情页在限速下会 503（实测 84s 后 503），
而**检索结果行里已经带** 馬名（カタカナ）/ 生年 / 性別 / 調教師 / 父 / 母，
一次请求就能拿到演示需要的全部字段，比翻详情页更省也更稳。

比对口径：以「生年 = 2026 − 公告年龄」做消歧，避免同名马（如 Embroidery 与
Embroidery(IRE)）。命中不到就留空，不猜。

用法：python pipeline/_explore/fetch_jbis_horses.py
产物：cache/_probe/jbis_horses.json（gitignore）
"""
from __future__ import annotations

import io
import json
import re
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENTRIES = ROOT / "cache/_probe/race_entries.json"
OUT = ROOT / "cache/_probe/jbis_horses.json"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
SLEEP = 8.0  # JBIS 限速保守值；实测 6s 间隔可用，8s 留余量
ANNOUNCE_YEAR = 2026

ROW_RE = re.compile(
    r'<a href="/horse/(\d{10})/" class="txt-link">([^<]+)</a>(.*?)(?=<a href="/horse/\d{10}/" class="txt-link">|$)',
    re.S,
)
TRAINER_RE = re.compile(r'/horse/trainer/[A-Z0-9]+/" class="txt-link">([^<]+)</a>')
PARENT_RE = re.compile(r'<a href="/horse/\d{10}/" class="txt-link txt-overflow">([^<]+)</a>')
YEAR_RE = re.compile(r'<div class="jc-center">(\d{4})</div>')
SEX_RE = re.compile(r'class="txt-(female|male|horse|gelding|colt|filly)"')


def get(url: str, timeout: int = 90) -> str | None:
    p = subprocess.run(
        ["curl", "-sS", "--max-time", str(timeout), "-A", UA,
         "-H", "Accept-Language: ja", url],
        capture_output=True,
    )
    if p.returncode != 0 or not p.stdout:
        return None
    return p.stdout.decode("utf-8", "ignore")


def parse(html: str) -> list[dict]:
    out = []
    for hid, name, rest in ROW_RE.findall(html):
        year = YEAR_RE.search(rest)
        sex = SEX_RE.search(rest)
        trainer = TRAINER_RE.search(rest)
        parents = PARENT_RE.findall(rest)
        out.append(
            {
                "jbis_id": hid,
                "jp": name.strip(),
                "birth_year": int(year.group(1)) if year else None,
                "sex": sex.group(1) if sex else None,
                "trainer_jp": trainer.group(1).strip() if trainer else None,
                "sire": parents[0].strip() if len(parents) > 0 else None,
                "dam": parents[1].strip() if len(parents) > 1 else None,
            }
        )
    return out


def main() -> None:
    rows = json.loads(ENTRIES.read_text(encoding="utf-8"))
    targets = []
    for r in rows:
        m = re.search(r"\(([A-Z]{2,3})\)\s*$", r["en"])
        origin = m.group(1) if m else None
        en = r["en"][: m.start()].strip() if m else r["en"].strip()
        targets.append({"en": en, "origin": origin, "age": r["age"], "no": r["no"]})

    result: dict[str, dict] = {}
    for i, t in enumerate(targets, 1):
        if t["origin"] != "JPN":
            # 非日本籍本轮不维护血统，留空但保留键位，便于以后扩区
            result[t["en"]] = {"origin": t["origin"], "skipped": True}
            print(f"[{i:02d}/{len(targets)}] {t['en']:24s} skip ({t['origin']})")
            continue

        url = f"https://www.jbis.or.jp/horse/result/?keyword={t['en'].replace(' ', '+')}"
        html = get(url)
        if html is None:
            result[t["en"]] = {"origin": "JPN", "error": "fetch_failed"}
            print(f"[{i:02d}/{len(targets)}] {t['en']:24s} FETCH FAILED")
            time.sleep(SLEEP)
            continue

        cands = parse(html)
        want_year = ANNOUNCE_YEAR - t["age"]
        # 优先：纯假名名 + 生年吻合；其次：纯假名名；再次：任何一条
        def is_jp(c: dict) -> bool:
            return bool(re.fullmatch(r"[\u30a0-\u30ff\u30fc\u4e00-\u9fffー・]+", c["jp"])) and "(" not in c["jp"]

        pick = next((c for c in cands if is_jp(c) and c["birth_year"] == want_year), None)
        if pick is None:
            pick = next((c for c in cands if is_jp(c)), None)
        if pick is None and cands:
            pick = cands[0]

        if pick:
            result[t["en"]] = {
                "origin": "JPN",
                "jbis_id": pick["jbis_id"],
                "jp": pick["jp"],
                "birth_year": pick["birth_year"],
                "birth_year_expect": want_year,
                "match": "exact" if pick["birth_year"] == want_year else "weak",
                "sex": pick["sex"],
                "trainer_jp": pick["trainer_jp"],
                "sire": pick["sire"],
                "dam": pick["dam"],
            }
            print(
                f"[{i:02d}/{len(targets)}] {t['en']:24s} → {pick['jp']:20s} "
                f"{pick['birth_year']} (期望 {want_year}) {'OK' if pick['birth_year']==want_year else 'WEAK'}"
                f"  父={pick['sire']} 母={pick['dam']}"
            )
        else:
            result[t["en"]] = {"origin": "JPN", "error": "no_match"}
            print(f"[{i:02d}/{len(targets)}] {t['en']:24s} NO MATCH")

        time.sleep(SLEEP)

    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    ok = sum(1 for v in result.values() if v.get("jp"))
    print(f"\n完成：{ok}/{len(result)} 拿到日文名 → {OUT}")


if __name__ == "__main__":
    main()
