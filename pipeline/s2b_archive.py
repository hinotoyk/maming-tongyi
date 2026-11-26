#!/usr/bin/env python3
"""S2b · 提交 Internet Archive，落 cache/archive.csv（对外只给回放地址）

输入  cache/docs/manifest.csv（S2 产物；文章 URL + 附件 URL）
输出  cache/archive.csv（表结构与 db/schema.sql 的 archive 表一一对应）

口径（SCHEMA.md §6 / IMPL.md §6，都是实测）：
  · 提交只能 `GET https://web.archive.org/save/{url}`：匿名可用，**302 同步返回快照**；
    `POST /save` 匿名直接 401，不许用。
  · 先 CDX 探测：已有快照就记 `cdx_existing`，不再提交（少发一个请求比调间隔更有效）。
  · 间隔 ≥10s、单线程、串行；走 Guard（web.archive.org：10s 起 / 上限 180s / 熔断 3 次）。
  · 429 会带 Retry-After，可能是几分钟到几小时：照它睡，别按 10s 硬撞。
  · 401 = 匿名口径被收紧 → **整批停**、切 SPN2（需账号），绝不记成「未存档」。
  · 5xx / 超时 → pending，下轮重试（最多 3 次）；4xx / blocked-url → failed 并保留原链接。

用法
  python pipeline/s2b_archive.py --kind article --limit 20
  python pipeline/s2b_archive.py --region 日本
  python pipeline/netguard.py report        # 跑完必做：IA 是最容易被 429 的站
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common as C
from netguard import Guard, RiskControlStop

ARCHIVE_FIELDS = [
    "target_url", "target_kind", "entity_type", "entity_uid", "status", "capture_method",
    "job_id", "snapshot_ts", "snapshot_url", "http_status", "mimetype", "sha1", "bytes",
    "submitted_at", "checked_at", "error",
]
ATTEMPTS = C.CACHE / "archive_attempts.json"
TS_RE = re.compile(r"/web/(\d{14})/")


def cdx_probe(g: Guard, url: str) -> str | None:
    """CDX 查已有快照，命中返回 14 位时间戳。命中就不提交（幂等，省一次 save）。"""
    q = ("https://web.archive.org/cdx/search/cdx?url=" + quote(url, safe="")
         + "&output=json&limit=-1&filter=statuscode:200&fl=timestamp")
    r = g.get(q, want_body=True, timeout=60)
    if not r.ok:
        return None
    try:
        rows = r.body.decode("utf-8", "ignore").strip().splitlines()
    except Exception:
        return None
    if len(rows) < 2:
        return None
    m = re.search(r"(\d{14})", rows[-1])
    return m.group(1) if m else None


def save_page(g: Guard, url: str) -> tuple[str | None, int, str]:
    """GET /save/{url}，跟随 302，从最终地址取 ts。返回 (ts, http_status, error)。"""
    r = g.get("https://web.archive.org/save/" + url, timeout=90)
    if r.status == 401:
        return None, 401, "401-需登录（整批停、切 SPN2，不许记成未存档）"
    if r.status == 429:
        ra = r.headers.get("retry-after", "")
        return None, 429, f"429-retry-after={ra or '未给'}"
    if not r.ok:
        return None, r.status, f"{r.outcome}/{r.signal or r.status}"
    m = TS_RE.search(r.effective or "")
    if m:
        return m.group(1), r.status, ""
    # 少数情况最终地址里没有 ts：退一步用 CDX 拿最新快照（多一个请求，但结论可靠）
    ts = cdx_probe(g, url)
    return ts, r.status, "" if ts else "saved-but-no-ts"


def targets_from_manifest(region: str | None, kind: str) -> list[dict]:
    out: list[dict] = []
    for m in C.read_csv(C.DOCS / "manifest.csv"):
        if m.get("status") != "ok":
            continue
        if region and m.get("region") != region:
            continue
        if kind in ("article", "all") and m.get("article_url"):
            out.append({"url": m["article_url"], "kind": "article",
                        "entity_type": "race", "entity_uid": m["race_uid"],
                        "sha1": "", "bytes": m.get("article_bytes") or ""})
        if kind in ("attachment", "all") and m.get("attach_url") and m.get("source_kind") in ("docx", "doc"):
            path = C.DOCS / m["race_uid"] / f"attach.{m['attach_kind']}"
            sha1 = ""
            if path.exists():
                import hashlib

                sha1 = hashlib.sha1(path.read_bytes()).hexdigest()
            out.append({"url": m["attach_url"], "kind": "attachment",
                        "entity_type": "race", "entity_uid": m["race_uid"],
                        "sha1": sha1, "bytes": m.get("attach_bytes") or ""})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="S2b · 提交 Internet Archive")
    ap.add_argument("--kind", choices=["article", "attachment", "all"], default="all")
    ap.add_argument("--region", default=None, help="只提交该地区的公告（如 日本）")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--max-attempts", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true", help="只探测 CDX，不提交")
    a = ap.parse_args(argv)

    if not (C.DOCS / "manifest.csv").exists():
        print("缺少 cache/docs/manifest.csv（先跑 pipeline/s2_fetch_docs.py）")
        return 1

    rows = targets_from_manifest(a.region, a.kind)
    old = {(r["target_url"], r["target_kind"]): r for r in C.read_csv(C.ARCHIVE_CSV)}
    attempts = C.read_json(ATTEMPTS, {}) or {}
    todo = []
    for t in rows:
        prev = old.get((t["url"], t["kind"]))
        if prev and prev.get("status") == "success":
            continue
        if attempts.get(t["url"], 0) >= a.max_attempts:
            continue
        todo.append(t)
    if a.limit:
        todo = todo[: a.limit]

    print(f"待提交 {len(todo)} 个（已有记录 {len(old)}，成功 "
          f"{sum(1 for r in old.values() if r.get('status') == 'success')}）")
    if not todo:
        return 0

    g = Guard()
    done = success = failed = pending = 0
    try:
        for n, t in enumerate(todo, 1):
            ts = cdx_probe(g, t["url"])
            rec = {
                "target_url": t["url"], "target_kind": t["kind"],
                "entity_type": t["entity_type"], "entity_uid": t["entity_uid"],
                "status": "pending", "capture_method": "", "job_id": "",
                "snapshot_ts": "", "snapshot_url": "",
                "http_status": "", "mimetype": "", "sha1": t["sha1"], "bytes": t["bytes"],
                "submitted_at": C.now_iso(), "checked_at": C.now_iso(), "error": "",
            }
            if ts:
                rec.update(status="success", capture_method="cdx_existing", snapshot_ts=ts,
                           snapshot_url=f"https://web.archive.org/web/{ts}/{t['url']}")
                success += 1
            elif a.dry_run:
                rec.update(status="pending", error="dry-run")
                pending += 1
            else:
                ts, status, err = save_page(g, t["url"])
                rec["http_status"] = status
                if ts:
                    rec.update(status="success", capture_method="save_get", snapshot_ts=ts,
                               snapshot_url=f"https://web.archive.org/web/{ts}/{t['url']}")
                    success += 1
                elif status == 401:
                    old[(t["url"], t["kind"])] = rec
                    C.write_csv(C.ARCHIVE_CSV, list(old.values()), ARCHIVE_FIELDS)
                    print(f"\n{err}\n→ 整批停止。匿名口径被收紧，需改走 SPN2 API（IA_ACCESS_KEY/IA_SECRET_KEY）。",
                          file=sys.stderr)
                    return 4
                elif status == 429 or (status and 500 <= status < 600):
                    rec.update(status="pending", error=err)
                    pending += 1
                    time.sleep(min(600.0, float(re.search(r"retry-after=(\d+)", err).group(1))
                                   if re.search(r"retry-after=(\d+)", err) else 60.0))
                else:
                    rec.update(status="failed", error=err)
                    failed += 1

            attempts[t["url"]] = attempts.get(t["url"], 0) + 1
            old[(t["url"], t["kind"])] = rec
            done += 1
            if done % 20 == 0:
                C.write_csv(C.ARCHIVE_CSV, list(old.values()), ARCHIVE_FIELDS)
                C.write_json(ATTEMPTS, attempts)
                print(f"  … {done}/{len(todo)}｜成功 {success} 失败 {failed} 待重试 {pending}"
                      f"｜间隔 {g.interval('web.archive.org'):.0f}s", flush=True)
    except RiskControlStop as e:
        print(str(e), file=sys.stderr)
        C.write_csv(C.ARCHIVE_CSV, list(old.values()), ARCHIVE_FIELDS)
        C.write_json(ATTEMPTS, attempts)
        return 2

    C.write_csv(C.ARCHIVE_CSV, list(old.values()), ARCHIVE_FIELDS)
    C.write_json(ATTEMPTS, attempts)
    total_ok = sum(1 for r in old.values() if r.get("status") == "success")
    ratio = total_ok / len(old) if old else 0
    print(f"\n本次 {done} 个：成功 {success} / 失败 {failed} / 待重试 {pending}")
    print(f"累计成功率 {ratio:.1%}（判据 ≥90%）→ {C.ARCHIVE_CSV}")
    print("跑完记得：python pipeline/netguard.py report")
    return 0 if ratio >= 0.9 else 3


if __name__ == "__main__":
    sys.exit(main())
