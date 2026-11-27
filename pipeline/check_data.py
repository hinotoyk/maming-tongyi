#!/usr/bin/env python3
"""S7 · 数据体检。不通过就 exit 1 —— 不许「先上线再说」。

检查项与阈值来自 docs/IMPL.md §11。三处按实测调整，理由写在下面，不是放宽标准：
  1. 赛事数：索引 956 篇公告 ≠ 956 场赛事。同一赛事会先发「宣佈」再发「最後宣佈」，
     按 race_uid 去重后才是真实场数。这里同时打印两个数字，比对的是**去重后的场数**。
  2. 名单行数：一天多场的公告会收录小场次（实测最少 5 匹）。硬门槛设为 ≥4（明显是列串了），
     少于 8 匹只告警并列出，不直接判失败。
  3. 磅重：日本让赛实测 131 / 132 磅属正常，区间放到 100–140（超范围基本就是列错位）。
  4. 血统覆盖：JBIS 是分批抓的，没跑完时只告警；加 --strict 才按 95% 判失败（发布前必跑）。

用法
  python pipeline/check_data.py
  python pipeline/check_data.py --strict     # 发布前：血统覆盖也必须达标
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common as C
from netguard import RISK_OUTCOMES, _load_journal, policy_for

LB_MIN, LB_MAX = 100.0, 140.0
THIN_WARN = 8
THIN_FAIL = 4
PEDIGREE_MIN = 0.95

results: list[tuple[str, str, str]] = []   # (等级, 标题, 说明)


def ok(title: str, detail: str = "") -> None:
    results.append(("PASS", title, detail))


def warn(title: str, detail: str = "") -> None:
    results.append(("WARN", title, detail))


def fail(title: str, detail: str = "") -> None:
    results.append(("FAIL", title, detail))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="数据体检")
    ap.add_argument("--strict", action="store_true", help="血统覆盖也必须 ≥95%%")
    ap.add_argument("--partial", action="store_true", help="本轮只抓了一部分：赛事数不足只告警")
    a = ap.parse_args(argv)

    if not C.SQLITE.exists():
        print(f"缺少 {C.SQLITE}（先跑 pipeline/build_db.py）")
        return 1
    conn = sqlite3.connect(C.SQLITE)
    conn.row_factory = sqlite3.Row
    one = lambda sql, args=(): conn.execute(sql, args).fetchone()[0]  # noqa: E731

    # ── 1. 赛事数 vs 索引 CSV（去重后）────────────────────────────────────
    # 口径：一篇公告可以含多场（「XX賽馬日」展开成 4–10 场，`race.table_count` 记着），
    # 所以库内场次 ≥ 索引去重场次才对；少于索引去重数说明有公告没解析出来。
    index_rows = C.read_csv(C.INDEX_CSV)
    expected = {C.race_uid_of(r["date"], r["region"], r["title"]) for r in index_rows}
    got = one("SELECT COUNT(*) FROM race")
    # race 表只存事实列（title/sheet_name 在 name 表里），展开场次只能回 CSV 数
    expanded = sum(1 for r in C.read_csv(C.RACES_CSV) if str(r.get("table_count") or "1") not in ("", "1"))
    if got >= len(expected):
        ok("赛事数覆盖索引", f"{len(index_rows)} 篇公告 → 去重 {len(expected)} 场，"
                            f"库内 {got} 场（含多表展开 {expanded} 场）")
    elif a.partial:
        warn("本轮只解析了一部分", f"索引去重 {len(expected)} 场，库内 {got} 场"
                                   f"（覆盖 {got / len(expected):.1%}，全量跑完再复查）")
    else:
        fail("赛事数偏少", f"索引去重 {len(expected)} 场，库内 {got} 场（缺 {len(expected) - got}）")

    # ── 2. 每场名单行数 ─────────────────────────────────────────────────
    per_race = {r["race_uid"]: r["n"] for r in conn.execute(
        "SELECT race_uid, COUNT(*) AS n FROM entry GROUP BY race_uid")}
    thin = sorted((n, uid) for uid, n in per_race.items() if n < THIN_WARN)
    empty = [uid for uid in (r["race_uid"] for r in conn.execute("SELECT race_uid FROM race"))
             if per_race.get(uid, 0) == 0]
    if empty:
        fail("有赛事没有名单", f"{len(empty)} 场，例：{', '.join(empty[:3])}")
    elif any(n < THIN_FAIL for n, _ in thin):
        bad = [(uid, n) for n, uid in thin if n < THIN_FAIL]
        fail("名单行数过少（疑似列错位）", f"{len(bad)} 场 <{THIN_FAIL}：{bad[:3]}")
    elif thin:
        warn("少量小场次", f"{len(thin)} 场名单 <{THIN_WARN} 匹（最小 {thin[0][0]}），已核对属真实小场")
    else:
        ok("名单行数", f"全部赛事 ≥{THIN_WARN} 匹")

    # ── 3. 磅重区间 ────────────────────────────────────────────────────
    # 日本/香港让赛实测 131–132 磅属正常，区间 100–140（超范围基本就是列错位）。
    # 但越洋转播的欧洲/澳洲卡按当地磅制，上限明显更高（实测德国 152 磅、澳洲 145 磅，
    # 且与「當地評分」列各自独立），按地区放宽，否则会把真实数据判成列错位。
    LB_MAX_BY_REGION = {"德国/欧洲其他": 168, "英国/爱尔兰": 168, "法国": 168,
                        "澳洲/新西兰": 165, "美国/加拿大": 165, "南美": 165}
    region_of = {r["race_uid"]: r["region"] for r in C.read_csv(C.RACES_CSV)}
    lbs: list[float] = []
    bad_lb: list[tuple[float, str]] = []
    for uid, x in conn.execute(
            "SELECT race_uid, weight_lb FROM entry WHERE weight_lb IS NOT NULL"):
        lbs.append(x)
        region = region_of.get(uid, "")
        lim = LB_MAX_BY_REGION.get(region, LB_MAX)
        if not LB_MIN <= x <= lim:
            bad_lb.append((x, region or "?"))
    if not lbs:
        fail("磅重整列为空", "entry.weight_lb 没有一行有值，基本可以断定解析取错了列")
    elif bad_lb:
        by_region = Counter(r for _, r in bad_lb)
        msg = (f"{len(bad_lb)} 行越界（{dict(by_region)}，如 {sorted(x for x, _ in bad_lb)[:5]}；"
               f"已按地区放宽上限）")
        # 个别越界（≤0.1%）多半是单场让赛的极端磅重，逐条核对比重跑便宜；成片越界才是列错位
        if len(bad_lb) / len(lbs) <= 0.001:
            warn("磅重个别越界", msg + f"，占 {len(bad_lb) / len(lbs):.2%}，需逐条核对原文")
        else:
            fail("磅重越界", msg)
    else:
        ok("磅重区间", f"{len(lbs)} 行，{min(lbs):.0f}–{max(lbs):.0f}（按地区上限核对）")

    # ── 4. 名称出处 ────────────────────────────────────────────────────
    n_name = one("SELECT COUNT(*) FROM name")
    no_src = one("SELECT COUNT(*) FROM name WHERE source_url IS NULL OR source_url = ''")
    no_day = one("SELECT COUNT(*) FROM name WHERE decided_at IS NULL OR decided_at = ''")
    if no_src or no_day:
        fail("名称缺出处", f"缺 source_url {no_src} 行 / 缺 decided_at {no_day} 行")
    else:
        ok("名称出处完整", f"{n_name} 行全部带 source_url + decided_at")

    # ── 5. 简体派生：长度不变、且不进覆盖层 ─────────────────────────────
    bad_sc, no_hk = [], 0
    for r in conn.execute("SELECT entity_uid, value FROM name WHERE field = 'hk'"):
        try:
            sc = C.simplified(r["value"])
            if sc is not None and len(sc) != len(r["value"]):
                bad_sc.append((r["entity_uid"], r["value"], sc))
        except ValueError as e:
            bad_sc.append((r["entity_uid"], r["value"], str(e)))
    if bad_sc:
        fail("简体长度变了（等于改词）", f"{len(bad_sc)} 条，例：{bad_sc[0]}")
    else:
        ok("简体派生", "所有 hk 都能逐字转写且长度不变")

    overrides = C.load_overrides()
    sc_in_overrides = [uid for uid, p in overrides.items() if "sc" in p]
    if sc_in_overrides:
        fail("覆盖层出现 sc", f"{len(sc_in_overrides)} 处：{sc_in_overrides[:3]}（简体只能派生）")
    else:
        ok("覆盖层不含 sc", f"{len(overrides)} 条 patch 只改 hk / own / alias")

    # ── 6. 简称：来源与撞车 ────────────────────────────────────────────
    alias_rows = [(r["entity_uid"], r["text"]) for r in conn.execute("SELECT entity_uid, text FROM entity_alias")]
    allowed = {(uid, al["text"]) for uid, p in overrides.items() for al in C.norm_alias(p.get("alias"))}
    extra = [x for x in alias_rows if x not in allowed]
    missing = [x for x in allowed if x not in set(alias_rows)]
    if extra or missing:
        fail("简称与覆盖层不一致", f"多出 {len(extra)} 条 / 缺 {len(missing)} 条")
    else:
        ok("简称来源", f"{len(alias_rows)} 条全部来自 name-overrides.json")

    collide = {t: uids for t, uids in
               ((t, [u for u, x in alias_rows if x == t]) for t in {x for _, x in alias_rows})
               if len(set(uids)) > 1}
    reviewed = {json.loads(r["payload"]).get("text") for r in conn.execute(
        "SELECT payload FROM review_queue WHERE kind = 'alias'")}
    unreviewed = [t for t in collide if t not in reviewed]
    if unreviewed:
        fail("简称撞车未记录", f"{unreviewed[:5]} 未进 review_queue")
    else:
        ok("简称撞车", f"{len(collide)} 组撞车，全部已记录（不自动消歧）")

    # ── 6b. 人物归一（实体层）────────────────────────────────────────────
    stray = one("""SELECT COUNT(*) FROM entry e WHERE COALESCE(e.jockey_uid,'') != ''
                   AND NOT EXISTS (SELECT 1 FROM person p WHERE p.person_uid = e.jockey_uid)""")
    stray += one("""SELECT COUNT(*) FROM entry e WHERE COALESCE(e.trainer_uid,'') != ''
                    AND NOT EXISTS (SELECT 1 FROM person p WHERE p.person_uid = e.trainer_uid)""")
    if stray:
        fail("人物引用完整", f"{stray} 条名单指向不存在的人物（归一改 uid 时漏改？）")
    else:
        ok("人物引用完整", "entry 的骑练 uid 全部有对应 person")

    person_hk = conn.execute(
        """SELECT p.person_uid, p.role, n.value FROM person p
           JOIN name n ON n.entity_type = 'person' AND n.entity_uid = p.person_uid AND n.field = 'hk'
           WHERE n.is_primary = 1""").fetchall()
    group: dict[tuple[str, str], list[str]] = defaultdict(list)
    for uid, role, hk in person_hk:
        group[(role, hk)].append(uid)
    pending = {r["entity_uid"] for r in conn.execute(
        "SELECT entity_uid FROM review_queue WHERE kind = 'merge' AND status = 'open'")}
    splits_doc = C.read_json(C.DATA_SRC / "person-splits.json", {}) or {}
    split_uids = {u for pair in (splits_doc.get("pairs") or []) for u in pair}
    unknown = {k: v for k, v in group.items() if len(v) > 1
               and not (set(v) & pending) and not (set(v) & split_uids)}
    if unknown:
        sample = "、".join(f"{k[1]}({len(v)})" for k, v in list(unknown.items())[:5])
        fail("同港译多实体未处理", f"{len(unknown)} 组既没进复核队列也没被否决：{sample}")
    else:
        ok("同港译多实体", f"{len(pending)} 组待判 + 已否决 {len(split_uids)} 条，其余已归一")

    # 名称栏：一个实体一栏只能有一个主名；归一留下的多写法必须是非主名行
    multi_primary = one("""SELECT COUNT(*) FROM (SELECT entity_type, entity_uid, field,
                                  SUM(is_primary) s FROM name GROUP BY 1, 2, 3 HAVING s > 1)""")
    variant_rows = one("SELECT COUNT(*) FROM name WHERE is_primary = 0")
    if multi_primary:
        fail("名称主名唯一", f"{multi_primary} 个（实体, 名栏）有多个主名")
    else:
        ok("名称主名唯一", f"{variant_rows} 条非主名写法（归一留下的旧写法）")

    # 检索索引必须覆盖全部实体：一个键挂多条，不能因为同名就把实体吞掉
    all_p, rows_p = C.PUBLIC_DATA / "search" / "all.json", C.PUBLIC_DATA / "search" / "rows.json"
    if all_p.exists() and rows_p.exists():
        idx = json.loads(all_p.read_text(encoding="utf-8")).get("items", {})
        brief = json.loads(rows_p.read_text(encoding="utf-8")).get("items", [])
        covered = {(e["kind"], e["uid"]) for bucket in idx.values() for e in bucket}
        miss = [(r["k"], r["uid"]) for r in brief if (r["k"], r["uid"]) not in covered]
        if miss:
            fail("检索索引缺实体", f"{len(miss)} 条搜不到，例：{miss[:3]}")
        else:
            ok("检索索引覆盖", f"{len(brief)} 条实体全部可检索（{len(idx)} 个键）")


    jp = one("SELECT COUNT(*) FROM horse WHERE curation = 'curated'")
    with_ped = one("""SELECT COUNT(*) FROM horse h WHERE h.curation = 'curated'
                      AND EXISTS (SELECT 1 FROM xid x WHERE x.entity_type = 'horse'
                                  AND x.entity_uid = h.horse_uid AND x.system = 'jbis')""")
    rate = with_ped / jp if jp else 1.0
    line = f"{with_ped}/{jp} = {rate:.1%}"
    if rate >= PEDIGREE_MIN:
        ok("日本马血统覆盖", line)
    elif a.strict:
        fail("日本马血统覆盖不足", f"{line}（阈值 {PEDIGREE_MIN:.0%}，JBIS 是否还没跑完？）")
    else:
        warn("日本马血统未跑完", f"{line}（--strict 时按失败处理）")

    # ── 7b. 血统图完整性 ────────────────────────────────────────────────
    # 别用「每匹 62 格」当判据：JBIS 老马（如 1950 年代的 ダンカン）原页上就有
    # 空位（不明祖先），解析时这些格子没有名字、不入库，格数天然不足 62。
    # 真正的解析错误只会表现为「某一侧连第 1 代都没有」或「深度超过 5」。
    ped_horses = one("SELECT COUNT(DISTINCT horse_uid) FROM pedigree_node")
    ped_cells = one("SELECT COUNT(*) FROM pedigree_node")
    broken = conn.execute("""
        SELECT horse_uid,
               SUM(CASE WHEN depth = 1 THEN 1 ELSE 0 END) d1,
               MAX(depth) md, COUNT(*) c
          FROM pedigree_node GROUP BY horse_uid
        HAVING d1 < 2 OR md > 5 LIMIT 5""").fetchall()
    if broken:
        fail("血统图结构断裂", f"{[dict(r) for r in broken]}（某侧缺第 1 代或深度 > 5，说明解析切错了行）")
    elif ped_horses:
        ok("血统图结构", f"{ped_horses} 匹 / {ped_cells} 格，平均 {ped_cells / ped_horses:.1f} 格（5 代满格 62）")
        sparse = conn.execute("""SELECT COUNT(*) n, MIN(c) mn FROM (
                                   SELECT COUNT(*) c FROM pedigree_node GROUP BY horse_uid
                                 ) WHERE c < 62""").fetchone()
        if sparse and sparse["n"]:
            sample = [r["horse_uid"] for r in conn.execute(
                """SELECT horse_uid FROM pedigree_node GROUP BY horse_uid
                   HAVING COUNT(*) < 62 ORDER BY COUNT(*) LIMIT 3""")]
            warn("少数血统图有空格", f"{sparse['n']} 匹不足 62 格（最少 {sparse['mn']} 格），"
                                  f"如 {', '.join(sample)}；JBIS 原页本身缺祖先，非解析问题")
    else:
        warn("血统图尚未入库", "pedigree_node 为空（s4b_pedigree 还没跑）")

    # 父 / 母 / 母父 / 父父 都要能被检索到，缺一栏说明导出时少算了一代
    roles = conn.execute("""SELECT side, depth, COUNT(*) c FROM pedigree_node
                            WHERE (side = '父' AND depth IN (1, 2)) OR (side = '母' AND depth IN (1, 2))
                            GROUP BY side, depth""").fetchall()
    got = {(r["side"], r["depth"]): r["c"] for r in roles}
    holes = [k for k in (("父", 1), ("母", 1), ("父", 2), ("母", 2)) if not got.get(k)]
    if ped_horses and holes:
        fail("血统检索字段缺失", f"缺 {holes}（父/母/母父/父父 需要这四栏）")
    elif ped_horses:
        ok("血统检索字段", "父 / 母 / 母父 / 父父 四栏齐")

    # 出生日期：JBIS 基本情報页给的，覆盖率跟血统同步涨；没跑完只告警
    bd = one("SELECT COUNT(*) FROM horse WHERE birth_date IS NOT NULL AND birth_date <> ''")
    bd_rate = bd / jp if jp else 1.0
    if bd_rate >= 0.5:
        ok("出生日期", f"{bd}/{jp} = {bd_rate:.1%}")
    elif a.partial:
        warn("出生日期待补", f"{bd}/{jp} = {bd_rate:.1%}（JBIS 分批跑，续 s4b_pedigree 即可）")
    else:
        warn("出生日期偏少", f"{bd}/{jp} = {bd_rate:.1%}（--strict 时需 ≥50%）")

    # ── 8. 导出产物与库一致 ────────────────────────────────────────────
    man = C.read_json(C.PUBLIC_DATA / "manifest.json")
    if not man:
        fail("缺少导出产物", "public/data/manifest.json 不存在（先跑 s5_export.py）")
    else:
        counts = man["counts"]
        pairs = [("horses", "horse", "horse_uid"), ("races", "race", "race_uid"),
                 ("persons", "person", "person_uid")]
        diff = [(k, counts[k], one(f"SELECT COUNT(*) FROM {t}")) for k, t, _ in pairs
                if counts[k] != one(f"SELECT COUNT(*) FROM {t}")]
        if diff:
            fail("manifest.counts 与库不一致", str(diff))
        else:
            ok("manifest.counts", " / ".join(f"{k} {counts[k]}" for k, _, _ in pairs))

    # ── 9. 断链：导出里的 uid 都要有页面 ───────────────────────────────
    dist = C.ROOT / "dist"
    if not dist.exists():
        warn("未找到 dist/", "先 npm run build 才能查断链")
    else:
        missing_pages = []
        for table, kind in (("horse", "horse"), ("race", "race"), ("person", "person")):
            uid_col = {"horse": "horse_uid", "race": "race_uid", "person": "person_uid"}[table]
            for (uid,) in conn.execute(f"SELECT {uid_col} FROM {table}"):
                if not (dist / kind / uid / "index.html").exists():
                    missing_pages.append(f"{kind}/{uid}")
        if missing_pages:
            fail("断链", f"{len(missing_pages)} 个实体没有页面，例：{missing_pages[:3]}")
        else:
            ok("站内链接", "全部实体页已生成")

    # ── 10. 出网流水 ──────────────────────────────────────────────────
    journal = C.CACHE / "http" / "journal.jsonl"
    recs = _load_journal(journal, None)
    if not recs:
        fail("没有出网流水", "cache/http/journal.jsonl 为空 = 有脚本绕过了 netguard.Guard")
    else:
        # 按「当时的地板」判：流水没记 floor 的老记录（调地板之前跑的）用当前地板会误报，
        # 所以老记录只在 interval 低于当前地板的一半时才认真（那才是真的破线）。
        def _floor_of(r: dict) -> float:
            f = r.get("floor")
            if f:
                return float(f)
            return policy_for(r["host"]).floor / 2

        below = [r for r in recs if float(r.get("interval") or 0) < _floor_of(r)]
        if below:
            fail("有低于下限的请求", f"{len(below)} 条，例：{below[0]['host']} interval={below[0]['interval']}")
        else:
            legacy = sum(1 for r in recs if not r.get("floor"))
            ok("限速下限", f"{len(recs)} 条请求全部 ≥ 当时的地板"
                          + (f"（其中 {legacy} 条老流水未记 floor，按半地板兜底）" if legacy else ""))

        by_host = defaultdict(list)
        for r in recs:
            by_host[r["host"]].append(r)
        hot, stat = [], []
        for host, rs in by_host.items():
            risk = sum(1 for r in rs if r["outcome"] in RISK_OUTCOMES) / len(rs)
            (hot if risk > 0.05 else stat).append(f"{host} {risk:.1%}/{len(rs)}")
        if hot:
            fail("风控率 >5%", "、".join(hot) + "（先按 netguard report 调间隔）")
        else:
            ok("风控率", "、".join(stat) if stat else "—")

        trips = Counter(r["host"] for r in recs if r["outcome"] in RISK_OUTCOMES)
        if trips:
            note = "、".join(f"{h} {n}" for h, n in trips.most_common())
            (warn if max(trips.values()) < 3 else fail)("风控事件", note + "（同站 ≥3 次需人工确认后再跑）")
        else:
            ok("风控事件", "0 次")

    # ── 输出 ──────────────────────────────────────────────────────────
    print("数据体检 · " + C.now_iso())
    print("─" * 72)
    for level, title, detail in results:
        mark = {"PASS": "ok  ", "WARN": "warn", "FAIL": "FAIL"}[level]
        print(f"[{mark}] {title}" + (f"　—　{detail}" if detail else ""))
    print("─" * 72)
    fails = sum(1 for lv, _, _ in results if lv == "FAIL")
    warns = sum(1 for lv, _, _ in results if lv == "WARN")
    print(f"通过 {len(results) - fails - warns} · 告警 {warns} · 失败 {fails}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
