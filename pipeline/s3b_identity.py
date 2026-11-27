#!/usr/bin/env python3
"""S3b · 人物实体归一 → 改写 cache/entries.csv 的人物 uid

这一阶段在 s3_parse（出 uid）与 s4_names（出名称）之间，只做一件事：
把「同一个人的多种写法」并成一条实体，其余全部原样传下去。

输入
  cache/entries.csv / cache/races.csv      s3 产物
  data-src/entity-merges.json              人工确认的合并（唯一入口，进 git）
  data-src/person-splits.json              人工否决的对（永不合并）

输出
  cache/entries.csv                        就地改写 jockey_uid / trainer_uid
  cache/identity_map.csv                   旧 uid → 主 uid（审计用）
  cache/identity_variants.csv              各实体的拉丁写法（主名 + 非主名），s4 据此落 name 行
  cache/review_identity.csv                verdict=review 的组 → s4 合并进 review_queue
  public/review/merge.json                 复核页数据（/edit/merge/）

原则
  · 合并只改 uid，不改名字：被并掉的写法作为 name 的非主名行留着，各自带出处，搜索照样命中。
  · 港译相同只是分桶，判定看拉丁名结构；判不了就进人工，不猜。
  · 人工否决（splits）优先级最高，任何自动规则都不能推翻它。

用法
  python pipeline/s3b_identity.py
  python pipeline/s3b_identity.py --dry-run     # 只出报告，不改 entries.csv
"""
from __future__ import annotations

import argparse
import json
import sys

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common as C
import identity as I

MERGES_JSON = C.DATA_SRC / "entity-merges.json"
SPLITS_JSON = C.DATA_SRC / "person-splits.json"
VARIANT_FIELDS = ["entity_uid", "role", "value", "is_primary", "race_uid"]
MAP_FIELDS = ["old_uid", "new_uid", "role", "hk", "verdict", "reason"]
REVIEW_FIELDS = ["kind", "entity_type", "entity_uid", "payload", "reason", "status", "created_at", "resolved_at"]


def load_merges() -> dict[str, list[str]]:
    doc = C.read_json(MERGES_JSON, {}) or {}
    entries = doc.get("entries", doc) if isinstance(doc, dict) else {}
    out: dict[str, list[str]] = {}
    for k, v in entries.items():
        if str(k).startswith("_") or not isinstance(v, dict):
            continue
        out[k] = [x for x in (v.get("merge") or []) if x]
    return out


def load_splits() -> set[tuple[str, str]]:
    doc = C.read_json(SPLITS_JSON, {}) or {}
    pairs = doc.get("pairs", []) if isinstance(doc, dict) else []
    out: set[tuple[str, str]] = set()
    for p in pairs:
        if isinstance(p, (list, tuple)) and len(p) == 2:
            out.add(tuple(sorted((str(p[0]), str(p[1])))))
    return out


def build_persons(entries: list[dict], races: dict[str, dict]) -> dict[str, I.Person]:
    persons: dict[str, I.Person] = {}
    for e in entries:
        r = races.get(e.get("race_uid", ""), {})
        date = r.get("race_date") or r.get("announce_date") or ""
        sample = C.race_title_zh(r.get("title", "")) or e.get("race_uid", "")
        for role, up, enp, zhp in (
            ("jockey", "jockey_uid", "jockey_en", "jockey_zh"),
            ("trainer", "trainer_uid", "trainer_en", "trainer_zh"),
        ):
            uid = (e.get(up) or "").strip()
            if not uid:
                continue
            p = persons.get(uid)
            if p is None:
                p = persons[uid] = I.Person(uid=uid, role=role)
            p.add((e.get(enp) or "").strip(), (e.get(zhp) or "").strip(),
                  e.get("race_uid", ""), date, sample)
    return persons


def absorb(dst: I.Person, src: I.Person) -> None:
    for k, v in src.names.items():
        dst.names[k] = dst.names.get(k, 0) + v
    for k, v in src.hks.items():
        dst.hks[k] = dst.hks.get(k, 0) + v
    dst.races |= src.races
    dst.first_date = min(dst.first_date, src.first_date)
    dst.last_date = max(dst.last_date, src.last_date)
    for s in src.sample:
        if len(dst.sample) < 3 and s not in dst.sample:
            dst.sample.append(s)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="S3b · 人物实体归一")
    ap.add_argument("--dry-run", action="store_true", help="只出报告，不改 entries.csv")
    a = ap.parse_args(argv)

    entries = C.read_csv(C.ENTRIES_CSV)
    races = {r["race_uid"]: r for r in C.read_csv(C.RACES_CSV)}
    if not entries:
        print("缺少 cache/entries.csv（先跑 python pipeline/s3_parse.py）")
        return 1

    persons = build_persons(entries, races)
    # 合并前留一份快照：合并后还要靠它知道「每个旧实体原来怎么写、跑过哪场」
    snap = {uid: (sorted(p.names, key=lambda x: (-p.names[x], x)), min(p.races) if p.races else "")
            for uid, p in persons.items()}
    print(f"解析出人物实体 {len(persons)}（骑手 {sum(1 for p in persons.values() if p.role == 'jockey')}"
          f" / 练马师 {sum(1 for p in persons.values() if p.role == 'trainer')}）")

    splits = load_splits()
    explicit = load_merges()
    old_to_new: dict[str, str] = {}
    merged_by: dict[str, str] = {}

    # ── 1. 人工确认的合并：归属写死在文件里，这里只做校验 ─────────────────
    for canonical, olds in explicit.items():
        if canonical not in persons:
            print(f"⚠ entity-merges.json 指向不存在的人物：{canonical}（跳过）")
            continue
        for old in olds:
            if old not in persons or old == canonical:
                print(f"⚠ 被并 uid 不存在：{old}（跳过）")
                continue
            if tuple(sorted((canonical, old))) in splits:
                print(f"⚠ {old} 同时出现在 person-splits.json 里，合并被否决（跳过）")
                continue
            old_to_new[old] = canonical
            merged_by[old] = "explicit"
            absorb(persons[canonical], persons.pop(old))

    # ── 2. 自动合并：同港译 + 同角色 + 拉丁名形态一致 ─────────────────────
    groups = I.group_persons(persons)
    auto_n = review_n = split_n = 0
    report: list[dict] = []
    for key, members in groups.items():
        v = I.judge(members, splits)
        item = {
            "key": key, "hk": members[0].hk, "role": members[0].role, "verdict": v.verdict,
            "reason": v.reason, "canonical": v.canonical,
            "members": [{
                "uid": p.uid, "name": p.name, "names": sorted(p.names, key=lambda x: -p.names[x]),
                "hk": p.hk, "races": len(p.races), "first": p.first_date, "last": p.last_date,
                "sample": p.sample,
            } for p in sorted(members, key=lambda p: p.uid)],
        }
        report.append(item)
        if v.verdict == "auto":
            auto_n += 1
            for p in members:
                if p.uid == v.canonical:
                    continue
                old_to_new[p.uid] = v.canonical
                merged_by[p.uid] = "auto"
                absorb(persons[v.canonical], persons.pop(p.uid))
        elif v.verdict == "split":
            split_n += 1
        else:
            review_n += 1

    print(f"同港译同角色分组 {len(groups)}：自动合并 {auto_n} 组 / 待人工 {review_n} 组 / 已否决 {split_n} 组")
    print(f"合计并掉 {len(old_to_new)} 条重复实体 → 人物 {len(persons)}")

    # ── 3. 拉丁写法清单：主名一条，其余作非主名行，搜索照样命中 ────────────
    def final_uid(uid: str) -> str:
        seen = set()
        while uid in old_to_new and uid not in seen:
            seen.add(uid)
            uid = old_to_new[uid]
        return uid

    variants: list[dict] = []
    seen_v: set[tuple[str, str]] = set()
    for uid, (names, race_uid) in snap.items():
        fin = final_uid(uid)
        if fin not in persons:
            continue
        own = snap.get(fin, ([], ""))[0]
        primary = own[0] if own else (names[0] if names else "")
        for nm in names:
            if not nm:
                continue
            is_primary = "1" if nm == primary else "0"
            key = (fin, C.norm_key(nm), is_primary)
            if key in seen_v:
                continue
            seen_v.add(key)
            variants.append({"entity_uid": fin, "role": persons[fin].role, "value": nm,
                             "is_primary": is_primary, "race_uid": race_uid})

    # ── 4. 写盘 ────────────────────────────────────────────────────────────
    if not a.dry_run:
        pinned = sum(1 for e in entries for up in ("jockey_uid", "trainer_uid")
                     if (e.get(up) or "").strip() in old_to_new)
        for e in entries:
            for up in ("jockey_uid", "trainer_uid"):
                uid = (e.get(up) or "").strip()
                if uid and uid in old_to_new:
                    e[up] = old_to_new[uid]
        C.write_csv(C.ENTRIES_CSV, entries, list(entries[0].keys()))
        print(f"改写 entries.csv 人物引用 {pinned} 处")

    C.write_csv(C.CACHE / "identity_map.csv", [
        {"old_uid": o, "new_uid": n, "role": persons[n].role, "hk": persons[n].hk,
         "verdict": merged_by.get(o, "auto"), "reason": ""}
        for o, n in sorted(old_to_new.items()) if n in persons
    ], MAP_FIELDS)

    C.write_csv(C.CACHE / "identity_variants.csv", variants, VARIANT_FIELDS)

    C.write_csv(C.CACHE / "review_identity.csv", [{
        "kind": "merge", "entity_type": "person", "entity_uid": r["canonical"],
        "payload": json.dumps({
            "hk": r["hk"], "role": r["role"], "reason": r["reason"],
            "members": [{"uid": m["uid"], "name": m["name"]} for m in r["members"]],
        }, ensure_ascii=False),
        "reason": r["reason"], "status": "open", "created_at": C.now_iso(), "resolved_at": "",
    } for r in report if r["verdict"] == "review"], REVIEW_FIELDS)

    out = C.PUBLIC_DATA.parent / "review"
    out.mkdir(parents=True, exist_ok=True)
    C.write_json(out / "merge.json", {
        "_meta": {
            "schema": "merge-review/v1", "updated": C.today(),
            "persons": len(persons), "groups": len(report),
            "auto": auto_n, "review": review_n, "split": split_n,
            "note": "s3b_identity.py 生成；判定结果写回 data-src/entity-merges.json 与 person-splits.json",
        },
        "groups": sorted(report, key=lambda r: (r["verdict"] != "review", -len(r["members"]))),
    }, indent=1)

    print(f"复核页数据 → {out / 'merge.json'}（{review_n} 组待判 / {auto_n} 组已自动合并）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
