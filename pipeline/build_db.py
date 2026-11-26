#!/usr/bin/env python3
"""按 db/schema.sql 重建 cache/maming.sqlite，并把 S3 / S4 的 CSV 灌进去。

表结构唯一出处是 db/schema.sql —— 改字段先改它，再跑本脚本重建，**不要手改 sqlite**。
本脚本只做「建表 + 灌 CSV」，不含任何业务判断（判断都在 S3 / S4）。

CSV → 表
  cache/races.csv        → race
  cache/horses.csv       → horse
  cache/persons.csv      → person
  cache/names.csv        → name
  cache/aliases.csv      → entity_alias
  cache/entries.csv      → entry
  cache/xid.csv          → xid
  cache/archive.csv      → archive（S2b 产物，没有就空表）
  cache/review_queue.csv → review_queue
  cache/override_apply.csv → override_apply

用法：python pipeline/build_db.py [--keep]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common as C


def as_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def as_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def as_text(v):
    v = (v or "").strip()
    return v or None


def load(conn: sqlite3.Connection, table: str, rows: list[dict], mapping: dict[str, tuple[str, callable]] | None = None):
    """mapping: 目标列 → (CSV 列, 转换函数)；不传则同名直灌。"""
    if not rows:
        return 0
    cols = list(mapping) if mapping else [k for k in rows[0]]
    sql = f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
    values = []
    for r in rows:
        if mapping:
            values.append(tuple(fn(r.get(src)) for src, fn in mapping.values()))
        else:
            values.append(tuple(as_text(r.get(c)) for c in cols))
    conn.executemany(sql, values)
    return len(values)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="重建 sqlite 工作库")
    ap.add_argument("--keep", action="store_true", help="不删旧库（默认重建，避免残留旧行）")
    a = ap.parse_args(argv)

    races = C.read_csv(C.RACES_CSV)
    entries = C.read_csv(C.ENTRIES_CSV)
    if not races:
        print("缺少 cache/races.csv（先跑 python pipeline/s3_parse.py）")
        return 1

    if C.SQLITE.exists() and not a.keep:
        C.SQLITE.unlink()

    conn = sqlite3.connect(C.SQLITE)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript((C.DB_DIR / "schema.sql").read_text(encoding="utf-8"))

    n_race = load(conn, "race", races, {
        "race_uid": ("race_uid", as_text),
        "announce_date": ("announce_date", lambda v: as_text(v) or ""),
        "race_date": ("race_date", as_text),
        "race_time": ("race_time", as_text),
        "session": ("session", as_text),
        "grade": ("grade", as_text),
        "grade_cn": ("grade_cn", as_text),
        "venue": ("venue", as_text),
        "venue_country": ("venue_country", as_text),
        "course": ("course", as_text),
        "distance": ("distance", as_text),
        "surface": ("surface", as_text),
        "first_seen_at": ("parsed_at", lambda v: as_text(v) or C.now_iso()),
    })
    n_horse = load(conn, "horse", C.read_csv(C.CACHE / "horses.csv"), {
        "horse_uid": ("horse_uid", as_text),
        "birth_year": ("birth_year", as_int),
        "birth_date": ("birth_date", as_text),
        "sex": ("sex", as_text),
        "color": ("color", as_text),
        "country_code": ("country_code", as_text),
        "origin_basis": ("origin_basis", as_text),
        "status": ("status", as_text),
        "curation": ("curation", lambda v: as_text(v) or "pending"),
    })
    # JBIS 基本情報比公告更权威也更细（生年月日 / 毛色 / 性別），
    # 有就覆盖公告推出来的周年级字段——这是数据来源优先级，不是业务判断。
    prof = C.read_csv(C.CACHE / "jbis_profile.csv")
    n_prof = 0
    for p in prof:
        sex = {"牡": "M", "牝": "F", "騸": "G"}.get((p.get("sex_jp") or "")[:1], "")
        conn.execute(
            """UPDATE horse SET birth_date = COALESCE(NULLIF(?, ''), birth_date),
                                color      = COALESCE(NULLIF(?, ''), color),
                                sex        = COALESCE(NULLIF(?, ''), sex),
                                birth_year = COALESCE(?, birth_year)
                WHERE horse_uid = ?""",
            (p.get("birth_date", ""), p.get("color", ""), sex,
             as_int((p.get("birth_date") or "")[:4]), p["horse_uid"]),
        )
        n_prof += 1
    n_ped = load(conn, "pedigree_node", C.read_csv(C.CACHE / "pedigree.csv"), {
        "horse_uid": ("horse_uid", as_text),
        "side": ("side", as_text),
        "depth": ("depth", as_int),
        "idx": ("idx", as_int),
        "name": ("name", as_text),
        "sex": ("sex", as_text),
        "birth_year": ("birth_year", as_int),
        "color": ("color", as_text),
        "jbis_id": ("jbis_id", as_text),
    })
    n_pedmeta = 0
    for jb in C.read_csv(C.JBIS_CSV):
        if not jb.get("jbis_id"):
            continue
        ped_json = C.read_json(C.CACHE / "pedigree" / f"{jb['jbis_id']}.json", {}) or {}
        # 只登记真的抓到了血统页的马：只有 id 没有格子的不进 meta，
        # 否则页面上会出现「血统已接入但整张图空白」的假象
        if not ped_json.get("tree"):
            continue
        conn.execute(
            """INSERT OR REPLACE INTO pedigree_meta (horse_uid, jbis_id, fno, cross, source_url, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (jb["horse_uid"], jb["jbis_id"], ped_json.get("fno") or "", ped_json.get("cross") or "",
             f"https://www.jbis.or.jp/horse/{jb['jbis_id']}/pedigree/",
             jb.get("fetched_at") or C.now_iso()),
        )
        n_pedmeta += 1
    n_person = load(conn, "person", C.read_csv(C.CACHE / "persons.csv"), {
        "person_uid": ("person_uid", as_text),
        "role": ("role", lambda v: as_text(v) or "jockey"),
        "country_code": ("country_code", as_text),
        "nationality_basis": ("nationality_basis", as_text),
    })
    n_name = load(conn, "name", C.read_csv(C.NAMES_CSV), {
        "entity_type": ("entity_type", as_text),
        "entity_uid": ("entity_uid", as_text),
        "field": ("field", as_text),
        "lang": ("lang", lambda v: as_text(v) or "zh-Hant"),
        "value": ("value", as_text),
        "is_primary": ("is_primary", lambda v: as_int(v) or 0),
        "source_url": ("source_url", lambda v: as_text(v) or ""),
        "archive_url": ("archive_url", as_text),
        "decided_at": ("decided_at", lambda v: as_text(v) or C.today()),
    })
    n_alias = load(conn, "entity_alias", C.read_csv(C.ALIASES_CSV), {
        "entity_type": ("entity_type", as_text),
        "entity_uid": ("entity_uid", as_text),
        "seq": ("seq", lambda v: as_int(v) or 0),
        "text": ("text", as_text),
        "note": ("note", as_text),
        "updated_at": ("updated_at", lambda v: as_text(v) or C.today()),
    })
    n_entry = load(conn, "entry", entries, {
        "race_uid": ("race_uid", as_text),
        "horse_uid": ("horse_uid", as_text),
        "saddle_no": ("saddle_no", as_int),
        "draw": ("draw", as_int),
        "jockey_uid": ("jockey_uid", as_text),
        "trainer_uid": ("trainer_uid", as_text),
        "weight_lb": ("lb", as_float),
        "claim_lb": ("claim_lb", as_float),
        "intl_rating": ("rating", as_int),
        "gear": ("gear", as_text),
        "sex": ("sex", as_text),
        "age": ("age", as_int),
        "finish": ("finish", as_int),
        "scraped_from": ("scraped_from", as_text),
        "doc_url": ("doc_url", as_text),
    })
    n_xid = load(conn, "xid", C.read_csv(C.CACHE / "xid.csv"))
    # S4b 也会补出新的 JBIS id（S4 跑完之后才命中的那批），别让 xid 表落后于 jbis.csv
    xid_extra = 0
    for jb in C.read_csv(C.JBIS_CSV):
        if not jb.get("jbis_id"):
            continue
        conn.execute(
            "INSERT OR REPLACE INTO xid (entity_type, entity_uid, system, external_id, confidence) "
            "VALUES ('horse', ?, 'jbis', ?, ?)",
            (jb["horse_uid"], jb["jbis_id"], 1.0 if jb.get("status") == "ok" else 0.6),
        )
        xid_extra += 1
    n_archive = load(conn, "archive", C.read_csv(C.ARCHIVE_CSV))
    n_review = load(conn, "review_queue", C.read_csv(C.CACHE / "review_queue.csv"))
    n_apply = load(conn, "override_apply", C.read_csv(C.CACHE / "override_apply.csv"))

    conn.commit()
    bad = conn.execute("PRAGMA foreign_key_check").fetchall()
    conn.close()

    print(f"race {n_race} · horse {n_horse} · person {n_person} · name {n_name} · alias {n_alias}")
    print(f"entry {n_entry} · xid {n_xid} · archive {n_archive} · review {n_review} · apply {n_apply}")
    print(f"JBIS 基本情報 {n_prof} · 血统格 {n_ped} · 血统页 {n_pedmeta} · jbis id {xid_extra}")
    print(f"→ {C.SQLITE}")
    if bad:
        print(f"⚠ 外键异常 {len(bad)} 条（多半是 entry 指向了不存在的马/赛事）")
        for b in bad[:5]:
            print("  ", b)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
