#!/usr/bin/env python3
"""S4b 进度快照 → 供每半小时汇报用（只读，不改任何数据）

    python pipeline/s4b_status.py            # 打印一段紧凑状态
    python pipeline/s4b_status.py --json     # 给自动化/脚本用

读 cache/jbis.csv / jbis_profile.csv / pedigree.csv 计数，
并用 cache/.s4b_progress.json 里的上一次快照算速度（首次无速度时用 31s/匹兜底）。
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "cache"
SNAP = CACHE / ".s4b_progress.json"
LOG = CACHE / "s4b_loop.log"
OLD_LOG = CACHE / "s6_run.log"
FALLBACK_SEC = 31.0


def read_csv(path: Path) -> list[dict]:
    import csv
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    entries = read_csv(CACHE / "entries.csv")
    jp = {e["horse_uid"] for e in entries if (e.get("origin") or "").upper() == "JPN"}
    jb = read_csv(CACHE / "jbis.csv")
    have_id = {r["horse_uid"] for r in jb
               if r.get("jbis_id") and r.get("status") in ("ok", "weak")}
    prof = {r["horse_uid"] for r in read_csv(CACHE / "jbis_profile.csv")}
    ped = {r["horse_uid"] for r in read_csv(CACHE / "pedigree.csv")}
    giveup = {r["horse_uid"] for r in jb
              if str(r.get("status") or "").startswith(("no-match", "empty"))}

    total = len(jp)
    n_id, n_prof, n_ped = len(have_id & jp), len(prof & jp), len(ped & jp)
    left = total - n_ped

    # 速度：与上次快照比
    now = time.time()
    prev = {}
    if SNAP.exists():
        try:
            prev = json.loads(SNAP.read_text(encoding="utf-8"))
        except Exception:
            prev = {}
    sec_per = FALLBACK_SEC
    if prev.get("ped") is not None and prev.get("ts") and n_ped > prev["ped"]:
        dt = now - float(prev["ts"])
        if dt > 60:
            sec_per = dt / (n_ped - prev["ped"])
    SNAP.parent.mkdir(parents=True, exist_ok=True)
    SNAP.write_text(json.dumps({"ts": now, "ped": n_ped, "prof": n_prof,
                                "id": n_id, "sec_per": round(sec_per, 1)}),
                    encoding="utf-8")

    # 是否在跑：日志写入 + 血统快照落盘（每匹一个 json，约 25s 一个）取最新
    cand: list[float] = []
    for p in (LOG, OLD_LOG):
        if p.exists():
            cand.append(p.stat().st_mtime)
    pdir = CACHE / "pedigree"
    if pdir.is_dir():
        cand.extend(p.stat().st_mtime for p in pdir.glob("*.json"))
    idle = round(now - max(cand)) if cand else None
    running = idle is not None and idle < 180

    # 本批进度（驱动日志里的「… 120/400」）
    batch = None
    if LOG.exists():
        import re
        hits = re.findall(r"… (\d+)/(\d+)（", LOG.read_text(encoding="utf-8", errors="ignore")[-8000:])
        if hits:
            batch = f"{hits[-1][0]}/{hits[-1][1]}"

    # 风控信号
    risk = None
    for p in (LOG, OLD_LOG):
        if not p.exists():
            continue
        tail = p.read_text(encoding="utf-8", errors="ignore")[-4000:]
        for kw in ("风控", "breaker", "429", "停止"):
            if kw in tail:
                risk = f"日志尾部出现「{kw}」"

    def hhmm(sec: float) -> str:
        t = time.localtime(now + sec)
        return time.strftime("%m-%d %H:%M", t)

    data = {
        "total": total, "with_id": n_id, "profile": n_prof, "pedigree": n_ped,
        "left": left, "giveup": len(giveup & jp),
        "pct_id": round(n_id / total * 100, 1) if total else 0,
        "pct_ped": round(n_ped / total * 100, 1) if total else 0,
        "sec_per_horse": round(sec_per, 1),
        "eta_full": hhmm(left * sec_per) if left else "已完成",
        "running": running, "idle_sec": idle, "risk": risk, "batch": batch,
    }

    if a.json:
        print(json.dumps(data, ensure_ascii=False))
        return 0

    print(f"日本马 {total} 匹")
    print(f"  有 JBIS id   {n_id:>4} 匹  {data['pct_id']}%")
    print(f"  基本情報     {n_prof:>4} 匹")
    print(f"  5 代血统     {n_ped:>4} 匹  {data['pct_ped']}%   剩 {left} 匹")
    print(f"  检索无此马（放弃）{len(giveup & jp)} 匹")
    print(f"速度 {sec_per:.0f}s/匹 · 全量预计 {data['eta_full']}")
    print(f"本批 {batch or '—'} · 状态 {'运行中' if running else '已停'}"
          f"（{idle if idle is not None else '无'}s 前有动作）"
          + (f" · 风控信号：{risk}" if risk else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
