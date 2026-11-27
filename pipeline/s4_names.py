#!/usr/bin/env python3
"""S4 · 名称与血统 → cache/names.csv + cache/aliases.csv（+ cache/jbis.csv）

顺序是硬要求（IMPL.md §8 / SCHEMA.md §3）：
    港译 hk（公告） → 原文 original（日本马取 JBIS 馬名，其他 = 拉丁名）
    → 覆盖层改 hk / own → 重算 sc → 覆盖层写 alias → 撞车检查

几条不能省的规矩：
  · `sc` 不落库：它是 hk 的纯函数，导出时现算（这一步只做长度校验，不写行）。
  · 名称必带出处：每行都有 source_url + decided_at，没有出处的不入库。
  · 简体永远跟着港译：覆盖层改 hk 后，导出的 sc 自动变，不需要也不允许单独改 sc。
  · 简称只从覆盖层读，机器不生成；撞车不自动消歧，只写 review_queue。
  · JBIS 只打检索页 /horse/result/?keyword=，间隔 8s 走 Guard；抓不到就是 NULL。

用法
  python pipeline/s4_names.py                     # 全量（JBIS 续抓，已有缓存跳过）
  python pipeline/s4_names.py --no-jbis           # 先不联网，只出公告名称
  python pipeline/s4_names.py --jbis-limit 20     # 本轮最多查 20 匹（分批跑 JBIS）
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common as C
import jbis
from netguard import Guard, RiskControlStop

NAME_FIELDS = [
    "entity_type", "entity_uid", "field", "lang", "value", "is_primary",
    "source_url", "archive_url", "decided_at",
]
ALIAS_FIELDS = ["entity_type", "entity_uid", "seq", "text", "note", "updated_at"]
JBIS_FIELDS = [
    "horse_uid", "en", "jbis_id", "jp", "birth_year", "sire", "dam",
    "trainer_jp", "status", "fetched_at",
]
HORSE_FIELDS = ["horse_uid", "birth_year", "sex", "country_code", "origin_basis", "status", "curation"]
PERSON_FIELDS = ["person_uid", "role", "country_code", "nationality_basis"]

# 公告不给骑练国籍。日本赛事里的汉字姓名按「日本籍」处理（与 demo 同口径），
# 其余一律留空；下面这几个是人工标注过的常客，可继续扩。
FOREIGN_PERSONS = {
    "j-mickael-barzalona": ("FRA", "法国"),
    "j-christophe-lemaire": ("FRA", "法国"),
    "j-cristian-demuro": ("ITA", "意大利"),
    "j-joao-moreira": ("BRA", "巴西"),
    "j-alexis-pouchin": ("FRA", "法国"),
    "t-francis-henri-graffard": ("FRA", "法国"),
}
ISO3_TO_CN = {
    "JPN": "日本", "FRA": "法国", "ITA": "意大利", "BRA": "巴西", "GBR": "英国",
    "IRL": "爱尔兰", "USA": "美国", "AUS": "澳洲", "NZL": "新西兰", "GER": "德国",
    "CAN": "加拿大", "ZAF": "南非", "KOR": "韩国", "SGP": "新加坡", "HKG": "中国香港",
    "CHN": "中国内地", "ARG": "阿根廷", "CHI": "智利", "PER": "秘鲁", "URY": "乌拉圭",
}

# ── JBIS 检索页（解析与限速都在 jbis.py / netguard.py，这里只调用）──────
# 「检索页一次给足 5 个字段」这条口径见 docs/IMPL.md §8；血统与生日跑 s6。


# ── 名称装配 ────────────────────────────────────────────────────────────
def add_name(rows: list[dict], *, etype: str, uid: str, field: str, lang: str, value: str | None,
             source_url: str, archive_url: str | None, decided_at: str, primary: int = 1) -> None:
    value = (value or "").strip()
    if not value:
        return
    rows.append({
        "entity_type": etype, "entity_uid": uid, "field": field, "lang": lang,
        "value": value, "is_primary": primary,
        "source_url": source_url or "", "archive_url": archive_url or "",
        "decided_at": decided_at or C.today(),
    })


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="S4 · 名称与血统")
    ap.add_argument("--no-jbis", action="store_true", help="跳过 JBIS（不联网，只出公告名称）")
    ap.add_argument("--jbis-limit", type=int, default=None, help="本轮最多查几匹（分批跑）")
    a = ap.parse_args(argv)

    races = C.read_csv(C.RACES_CSV)
    entries = C.read_csv(C.ENTRIES_CSV)
    if not races or not entries:
        print("缺少 cache/races.csv 或 cache/entries.csv（先跑 python pipeline/s3_parse.py）")
        return 1

    overrides = C.load_overrides()
    overrides_sha = C.sha256_file(C.OVERRIDES) if C.OVERRIDES.exists() else ""

    # 公告页的存档地址（S2b 跑过才有；没跑就留空，页面显示「原件未存档」）
    archive_map = {r["target_url"]: r.get("snapshot_url", "") for r in C.read_csv(C.ARCHIVE_CSV)}

    race_by_uid = {r["race_uid"]: r for r in races}
    names: list[dict] = []
    apply_log: list[dict] = []
    problems: list[str] = []
    name_conflicts: list[dict] = []      # 同一匹马多个港译 → review_queue(kind=name)
    horses_rows: list[dict] = []
    persons_rows: list[dict] = []
    person_facts: dict[str, dict] = {}
    xid_rows: list[dict] = []

    # ── 1. 赛事名：公告标题去掉「– 最後宣佈…」就是港译 ────────────────────
    for r in races:
        hk = C.race_title_zh(r["title"])
        add_name(names, etype="race", uid=r["race_uid"], field="hk", lang="zh-Hant", value=hk,
                 source_url=r["article_url"], archive_url=archive_map.get(r["article_url"]),
                 decided_at=r["announce_date"] or C.today())
        if not hk:
            problems.append(f"race 无名：{r['race_uid']}")

    # ── 2. 马匹：港译取公告（多数决），英文取拉丁名，原文日本马取 JBIS ─────
    per_horse: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        per_horse[e["horse_uid"]].append(e)

    jbis_old = {r["horse_uid"]: r for r in C.read_csv(C.JBIS_CSV)}
    jbis_new: dict[str, dict] = dict(jbis_old)
    targets = []
    for uid, es in per_horse.items():
        origin = (es[0].get("origin") or "").upper()
        en = next((e["horse_en"] for e in es if e.get("horse_en")), "")
        if origin == "JPN" and en and uid not in jbis_old:
            targets.append((uid, en, es))

    g = None
    if targets and not a.no_jbis:
        g = Guard()
        if a.jbis_limit:
            targets = targets[: a.jbis_limit]
        print(f"JBIS：待查 {len(targets)} 匹（间隔见 netguard，命中即缓存）")
        try:
            for n, (uid, en, es) in enumerate(targets, 1):
                years = [int(race_by_uid[e["race_uid"]]["race_date"][:4]) - int(e["age"])
                         for e in es if e.get("age") and race_by_uid.get(e["race_uid"], {}).get("race_date")]
                want = Counter(years).most_common(1)[0][0] if years else None
                info = jbis.search(g, en, want)
                jbis_new[uid] = {
                    "horse_uid": uid, "en": en,
                    "jbis_id": info.get("jbis_id", ""), "jp": info.get("jp", ""),
                    "birth_year": info.get("birth_year") or "",
                    "sire": info.get("sire", "") or "", "dam": info.get("dam", "") or "",
                    "trainer_jp": info.get("trainer_jp", "") or "",
                    "status": info.get("status", ""), "fetched_at": C.now_iso(),
                }
                if n % 20 == 0:
                    print(f"  … JBIS {n}/{len(targets)}", flush=True)
                # 每 50 匹落一次盘：一批 300 匹要跑一小时，中途崩掉不该白跑
                if n % 50 == 0:
                    C.write_csv(C.JBIS_CSV, list(jbis_new.values()), JBIS_FIELDS)
        except RiskControlStop as e:
            print(str(e), file=sys.stderr)
            C.write_csv(C.JBIS_CSV, list(jbis_new.values()), JBIS_FIELDS)
            return 2

    for uid, es in per_horse.items():
        en = next((e["horse_en"] for e in es if e.get("horse_en")), "")
        origin = next((e["origin"] for e in es if e.get("origin")), "") or ""
        # 港译：同一匹马在多场公告里可能有不同写法，取出现次数最多的那个当主名
        zh_votes = Counter(e["horse_zh"] for e in es if e.get("horse_zh"))
        hk = zh_votes.most_common(1)[0][0] if zh_votes else None
        src_race = next((race_by_uid[e["race_uid"]] for e in es
                         if e.get("horse_zh") == hk and e["race_uid"] in race_by_uid), race_by_uid[es[0]["race_uid"]])
        src_url, arc = src_race["article_url"], archive_map.get(src_race["article_url"])
        decided = src_race["announce_date"] or C.today()

        jb = jbis_new.get(uid) or {}
        jp = jb.get("jp") if jb.get("status") in ("ok", "weak") else None
        add_name(names, etype="horse", uid=uid, field="hk", lang="zh-Hant", value=hk,
                 source_url=src_url, archive_url=arc, decided_at=decided)
        add_name(names, etype="horse", uid=uid, field="en", lang="en", value=en,
                 source_url=src_url, archive_url=arc, decided_at=decided)
        add_name(names, etype="horse", uid=uid, field="original",
                 lang="ja" if jp else "en", value=jp or en,
                 source_url="https://www.jbis.or.jp/" if jp else src_url,
                 archive_url=None if jp else arc,
                 decided_at=(jb.get("fetched_at") or decided)[:10])
        if len(zh_votes) > 1:
            # 同一匹马在不同公告里被译成两个中文名：names 表只能留一个（先到者），
            # 另一个不能就这么丢掉 —— 进人工队列，等人判定哪个是正式港译。
            problems.append(f"港译多写法 {uid}: " + " / ".join(zh_votes))
            name_conflicts.append({
                "uid": uid, "votes": list(zh_votes), "kept": hk,
                "source_url": src_url, "archive_url": arc,
            })

        # 事实表：sex 取多数决；生年只填有出处的（JBIS 生年 > 公告年−年龄）
        sex = Counter(e["sex"] for e in es if e.get("sex")).most_common(1)
        ages = [(int(race_by_uid[e["race_uid"]]["race_date"][:4]) - int(e["age"]), e["race_uid"])
                for e in es
                if e.get("age") and race_by_uid.get(e["race_uid"], {}).get("race_date")]
        birth = jb.get("birth_year") or (Counter(a for a, _ in ages).most_common(1)[0][0] if ages else None)
        _, _, iso3 = C.origin_meta(origin)
        horses_rows.append({
            "horse_uid": uid,
            "birth_year": birth if birth else "",
            "sex": sex[0][0] if sex else "",
            "country_code": iso3 if iso3 != "UNK" else "",
            "origin_basis": "docx_suffix" if origin else "",
            "status": "unknown",
            "curation": "curated" if origin == "JPN" else "pending",
        })
        if jb.get("jbis_id"):
            xid_rows.append({
                "entity_type": "horse", "entity_uid": uid, "system": "jbis",
                "external_id": jb["jbis_id"],
                "confidence": 1.0 if jb.get("status") == "ok" else 0.6,
            })

    # ── 3. 人物名：汉字姓名即港译；非汉字姓名只有拉丁栏 ──────────────────
    # 归一并过的人物（s3b）会有多条拉丁写法：主名一条、其余作非主名行留着，
    # 这样「A P O'Brien」仍然搜得到「Aidan O'Brien」那一页，而不是被并没了。
    id_variants = C.read_csv(C.CACHE / "identity_variants.csv")
    id_primary = {r["entity_uid"]: r["value"] for r in id_variants if r.get("is_primary") == "1"}
    per_person: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        if e.get("jockey_uid"):
            per_person[e["jockey_uid"]].append({**e, "_role": "jockey"})
        if e.get("trainer_uid"):
            per_person[e["trainer_uid"]].append({**e, "_role": "trainer"})
    for uid, es in per_person.items():
        role = es[0]["_role"]
        # 取出现最多的那个写法，而不是「第一个非空」：同一人在几千条出赛里拼写会飘
        # （Michael Dee 有 1 条被写成中文名当英文），取首条会把整页标题写成错的。
        # 港译优先要有汉字的写法——汉字姓名才是港译，拉丁名只是同一栏的填充值。
        cnt_zh = Counter(e[f"{role}_zh"] for e in es if e.get(f"{role}_zh"))
        cnt_en = Counter(e[f"{role}_en"] for e in es if e.get(f"{role}_en"))
        zh_pool = [(v, n) for v, n in cnt_zh.items() if C.has_cjk(v)] or list(cnt_zh.items())
        zh = max(zh_pool, key=lambda x: x[1])[0] if zh_pool else ""
        en = id_primary.get(uid) or (max(cnt_en.items(), key=lambda x: x[1])[0] if cnt_en else "")
        src_race = next((race_by_uid.get(e["race_uid"], {}) for e in es
                         if zh and e.get(f"{role}_zh") == zh), race_by_uid.get(es[0]["race_uid"], {}))
        src_url, arc = src_race.get("article_url", ""), archive_map.get(src_race.get("article_url", ""))
        decided = src_race.get("announce_date") or C.today()
        add_name(names, etype="person", uid=uid, field="hk", lang="zh-Hant", value=zh,
                 source_url=src_url, archive_url=arc, decided_at=decided)
        add_name(names, etype="person", uid=uid, field="en", lang="en", value=en,
                 source_url=src_url, archive_url=arc, decided_at=decided)
        # 原文 = 所属语区的原始书写（SCHEMA §2.1）：海外骑练的原文就是拉丁名，港译只是译名。
        # 以前「汉字姓名即原文」把 4157 条人物的原文写成了中文，等于把 L1 港译冒充原文；
        # 现在有拉丁名就取拉丁名，只有纯中文名（拿不到拉丁写法）才落原文。
        add_name(names, etype="person", uid=uid, field="original", lang="en" if en else "zh-Hant",
                 value=en or zh, source_url=src_url, archive_url=arc, decided_at=decided)

        # 国籍：公告不给。人工标注优先，其次「汉字姓名 + 该场公告属日本/香港」按举办地判，
        # 两者都不成立就留空（宁可显示「—」，也不按拼写猜国籍）。
        if uid in FOREIGN_PERSONS:
            code, basis = FOREIGN_PERSONS[uid][0], "manual"
        elif C.has_cjk(zh):
            regions = {race_by_uid[e["race_uid"]]["region"] for e in es if e["race_uid"] in race_by_uid}
            if regions == {"日本"}:
                code, basis = "JPN", "announce_region"
            elif regions == {"香港"}:
                code, basis = "HKG", "announce_region"
            else:
                code, basis = "", ""
        else:
            code, basis = "", ""
        persons_rows.append({
            "person_uid": uid, "role": role, "country_code": code, "nationality_basis": basis,
        })
        person_facts[uid] = {"role": role, "country_code": code, "nationality_cn": ISO3_TO_CN.get(code, ""),
                             "display_zh": zh, "display_en": en, "horses": sorted({e["horse_uid"] for e in es})}

    # 归一并进来的拉丁写法：非主名行，各带自己那场的出处；不上主名位、不进分级
    seen_var: set[tuple[str, str]] = set()
    for r in id_variants:
        if r.get("is_primary") == "1":
            continue
        vkey = (r["entity_uid"], C.norm_key(r["value"]))
        if vkey in seen_var:
            continue
        seen_var.add(vkey)
        src = race_by_uid.get(r.get("race_uid", ""), {})
        url = src.get("article_url", "")
        add_name(names, etype="person", uid=r["entity_uid"], field="en", lang="en",
                 value=r["value"], source_url=url, archive_url=archive_map.get(url),
                 decided_at=src.get("announce_date") or C.today(), primary=0)

    # ── 4. 覆盖层：hk / own 覆盖 → 重算 sc（校验长度）→ alias ─────────────
    # 主名索引：覆盖层按 (实体, 名栏) 替换主名。多写法（同一栏的多条）另存一张表，
    # 键带上 value——否则归一并进来的「A P O'Brien」会在写盘前被主名顶掉。
    by_key: dict[tuple[str, str, str], dict] = {}
    all_rows: dict[tuple[str, str, str, str], dict] = {}
    for row in names:
        all_rows[(row["entity_type"], row["entity_uid"], row["field"], row["value"])] = row
        if int(row.get("is_primary") or 0):
            by_key[(row["entity_type"], row["entity_uid"], row["field"])] = row

    uid_type = {r["race_uid"]: "race" for r in races}
    uid_type.update({u: "horse" for u in per_horse})
    uid_type.update({u: "person" for u in per_person})

    for uid, patch in overrides.items():
        etype = uid_type.get(uid)
        if not etype:
            problems.append(f"覆盖层指向不存在的实体：{uid}")
            continue
        for field in ("hk", "own"):
            val = patch.get(field)
            if not (isinstance(val, str) and val.strip()):
                continue
            key = (etype, uid, field)
            old = by_key.get(key, {}).get("value")
            row = {
                "entity_type": etype, "entity_uid": uid, "field": field,
                "lang": "zh-Hant", "value": val.strip(), "is_primary": 1,
                "source_url": C.OVERRIDES.as_posix(), "archive_url": "",
                "decided_at": C.today(),
            }
            by_key[key] = row
            if old:
                all_rows.pop((etype, uid, field, old), None)
            all_rows[(etype, uid, field, row["value"])] = row
            apply_log.append({
                "applied_at": C.now_iso(), "overrides_sha": overrides_sha,
                "entity_type": etype, "entity_uid": uid, "field": field,
                "old_value": old or "", "new_value": val.strip(),
            })
            if field == "hk":
                hk_new = val.strip()
                C.simplified(hk_new)          # 长度不变校验：不通过直接抛错，拒绝落库
                if old and old != hk_new:
                    problems.append(f"人工改港译 {uid}: {old} → {hk_new}")

    names = list(all_rows.values())

    # ── 5. 简称：只读覆盖层，全量替换；撞车写 review_queue ─────────────────
    aliases: list[dict] = []
    # 身份归一的待判组（s3b）：原样带进 review_queue，人工判完写回 entity-merges.json
    review: list[dict] = C.read_csv(C.CACHE / "review_identity.csv")
    for item in name_conflicts:
        review.append({
            "kind": "name", "entity_type": "horse", "entity_uid": item["uid"],
            "payload": json.dumps({"votes": item["votes"], "kept": item["kept"],
                                   "source_url": item["source_url"],
                                   "archive_url": item["archive_url"]}, ensure_ascii=False),
            "reason": "同一匹马出现多个港译写法（names 表先到者优先，其余待人判定）",
            "status": "open", "created_at": C.now_iso(), "resolved_at": "",
        })
    for uid, patch in overrides.items():
        etype = uid_type.get(uid)
        if not etype:
            continue
        for seq, item in enumerate(C.norm_alias(patch.get("alias"))):
            aliases.append({
                "entity_type": etype, "entity_uid": uid, "seq": seq,
                "text": item["text"], "note": item["note"] or "", "updated_at": C.today(),
            })
    collisions: dict[str, list[str]] = defaultdict(list)
    for al in aliases:
        collisions[al["text"]].append(al["entity_uid"])
    for text, uids in collisions.items():
        if len(uids) > 1:
            review.append({
                "kind": "alias", "entity_type": "", "entity_uid": "",
                "payload": json.dumps({"text": text, "uids": sorted(set(uids))}, ensure_ascii=False),
                "reason": "同一简称被多个实体使用（两边照常展示，不做自动消歧）",
                "status": "open", "created_at": C.now_iso(), "resolved_at": "",
            })

    C.write_csv(C.NAMES_CSV, names, NAME_FIELDS)
    C.write_csv(C.ALIASES_CSV, aliases, ALIAS_FIELDS)
    C.write_csv(C.CACHE / "horses.csv", horses_rows, HORSE_FIELDS)
    C.write_csv(C.CACHE / "persons.csv", persons_rows, PERSON_FIELDS)
    C.write_csv(C.CACHE / "xid.csv", xid_rows,
                ["entity_type", "entity_uid", "system", "external_id", "confidence"])
    C.write_csv(C.CACHE / "override_apply.csv", apply_log,
                ["applied_at", "overrides_sha", "entity_type", "entity_uid", "field", "old_value", "new_value"])
    C.write_csv(C.CACHE / "review_queue.csv", review,
                ["kind", "entity_type", "entity_uid", "payload", "reason", "status", "created_at", "resolved_at"])
    if jbis_new:
        C.write_csv(C.JBIS_CSV, list(jbis_new.values()), JBIS_FIELDS)

    got_ped = sum(1 for u in per_horse if (jbis_new.get(u) or {}).get("status") in ("ok", "weak"))
    jp_total = sum(1 for u, es in per_horse.items() if (es[0].get("origin") or "").upper() == "JPN")
    hk_rows = [n for n in names if n["field"] == "hk"]
    no_sc = sum(1 for n in hk_rows if not C.simplified(n["value"]))
    print(f"名称 {len(names)} 行（港译 {len(hk_rows)} 行，简体可派生 {len(hk_rows) - no_sc} 行）")
    print(f"实体：赛事 {len(races)} / 马匹 {len(per_horse)} / 人物 {len(per_person)}")
    print(f"JBIS 命中：日本马 {got_ped}/{jp_total}"
          + ("（本轮未全查，可续跑）" if a.jbis_limit and got_ped < jp_total else ""))
    n_alias_col = sum(1 for r in review if r["kind"] == "alias")
    print(f"简称 {len(aliases)} 条（全部来自覆盖层），简称撞车 {n_alias_col} 组；"
          f"港译多写法 {len(name_conflicts)} 条 → review_queue")
    print(f"人工层：{len(overrides)} 条 patch，套用记录 {len(apply_log)} 条")
    if problems:
        print("提示：")
        for p in problems[:15]:
            print("  -", p)
        if len(problems) > 15:
            print(f"  … 共 {len(problems)} 条")
    print(f"→ {C.NAMES_CSV}\n→ {C.ALIASES_CSV}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
