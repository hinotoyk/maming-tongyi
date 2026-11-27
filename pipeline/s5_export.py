#!/usr/bin/env python3
"""S5 · 导出站点数据 → public/data/

输入  cache/maming.sqlite（事实表 + name + entity_alias）+ cache/jbis.csv（血统）+ 覆盖层
输出
  public/data/manifest.json          版本 / 生成时间 / 计数 / curation 覆盖 / 分片数
  public/data/horses/shard-000.json  每片 500
  public/data/races/shard-000.json
  public/data/persons/shard-000.json
  public/data/search/all.json        名称检索索引（给索引页客户端搜索）
  public/data/search/name-XXXX.json  按词首字符分片（每个首字一片，十六进制码位命名）
  public/data/search/index.json      首字 → 分片文件

口径（SCHEMA.md §4）：
  · 一实体一 URL 不变，但**存储分片**：2 万个碎文件会拖死 Windows 文件系统与 git 索引。
  · 每个文件带 `_meta`（schema / updated / count），裸数组无法判断新旧。
  · `sc` 在这一步现算（不落库），永远跟着 hk 走。
  · `curation` 的覆盖范围必须进 manifest —— 哪些地区校过、哪些没校，公开可查。

用法：python pipeline/s5_export.py
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common as C

SHARD_SIZE = 500
# 分页行的加权轮转顺序：curated 马 / 其他马 / 赛事 / 骑手 / 练马师。
# 与 src/lib/data.ts 的 WEIGHTS 一一对应；两边不一致会导致首屏与第 1 页不是同一批行。
WEIGHTS = (3, 2, 1, 1, 1)
SCHEMA = "names/v3"
ROLE_CN = {"jockey": "骑手", "trainer": "练马师"}
COUNTRY_CN = {
    "JPN": "日本", "USA": "美国", "CAN": "加拿大", "GBR": "英国", "IRL": "爱尔兰",
    "FRA": "法国", "DEU": "德国", "ITA": "意大利", "ESP": "西班牙", "AUS": "澳洲",
    "NZL": "新西兰", "ZAF": "南非", "BRA": "巴西", "ARG": "阿根廷", "CHL": "智利",
    "PER": "秘鲁", "URY": "乌拉圭", "KOR": "韩国", "SGP": "新加坡", "HKG": "中国香港",
    "CHN": "中国内地", "ARE": "阿联酋", "TUR": "土耳其", "SWE": "瑞典", "DNK": "丹麦",
    "NOR": "挪威", "FIN": "芬兰", "POL": "波兰", "CZE": "捷克", "HUN": "匈牙利",
    "AUT": "奥地利", "CHE": "瑞士", "BEL": "比利时", "NLD": "荷兰", "RUS": "俄罗斯",
    "IND": "印度", "MYS": "马来西亚",
}
SEX_CN = {"M": "雄", "F": "雌", "G": "骟"}


def q(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(sql, args).fetchall()


def build_names(conn: sqlite3.Connection) -> dict[tuple[str, str], dict]:
    """name + entity_alias → { (type, uid): {original,en,hk,sc,own,alias[],variants[]} }

    同一栏可以有多行：主名一行，其余是同一个人的其他写法（归一后并进来的）。
    主名按 is_primary DESC 先取，剩下的进 `variants`——检索要吃全部写法，
    页面只显示主名，人物页把 variants 列成「其他写法」。
    """
    out: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"original": None, "en": None, "hk": None, "sc": None, "own": None,
                 "alias": [], "variants": []}
    )
    for r in q(conn, "SELECT * FROM name ORDER BY is_primary DESC, decided_at DESC"):
        key = (r["entity_type"], r["entity_uid"])
        field = r["field"]
        if field not in ("original", "en", "hk", "own"):
            continue
        if not out[key].get(field):
            out[key][field] = r["value"]
            continue
        if r["value"] != out[key][field]:
            out[key]["variants"].append({"field": field, "value": r["value"]})
    for r in q(conn, "SELECT * FROM entity_alias ORDER BY entity_type, entity_uid, seq"):
        out[(r["entity_type"], r["entity_uid"])]["alias"].append({"text": r["text"], "note": r["note"] or None})
    for names in out.values():
        names["sc"] = C.simplified(names["hk"])
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="S5 · 导出分片 + manifest + 搜索索引")
    ap.add_argument("--quiet", action="store_true", help="只打印结果行")
    ap.add_argument("--char-shards", action="store_true",
                    help="额外生成按首字分片的 search/name-*.json（体积翻倍，默认关）")
    a = ap.parse_args(argv)
    char_shards = a.char_shards
    if not C.SQLITE.exists():
        print(f"缺少 {C.SQLITE}（先跑 python pipeline/build_db.py）")
        return 1

    conn = sqlite3.connect(C.SQLITE)
    names = build_names(conn)

    # 血统：schema 里没有 pedigree 表，JBIS 结果留在 cache/jbis.csv（原件不入库）
    jbis = {r["horse_uid"]: r for r in C.read_csv(C.JBIS_CSV)}

    # 公告页 / 附件的存档地址：archive 表里有就用回放地址，没有就留空
    archive = {r["target_url"]: r["snapshot_url"] for r in q(conn, "SELECT target_url, snapshot_url FROM archive")
               if r["snapshot_url"]}
    doc_meta = {m["race_uid"]: m for m in C.read_csv(C.DOCS / "manifest.csv")}

    entries_by_race: dict[str, list[dict]] = defaultdict(list)
    entries_by_horse: dict[str, list[dict]] = defaultdict(list)
    for e in q(conn, "SELECT * FROM entry ORDER BY race_uid, saddle_no"):
        row = {
            "no": e["saddle_no"], "draw": e["draw"], "gear": e["gear"],
            "horse_uid": e["horse_uid"], "jockey_uid": e["jockey_uid"], "trainer_uid": e["trainer_uid"],
            "lb": e["weight_lb"], "claim": e["claim_lb"],
            "rating": e["intl_rating"], "sex": e["sex"], "age": e["age"],
            "race_uid": e["race_uid"],
        }
        entries_by_race[e["race_uid"]].append(row)
        entries_by_horse[e["horse_uid"]].append(row)

    races = []
    for r in q(conn, "SELECT * FROM race ORDER BY race_date DESC, race_uid"):
        meta = doc_meta.get(r["race_uid"], {})
        src = {
            "article": {"url": meta.get("article_url", ""),
                        "archive_url": archive.get(meta.get("article_url", ""), "")},
            "attachment": {"url": meta.get("attach_url", ""),
                           "archive_url": archive.get(meta.get("attach_url", ""), "")},
        }
        races.append({
            "uid": r["race_uid"],
            "names": names[("race", r["race_uid"])],
            "announce_date": r["announce_date"],
            "race_date": r["race_date"], "race_time": r["race_time"], "session": r["session"],
            "grade": r["grade"], "grade_cn": r["grade_cn"],
            "venue": r["venue"], "venue_country": r["venue_country"], "course": r["course"],
            "distance": r["distance"], "surface": r["surface"],
            "entries": entries_by_race.get(r["race_uid"], []),
            "source": src,
        })

    horses = []
    # 血统：pedigree_node 一行一格 → 每匹一棵 {父:[[..]]×5, 母:[[..]]×5}
    ped_nodes: dict[str, dict[str, dict[int, dict[int, dict]]]] = defaultdict(
        lambda: {"父": defaultdict(dict), "母": defaultdict(dict)}
    )
    for r in q(conn, "SELECT horse_uid, side, depth, idx, name, sex, birth_year, color, jbis_id "
                     "FROM pedigree_node ORDER BY horse_uid, side, depth, idx"):
        node = {"n": r["name"]}
        if r["sex"]:
            node["s"] = r["sex"]
        if r["birth_year"]:
            node["y"] = r["birth_year"]
        # 深代（4-5 代）只留名字与生年：62 格全塞毛色/ID 会让分片从 22 MB 涨到 30 MB 以上
        if r["depth"] <= 3:
            if r["color"]:
                node["c"] = r["color"]
            if r["jbis_id"]:
                node["id"] = r["jbis_id"]
        ped_nodes[r["horse_uid"]][r["side"]][r["depth"]][r["idx"]] = node
    ped_meta = {r["horse_uid"]: r for r in q(conn, "SELECT * FROM pedigree_meta")}

    def pedigree_of(h: sqlite3.Row) -> dict | None:
        uid = h["horse_uid"]
        jb = jbis.get(uid) or {}
        meta = ped_meta.get(uid)
        if not (meta and jb.get("jbis_id")):
            return None
        sides = ped_nodes.get(uid) or {"父": {}, "母": {}}
        if not any(sides.get("父")) and not any(sides.get("母")):
            return None
        tree = {}
        for side in ("父", "母"):
            rows = []
            for depth in range(1, 6):
                slot = sides.get(side, {}).get(depth) or {}
                rows.append([slot.get(i) for i in range(2 ** (depth - 1))] if slot else [])
            tree[side] = rows

        def nm(side: str, depth: int, idx: int) -> str | None:
            rows = tree.get(side) or []
            node = rows[depth - 1][idx] if depth <= len(rows) and idx < len(rows[depth - 1]) else None
            return (node or {}).get("n")

        return {
            "source": "JBIS",
            "jbis_id": jb["jbis_id"],
            "birth_date": h["birth_date"],
            "birth_year": h["birth_year"],
            "color": h["color"],
            "fno": meta["fno"] or None,
            "cross": meta["cross"] or None,
            "sire": nm("父", 1, 0),
            "dam": nm("母", 1, 0),
            "siresire": nm("父", 2, 0),
            "damsire": nm("母", 2, 0),
            "tree": tree,
            "fetched_at": (jb.get("fetched_at") or "")[:10],
        }

    for h in q(conn, "SELECT * FROM horse ORDER BY horse_uid"):
        uid = h["horse_uid"]
        es = sorted(entries_by_horse.get(uid, []), key=lambda e: e["race_uid"])
        first = es[0] if es else {}
        ped = pedigree_of(h)
        horses.append({
            "uid": uid,
            "names": names[("horse", uid)],
            "origin": h["country_code"] or "",
            "country": COUNTRY_CN.get(h["country_code"], h["country_code"] or "—"),
            "sex": h["sex"], "sex_cn": SEX_CN.get(h["sex"] or "", ""),
            "birth_date": h["birth_date"], "birth_year": h["birth_year"], "color": h["color"],
            "age": first.get("age"), "rating": first.get("rating"), "lb": first.get("lb"),
            "no": first.get("no"), "draw": first.get("draw"), "gear": first.get("gear"),
            "pedigree": ped,
            "curation": h["curation"],
            "status": "curated" if h["curation"] == "curated" else "pending",
            "jockey_uid": first.get("jockey_uid"), "trainer_uid": first.get("trainer_uid"),
            "race_uid": first.get("race_uid"),
            "entries": es,
        })

    horses_by_person: dict[str, set[str]] = defaultdict(set)
    for e in q(conn, "SELECT jockey_uid, trainer_uid, horse_uid FROM entry"):
        if e["jockey_uid"]:
            horses_by_person[e["jockey_uid"]].add(e["horse_uid"])
        if e["trainer_uid"]:
            horses_by_person[e["trainer_uid"]].add(e["horse_uid"])
    persons = []
    for p in q(conn, "SELECT * FROM person ORDER BY role, person_uid"):
        persons.append({
            "uid": p["person_uid"], "role": p["role"], "role_cn": ROLE_CN.get(p["role"], p["role"]),
            "nationality": p["country_code"] or "", "nationality_cn": COUNTRY_CN.get(p["country_code"], "—"),
            "names": names[("person", p["person_uid"])],
            "horses": sorted(horses_by_person.get(p["person_uid"], ())),
            # 同名跨角色：一个人可能既是骑手又是练马师（Donnacha O'Brien）。
            # 按 P0 的口径不并成一条（role 是 person 的主键维度），但两页要互链。
            "counterpart": [],
        })

    # 港译 → 角色 → uid：用来给跨角色的同名人物互相指路
    hk_role: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for p in persons:
        hk = p["names"].get("hk")
        if not hk:
            continue
        try:
            k = C.norm_key(C.simplified(hk))
        except Exception:
            k = C.norm_key(hk)
        hk_role[k][p["role"]].append(p["uid"])
    for p in persons:
        hk = p["names"].get("hk")
        if not hk:
            continue
        try:
            k = C.norm_key(C.simplified(hk))
        except Exception:
            k = C.norm_key(hk)
        other = "trainer" if p["role"] == "jockey" else "jockey"
        p["counterpart"] = sorted(u for u in hk_role[k].get(other, []) if u != p["uid"])
    conn.close()

    # ── 落盘 ────────────────────────────────────────────────────────────
    out = C.PUBLIC_DATA
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    updated = C.today()

    def write_shards(kind: str, items: list[dict]) -> int:
        d = out / kind
        d.mkdir(parents=True, exist_ok=True)
        n = 0
        for i in range(0, max(len(items), 1), SHARD_SIZE):
            chunk = items[i:i + SHARD_SIZE]
            C.write_json(d / f"shard-{n:03d}.json", {
                "_meta": {"schema": SCHEMA, "updated": updated, "count": len(chunk), "kind": kind},
                "items": chunk,
            }, indent=0)
            n += 1
        return n

    shards = {
        "horses": write_shards("horses", horses),
        "races": write_shards("races", races),
        "persons": write_shards("persons", persons),
    }

    # 检索索引：键 = 归一化名称（小写去空白 + 繁简双向展开），值 = **列表** [{kind, uid, m}]
    # m 是「这个键命中哪些名栏」的位掩码：o 原文 1 / e 英文 2 / h 港译 4 / s 简体 8 /
    # w 自译 16 / a 简称 32。页面上的「按字段检索」就是拿 m 做与运算，
    # 这样同一个键既能整词命中，也能只命中某一个栏，不必为六个栏各存一份索引。
    #
    # 值必须是列表：同名不同实体是常态（同名赛事、不同年份的同名马、父子同名练马师）。
    # 旧版一个键只留一个实体，28586 个实体里有 2086 个因此完全搜不到。
    MASK = {"o": 1, "e": 2, "h": 4, "s": 8, "w": 16, "a": 32}
    search: dict[str, list[dict]] = {}
    for kind, items in (("horse", horses), ("race", races), ("person", persons)):
        for it in items:
            n = it["names"]
            pairs = [(f, n.get(k)) for f, k in
                     (("o", "original"), ("e", "en"), ("h", "hk"), ("s", "sc"), ("w", "own"))]
            pairs += [("e", v["value"]) for v in (n.get("variants") or []) if v["field"] == "en"]
            pairs += [("h", v["value"]) for v in (n.get("variants") or []) if v["field"] == "hk"]
            pairs += [("a", a["text"]) for a in (n.get("alias") or [])]
            for field, t in pairs:
                for key in C.norm_variants(t or ""):
                    bucket = search.setdefault(key, [])
                    for entry in bucket:
                        if entry["kind"] == kind and entry["uid"] == it["uid"]:
                            entry["m"] |= MASK[field]
                            break
                    else:
                        bucket.append({"kind": kind, "uid": it["uid"], "m": MASK[field]})

    # 索引页 / 改名页的分页行。顺序就是翻页顺序：SSR 首屏是它前 200 条，
    # 客户端第 N 页也按这个数组切片——两边必须同一份顺序，否则翻页会跳行或重复。
    #
    # 排序规则：五条队列加权轮转（curated 马 3、其他马 2、赛事 1、骑手 1、练马师 1）。
    # 等价实现写在 src/lib/data.ts 的 WEIGHTS，改这里要同步那边；check_data 会比对两边首屏。
    queues: list[list[dict]] = [
        [{
            "k": "horse", "uid": h["uid"], "cn": "马匹", "f": "horse",
            "o": h["names"]["original"], "e": h["names"]["en"], "h": h["names"]["hk"],
            "s": h["names"]["sc"], "w": h["names"]["own"],
            "a": [x["text"] for x in h["names"]["alias"]],
            "c": h["country"], "cc": h["origin"] or "",
            "st": "已校订", "cls": "lv-l1",
        } for h in horses if h["curation"] == "curated"],
        [{
            "k": "horse", "uid": h["uid"], "cn": "马匹", "f": "horse",
            "o": h["names"]["original"], "e": h["names"]["en"], "h": h["names"]["hk"],
            "s": h["names"]["sc"], "w": h["names"]["own"],
            "a": [x["text"] for x in h["names"]["alias"]],
            "c": h["country"], "cc": h["origin"] or "",
            "st": "待维护", "cls": "lv-l3",
        } for h in horses if h["curation"] != "curated"],
        [{
            "k": "race", "uid": r["uid"], "cn": "赛事", "f": "race",
            "o": r["names"]["original"], "e": r["names"]["en"], "h": r["names"]["hk"],
            "s": r["names"]["sc"], "w": r["names"]["own"],
            "a": [x["text"] for x in r["names"]["alias"]],
            "c": r["venue_country"] or "—", "cc": "", "st": "公告直取", "cls": "lv-src",
        } for r in races],
        [{
            "k": "person", "uid": p["uid"], "cn": p["role_cn"], "f": p["role"],
            "o": p["names"]["original"], "e": p["names"]["en"], "h": p["names"]["hk"],
            "s": p["names"]["sc"], "w": p["names"]["own"],
            "a": [x["text"] for x in p["names"]["alias"]],
            "c": p["nationality_cn"] or "—", "cc": p["nationality"] or "",
            "st": "公告直取", "cls": "lv-src",
        } for p in persons if p["role"] == "jockey"],
        [{
            "k": "person", "uid": p["uid"], "cn": p["role_cn"], "f": p["role"],
            "o": p["names"]["original"], "e": p["names"]["en"], "h": p["names"]["hk"],
            "s": p["names"]["sc"], "w": p["names"]["own"],
            "a": [x["text"] for x in p["names"]["alias"]],
            "c": p["nationality_cn"] or "—", "cc": p["nationality"] or "",
            "st": "公告直取", "cls": "lv-src",
        } for p in persons if p["role"] != "jockey"],
    ]
    rows = []
    cursor = [0] * len(queues)
    while any(cursor[i] < len(queues[i]) for i in range(len(queues))):
        for i, w in enumerate(WEIGHTS):
            take = queues[i][cursor[i]: cursor[i] + w]
            cursor[i] += len(take)
            rows.extend(take)

    # 血统检索：父 / 母 / 母父 / 父父 各自一张「名字 → 马 uid 列表」。
    # 名称索引是「键 → 单个实体」，一个种马名会挂几百匹产驹，塞不进 items，所以单开一节。
    ped_index: dict[str, dict[str, list[str]]] = {k: defaultdict(list) for k in ("父", "母", "母父", "父父")}
    for h in horses:
        ped = h["pedigree"]
        if not ped:
            continue
        for role, name in (("父", ped.get("sire")), ("母", ped.get("dam")),
                           ("母父", ped.get("damsire")), ("父父", ped.get("siresire"))):
            if name:
                ped_index[role][name].append(h["uid"])
    ped_json = {role: {k: sorted(set(v)) for k, v in names_map.items() if k}
                for role, names_map in ped_index.items()}

    sdir = out / "search"
    sdir.mkdir(parents=True, exist_ok=True)
    C.write_json(sdir / "all.json", {"_meta": {"schema": "search/v3", "updated": updated,
                                               "count": len(search),
                                               "fields": ["o", "e", "h", "s", "w", "a"],
                                               "ped": {k: len(v) for k, v in ped_json.items()}},
                                     "items": search, "ped": ped_json}, indent=0)
    C.write_json(sdir / "rows.json", {"_meta": {"schema": "rows/v2", "updated": updated,
                                               "count": len(rows), "page": 200,
                                               "weights": list(WEIGHTS)}, "items": rows}, indent=0)

    # 首字分片默认不生成：它是 all.json 的第三份副本（2 万匹马就多出 3000 个文件、
    # 11 MB），而索引页客户端只取 all.json + rows.json。要按首字懒加载时再开 --char-shards。
    if char_shards:
        by_char: dict[str, dict] = defaultdict(dict)
        for key, v in search.items():
            by_char[key[0]].update({key: v})
        index: dict[str, dict] = {}
        for ch, items in by_char.items():
            fname = f"name-{ord(ch):04x}.json"
            C.write_json(sdir / fname, {"_meta": {"schema": "search/v1", "updated": updated,
                                                 "char": ch, "count": len(items)}, "items": items}, indent=0)
            index[ch] = {"file": fname, "count": len(items)}
        C.write_json(sdir / "index.json", {"_meta": {"schema": "search/v1", "updated": updated,
                                                    "count": len(index)}, "chars": index}, indent=0)
    else:
        for stale in sdir.glob("name-*.json"):
            stale.unlink()
        (sdir / "index.json").unlink(missing_ok=True)

    # curation 覆盖范围：哪些地区已校订、哪些只抓不维（公开可查）
    regions: dict[str, int] = defaultdict(int)
    for r in C.read_csv(C.INDEX_CSV):
        regions[r["region"]] += 1
    curated_n = sum(1 for h in horses if h["curation"] == "curated")
    manifest = {
        "v": SCHEMA,
        "generated_at": C.now_iso(),
        "updated": updated,
        "counts": {
            "horses": len(horses), "races": len(races), "persons": len(persons),
            "names": sum(1 for h in horses + races + persons if h["names"].get("hk")),
            "aliases": sum(len(h["names"].get("alias") or []) for h in horses + races + persons),
            "pedigree": sum(1 for h in horses if h["pedigree"]),
        },
        "curation": {
            "curated": curated_n,
            "pending": len(horses) - curated_n,
            "policy": "抓取全量、译名只维护日本籍（horse.curation = curated）",
            "regions": [
                {"region": k, "announcements": v, "status": "curated" if k == "日本" else "pending"}
                for k, v in sorted(regions.items(), key=lambda kv: -kv[1])
            ],
        },
        "shards": shards,
        "shard_size": SHARD_SIZE,
        "search": ({"all": "search/all.json", "rows": "search/rows.json", "index": "search/index.json"}
                   if char_shards else {"all": "search/all.json", "rows": "search/rows.json"}),
        "index_total": sum(regions.values()),
    }
    C.write_json(out / "manifest.json", manifest)

    total = sum(f.stat().st_size for f in out.rglob("*.json"))
    print(f"赛事 {len(races)} · 马匹 {len(horses)} · 人物 {len(persons)}"
          f"（已校订 {curated_n}）")
    print(f"名称检索键 {len(search)} 个"
          + (f"，首字分片 {len(list((out / 'search').glob('name-*.json')))} 片" if char_shards else ""))
    print(f"产物 {total / 1e6:.2f} MB（上限 60 MB）→ {out}")
    if total > 60e6:
        print("⚠ 产物超过 60 MB：先看 search/all.json 是否该改成按需加载")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
