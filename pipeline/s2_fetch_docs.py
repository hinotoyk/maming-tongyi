#!/usr/bin/env python3
"""S2 · 抓公告正文与附件 → cache/docs/{race_uid}/

输入  data-src/hkjc_entries_index.csv（S1 产出，956 条）
输出  cache/docs/{race_uid}/article.html、attach.docx|attach.doc、meta.json
      cache/docs/manifest.csv（全量清单，断点续抓与 S3 都读它）

必须处理的三件事（docs/IMPL.md §5，都是实测坑）：
  1. 协议相对链接 `//res.hkjc.com/...`：直接拼 racingnews 会 500，统一补 https:，
     主机白名单只认 res / corporate / racing.hkjc.com。
  2. 两种扩展名：.docx 是 ZIP（504b0304），.doc 是 OLE2（d0cf11e0），S3 分别处理。
  3. 正文形态三种：docx 附件 / doc 附件 / 内联 HTML 表格。三种都要留标记，
     只抓附件会漏掉内联那 1/3。

出网一律走 netguard.Guard（AGENTS.md §2.9）：串行、单线程、间隔只因风控上调。
撞 RiskControlStop → exit 2，已抓完的目录天然就是检查点。

用法
  python pipeline/s2_fetch_docs.py --from 0 --to 50        # 抓前 50 条
  python pipeline/s2_fetch_docs.py --region 日本           # 只跑日本队列（不影响抓取范围口径）
  python pipeline/s2_fetch_docs.py --only-failed            # 重跑上一批失败的
  python pipeline/netguard.py report                        # 跑完必做
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common as C
from netguard import Guard, RiskControlStop

ALLOWED_ATTACH_HOSTS = {"res.hkjc.com", "corporate.hkjc.com", "racing.hkjc.com"}
ATTACH_RE = re.compile(r"\.(docx?|pdf|xlsx?)(?:$|[?#])", re.I)
# 引号必须按「定界符」匹配：附件名里会出现撇号
# （`20211002_Prixdel'ArcdeTriompheDay_FinalDecl_OC_C.docx`），
# 老写法 [^"']+ 会在撇号处截断，整条链接被静默丢弃。
HREF_RE = re.compile(r"""href\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)


def hrefs(html: str) -> list[str]:
    """取出全部 href 值（双引号/单引号两种定界符都认，值内部允许另一种引号）。"""
    return [m.group(1) if m.group(1) is not None else m.group(2)
            for m in HREF_RE.finditer(html or "")]
OFFICE_EXT = {"docx", "doc"}
MANIFEST_FIELDS = [
    "race_uid", "announce_date", "region", "title", "article_url",
    "article_status", "article_http", "article_bytes", "article_sha256",
    "attach_url", "attach_kind", "attach_status", "attach_http", "attach_bytes", "attach_sha256",
    "inline_table", "source_kind", "status", "fetched_at", "error",
]


def resolve_url(href: str, base: str) -> str:
    """协议相对（//host/...）与站内相对（/path）都补成 https 绝对地址。

    实测：2018–2022 的部分公告把附件地址写在 `<a>` 里时带了换行
    （`//corporate.hkjc.com/image/202211/\n\n20221111_...DOC`），
    curl 会直接 exit 3（URL malformed）。所以先把 href 里的空白全部清掉。
    """
    href = re.sub(r"\s+", "", href or "")
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return "https://" + urlsplit(base).netloc + href
    if href.startswith("http://"):
        return "https://" + href[len("http://"):]
    return urljoin(base, href)


def find_attachments(html: str, article_url: str) -> tuple[list[dict], list[dict]]:
    """正文里的名单附件 → (可用附件, 被跳过的 [{url, reason}])。

    只认白名单主机（白名单外不抓，避免替别人打流量）与 office 扩展名
    （pdf/xlsx 解析不出名单，只留痕不下载）。被跳过的一律记下原因，
    否则「只有 PDF」会被误报成「主机不在白名单」。
    """
    found: list[dict] = []
    skipped: list[dict] = []
    seen: set[str] = set()
    for href in hrefs(html):
        if not ATTACH_RE.search(href):
            continue
        url = resolve_url(href, article_url)
        if url in seen:
            continue
        seen.add(url)
        host = urlsplit(url).netloc.lower()
        ext = ATTACH_RE.search(url).group(1).lower()
        if host not in ALLOWED_ATTACH_HOSTS:
            skipped.append({"url": url, "reason": "host"})
            continue
        if ext not in OFFICE_EXT:
            skipped.append({"url": url, "reason": "ext"})      # pdf/xlsx 解析不了，只记不抓
            continue
        found.append({"url": url, "ext": ext})
    return found, skipped


def has_inline_table(html: str) -> bool:
    """正文里是否有「馬名 + 騎/練」这种名单表（内联形态）。"""
    return any(C.looks_like_header(row) for t in C.html_tables(html) for row in t)


def build_plan(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """索引 CSV → 抓取计划。

    同一 `race_uid` 只留一条（取 announce_date 最近）：实测同一场赛事会先发「宣佈出賽馬匹名單」
    （只有馬名/練馬師），几天后再发「最後宣佈出賽馬匹名單及排位」（含马号/档位/磅重）。
    两条都抓就是重复，且旧的一条字段更少 —— 留最新的一条，其余记进 duplicates.csv（供存档阶段用）。
    """
    plan: dict[str, dict] = {}
    dups: list[dict] = []
    for r in sorted(rows, key=lambda x: x.get("date", ""), reverse=True):
        uid = C.race_uid_of(r["date"], r["region"], r["title"])
        if uid in plan:
            dups.append({
                "race_uid": uid,
                "announce_date": r["date"],
                "region": r["region"],
                "title": r["title"],
                "url": r["url"],
                "kept_url": plan[uid]["url"],
                "reason": "same-url" if plan[uid]["url"] == r["url"] else "superseded",
            })
            continue
        plan[uid] = {
            "race_uid": uid,
            "announce_date": r["date"],
            "region": r["region"],
            "title": r["title"],
            "url": r["url"],
        }
    return list(plan.values()), dups


def load_manifest() -> dict[str, dict]:
    return {r["race_uid"]: r for r in C.read_csv(C.DOCS / "manifest.csv")}


def write_manifest(metas: list[dict]) -> Path:
    metas = sorted(metas, key=lambda m: (m.get("announce_date") or "", m["race_uid"]), reverse=True)
    return C.write_csv(C.DOCS / "manifest.csv", metas, MANIFEST_FIELDS)


def write_duplicates(dups: list[dict]) -> Path:
    """被同 race_uid 覆盖掉的公告（旧的一条）。不入 races，但留档，存档阶段仍可提交。"""
    p = C.DOCS / "duplicates.csv"
    old = C.read_csv(p)
    seen = {(r["race_uid"], r["url"]) for r in old}
    for d in dups:
        if (d["race_uid"], d["url"]) in seen:
            continue
        old.append(d)
        seen.add((d["race_uid"], d["url"]))
    return C.write_csv(p, old, ["race_uid", "announce_date", "region", "title", "url", "kept_url", "reason"])


def meta_path(uid: str) -> Path:
    return C.DOCS / uid / "meta.json"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="S2 · 抓公告正文与附件")
    ap.add_argument("--from", dest="start", type=int, default=0, help="计划内起始下标（含）")
    ap.add_argument("--to", dest="end", type=int, default=None, help="计划内结束下标（不含）")
    ap.add_argument("--region", default=None, help="只跑该地区的队列（不改变全量抓取口径）")
    ap.add_argument("--region-priority", default="日本", help="把该地区排到队首（默认日本）")
    ap.add_argument("--only-failed", action="store_true", help="只重跑 status != ok 的条目")
    ap.add_argument("--max-attach", type=int, default=2, help="每篇最多下载几个附件")
    ap.add_argument("--no-skip", action="store_true", help="忽略断点，全部重抓")
    a = ap.parse_args(argv)

    rows = C.read_csv(C.INDEX_CSV)
    if not rows:
        print(f"缺少索引：{C.INDEX_CSV}（先跑 python pipeline/s1_classify.py）")
        return 1

    plan, dups = build_plan(rows)
    if a.region:
        plan = [p for p in plan if p["region"] == a.region]
    # 队列顺序：优先地区在前，同地区内公告日期倒序（地区只决定顺序，不决定抓不抓）
    plan.sort(key=lambda p: p["announce_date"], reverse=True)
    if a.region_priority:
        plan.sort(key=lambda p: p["region"] != a.region_priority)
    plan = plan[a.start:a.end]

    old = {} if a.no_skip else load_manifest()
    g = Guard()
    metas: dict[str, dict] = dict(old)

    done = skipped = failed = 0
    try:
        for i, p in enumerate(plan, 1):
            uid = p["race_uid"]
            prev = metas.get(uid)
            if prev and prev.get("status") == "ok" and meta_path(uid).exists() and not a.no_skip:
                skipped += 1
                continue
            # --only-failed：只碰上一批没成功的（failed / partial / 无记录）
            if a.only_failed and prev and prev.get("status") == "ok":
                skipped += 1
                continue

            d = C.DOCS / uid
            d.mkdir(parents=True, exist_ok=True)
            meta = {
                "race_uid": uid,
                "announce_date": p["announce_date"],
                "region": p["region"],
                "title": p["title"],
                "article_url": p["url"],
                "article_status": "",
                "article_http": 0,
                "article_bytes": 0,
                "article_sha256": "",
                "attach_url": "",
                "attach_kind": "",
                "attach_status": "",
                "attach_http": 0,
                "attach_bytes": 0,
                "attach_sha256": "",
                "inline_table": 0,
                "source_kind": "",
                "status": "failed",
                "fetched_at": C.now_iso(),
                "error": "",
            }

            r = g.get(p["url"], accept_language="zh-HK,zh;q=0.9")
            meta["article_status"] = r.outcome
            meta["article_http"] = r.status
            if not r.ok:
                meta["error"] = f"article:{r.outcome}/{r.signal or r.status}"
                C.write_json(meta_path(uid), meta)
                metas[uid] = meta
                failed += 1
                continue

            html = r.text
            art = d / "article.html"
            art.write_text(html, encoding="utf-8")
            meta["article_bytes"] = art.stat().st_size
            meta["article_sha256"] = C.sha256_file(art)
            meta["inline_table"] = 1 if has_inline_table(html) else 0

            atts, skipped_atts = find_attachments(html, p["url"])
            if skipped_atts:
                meta["error"] = "skipped:" + ",".join(
                    f"{s['reason']}@{urlsplit(s['url']).netloc}" for s in skipped_atts[:3])

            kind = ""
            for att in atts[: a.max_attach]:
                dest = d / f"attach.{att['ext']}"
                dr = g.download(att["url"], dest)
                meta["attach_url"] = att["url"]
                meta["attach_status"] = dr.outcome
                meta["attach_http"] = dr.status
                if dr.ok:
                    meta["attach_kind"] = att["ext"]
                    meta["attach_bytes"] = int(dr.headers.get("bytes") or dest.stat().st_size)
                    meta["attach_sha256"] = dr.headers.get("sha256", "")
                    root = C.sniff_attach_kind(dest.read_bytes()[:8])
                    kind = root or att["ext"]
                    break
                meta["error"] = f"attach:{dr.outcome}/{dr.signal or dr.status}"

            if kind in ("docx", "doc"):
                meta["source_kind"] = kind
                meta["status"] = "ok"
            elif meta["inline_table"]:
                meta["source_kind"] = "inline"
                meta["status"] = "ok"
            else:
                meta["source_kind"] = ""
                meta["status"] = "partial" if not meta["attach_url"] else "failed"
                meta["error"] = meta["error"] or "no-usable-source"

            C.write_json(meta_path(uid), meta)
            metas[uid] = meta
            done += 1

            if done % 50 == 0:
                hk = g.interval("racingnews.hkjc.com")
                res = g.interval("res.hkjc.com")
                print(f"  … 已完成 {done} 篇｜racingnews {hk:.1f}s / res {res:.1f}s｜"
                      f"失败 {failed}", flush=True)

    except RiskControlStop as e:
        print(str(e), file=sys.stderr)
        write_manifest(list(metas.values()))
        return 2

    path = write_manifest(list(metas.values()))
    dup_path = write_duplicates(dups)
    ok = sum(1 for m in metas.values() if m.get("status") == "ok")
    partial = sum(1 for m in metas.values() if m.get("status") == "partial")
    inline = sum(1 for m in metas.values() if m.get("source_kind") == "inline")
    print(f"\n本次：新增 {done} / 跳过 {skipped} / 失败 {failed}")
    print(f"清单：{len(metas)} 场（ok {ok} / partial {partial} / 内联表 {inline}）")
    print(f"同赛事旧公告：{len(dups)} 条 → {dup_path}")
    print(f"→ {path}")
    print("跑完记得：python pipeline/netguard.py report（结论写进下一批 --interval）")
    return 0 if failed == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
