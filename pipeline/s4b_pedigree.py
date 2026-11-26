#!/usr/bin/env python3
"""S4b · JBIS 血统补全 → cache/jbis.csv + jbis_profile.csv + pedigree.csv

为什么单独一个阶段：S4 只判名字，血统一匹要两页（基本情報 + 5 代血統），
混在 S4 里会把「名字改一次就重抓一遍血统」的代价带进来。这里按 jbis.csv 的 id 逐匹补，
断点续跑（已有行就跳过），10s 地板串行，跑多久都不会重复请求。

一匹马最多三个请求，缺哪个补哪个：
    1. 没有 jbis_id      → 検索页（英文名 + 生年消歧），顺手补齐 日文名 / 父 / 母
    2. 没有生日/毛色      → 基本情報页 /horse/{id}/
    3. 没有 5 代血统      → 血統页 /horse/{id}/pedigree/

输出（都是派生数据，原页 HTML 不入库）
    cache/jbis.csv           horse_uid,en,jbis_id,jp,birth_year,sire,dam,trainer_jp,status,fetched_at
    cache/jbis_profile.csv   horse_uid,jbis_id,name_jp,name_en,name_country,birth_date,sex_jp,color,
                             birth_place,breeder,owner,trainer_jp,fetched_at
    cache/pedigree.csv       horse_uid,side,depth,idx,name,sex,birth_year,color,jbis_id
    cache/pedigree/{id}.json 单匹血统树（{tree:{父:[[..]],母:[[..]]},fno,cross,...}）

用法
  python pipeline/s4b_pedigree.py                  # 全量补（已有缓存跳过）
  python pipeline/s4b_pedigree.py --limit 200      # 本轮最多补 200 匹
  python pipeline/s4b_pedigree.py --skip-profile   # 只补血统页（省一半请求）
  python pipeline/s4b_pedigree.py --skip-pedigree  # 只补基本情報（出生日期）
  python pipeline/s4b_pedigree.py --ids jp-access,jp-ablaze
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common as C
import jbis
from netguard import Guard, RiskControlStop

PROFILE_CSV = C.CACHE / "jbis_profile.csv"
PEDIGREE_CSV = C.CACHE / "pedigree.csv"
PED_DIR = C.CACHE / "pedigree"

JBIS_FIELDS = [
    "horse_uid", "en", "jbis_id", "jp", "birth_year", "sire", "dam",
    "trainer_jp", "status", "fetched_at",
]
PROFILE_FIELDS = [
    "horse_uid", "jbis_id", "name_jp", "name_en", "name_country", "birth_date",
    "sex_jp", "color", "birth_place", "breeder", "owner", "trainer_jp", "fetched_at",
]
PED_FIELDS = ["horse_uid", "side", "depth", "idx", "name", "sex", "birth_year", "color", "jbis_id"]

SAVE_EVERY = 20


def flatten(uid: str, tree: dict) -> list[dict]:
    rows = []
    for side in ("父", "母"):
        for depth, row in enumerate(tree.get(side) or [], 1):
            for idx, node in enumerate(row):
                if not node or not node.get("name"):
                    continue
                rows.append({
                    "horse_uid": uid, "side": side, "depth": depth, "idx": idx,
                    "name": node["name"], "sex": node.get("sex") or "",
                    "birth_year": node.get("year") or "", "color": node.get("color") or "",
                    "jbis_id": node.get("id") or "",
                })
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="S6 · JBIS 血统补全")
    ap.add_argument("--limit", type=int, default=None, help="本轮最多补几匹")
    ap.add_argument("--ids", default="", help="只跑这几匹（逗号分隔 horse_uid）")
    ap.add_argument("--skip-profile", action="store_true", help="不抓基本情報（出生日期 / 毛色）")
    ap.add_argument("--skip-pedigree", action="store_true", help="不抓 5 代血統")
    ap.add_argument("--no-search", action="store_true", help="没有 jbis_id 就跳过，不去检索页")
    a = ap.parse_args(argv)

    races = {r["race_uid"]: r for r in C.read_csv(C.RACES_CSV)}
    entries = C.read_csv(C.ENTRIES_CSV)
    if not entries:
        print("缺少 cache/entries.csv（先跑 python pipeline/s3_parse.py）")
        return 1

    per_horse: dict[str, list[dict]] = {}
    for e in entries:
        per_horse.setdefault(e["horse_uid"], []).append(e)
    jpn = {u: es for u, es in per_horse.items() if (es[0].get("origin") or "").upper() == "JPN"}
    if a.ids:
        want = {x.strip() for x in a.ids.split(",") if x.strip()}
        jpn = {u: es for u, es in jpn.items() if u in want}

    jbis_rows = {r["horse_uid"]: r for r in C.read_csv(C.JBIS_CSV)}
    prof_rows = {r["horse_uid"]: r for r in C.read_csv(PROFILE_CSV)}
    ped_rows: dict[str, list[dict]] = {}
    for r in C.read_csv(PEDIGREE_CSV):
        ped_rows.setdefault(r["horse_uid"], []).append(r)
    PED_DIR.mkdir(parents=True, exist_ok=True)

    def need(uid: str) -> tuple[bool, bool, bool]:
        jb = jbis_rows.get(uid) or {}
        ok = jb.get("status") in ("ok", "weak") and bool(jb.get("jbis_id"))
        # 检索页明确回「没这匹马」就不必再试：每轮重查一遍只会白烧 10 秒。
        # 抓取失败（fetch-xxx）仍要重试。
        give_up = str(jb.get("status") or "").startswith(("no-match", "empty"))
        return (not ok and not give_up, ok and uid not in prof_rows, ok and uid not in ped_rows)

    todo = [u for u in sorted(jpn) if any(need(u))]
    if a.limit:
        todo = todo[: a.limit]
    if not todo:
        print("无需补全：日本马的血统与生卒信息都已就位。")
        return 0

    n_missing_id = sum(1 for u in todo if need(u)[0])
    print(f"日本马 {len(jpn)} 匹，本轮待补 {len(todo)} 匹"
          f"（其中 {n_missing_id} 匹还没有 jbis_id）"
          f"{'，跳过基本情報' if a.skip_profile else ''}{'，跳过血統页' if a.skip_pedigree else ''}")

    def save() -> None:
        C.write_csv(C.JBIS_CSV, list(jbis_rows.values()), JBIS_FIELDS)
        C.write_csv(PROFILE_CSV, list(prof_rows.values()), PROFILE_FIELDS)
        flat = [r for rows in ped_rows.values() for r in rows]
        C.write_csv(PEDIGREE_CSV, flat, PED_FIELDS)

    g = Guard()
    done = prof = ped = 0
    try:
        for i, uid in enumerate(todo, 1):
            es = jpn[uid]
            want_id, want_prof, want_ped = need(uid)
            if want_id:
                if a.no_search:
                    continue
                en = next((e["horse_en"] for e in es if e.get("horse_en")), "")
                if not en:
                    continue
                years = [int(races[e["race_uid"]]["race_date"][:4]) - int(e["age"])
                         for e in es
                         if e.get("age") and races.get(e["race_uid"], {}).get("race_date")]
                want_year = Counter(years).most_common(1)[0][0] if years else None
                info = jbis.search(g, en, want_year)
                jbis_rows[uid] = {
                    "horse_uid": uid, "en": en,
                    "jbis_id": info.get("jbis_id", ""), "jp": info.get("jp", ""),
                    "birth_year": info.get("birth_year") or "",
                    "sire": info.get("sire", ""), "dam": info.get("dam", ""),
                    "trainer_jp": info.get("trainer_jp", ""),
                    "status": info.get("status", ""), "fetched_at": C.now_iso(),
                }
                if not (jbis_rows[uid]["status"] in ("ok", "weak") and jbis_rows[uid]["jbis_id"]):
                    if i % SAVE_EVERY == 0:
                        save()
                    continue
                hid = jbis_rows[uid]["jbis_id"]
                # 刚拿到 id：这一匹的生日与血统也顺手补齐（不要等下一轮）
                want_prof, want_ped = uid not in prof_rows, uid not in ped_rows
            else:
                hid = jbis_rows[uid]["jbis_id"]

            if want_ped and not a.skip_pedigree:
                p = jbis.pedigree(g, hid)
                if p.get("status") == "ok":
                    rows = flatten(uid, p["tree"])
                    if rows:
                        ped_rows[uid] = rows
                        ped += 1
                        C.write_json(PED_DIR / f"{hid}.json", {
                            "jbis_id": hid, "name_jp": p.get("name_jp"),
                            "name_en": p.get("name_en"), "fno": p.get("fno"),
                            "cross": p.get("cross"), "tree": p["tree"],
                        }, indent=0)
            if want_prof and not a.skip_profile:
                pr = jbis.profile(g, hid)
                if pr.get("status") == "ok":
                    prof_rows[uid] = {
                        "horse_uid": uid, "jbis_id": hid,
                        "name_jp": pr.get("name_jp") or "", "name_en": pr.get("name_en") or "",
                        "name_country": pr.get("name_country") or "",
                        "birth_date": pr.get("birth_date") or "", "sex_jp": pr.get("sex_jp") or "",
                        "color": pr.get("color") or "", "birth_place": pr.get("birth_place") or "",
                        "breeder": pr.get("breeder") or "", "owner": pr.get("owner") or "",
                        "trainer_jp": pr.get("trainer_jp") or "", "fetched_at": C.now_iso(),
                    }
                    prof += 1

            done += 1
            if i % 10 == 0:
                print(f"  … {i}/{len(todo)}（血統 +{ped} / 基本情報 +{prof}）", flush=True)
            if i % SAVE_EVERY == 0:
                save()
    except RiskControlStop as e:
        print(str(e), file=sys.stderr)
        save()
        return 2

    save()
    got_ped = len(ped_rows)
    got_prof = len(prof_rows)
    print(f"本轮完成 {done} 匹：血統 +{ped}（累计 {got_ped}/{len(jpn)}），"
          f"基本情報 +{prof}（累计 {got_prof}/{len(jpn)}）")
    print(f"→ {C.JBIS_CSV}\n→ {PROFILE_CSV}\n→ {PEDIGREE_CSV}")
    missing = [u for u in jpn if u not in ped_rows]
    if missing:
        print(f"还剩 {len(missing)} 匹没血统，续跑同一条命令即可（已有缓存自动跳过）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
