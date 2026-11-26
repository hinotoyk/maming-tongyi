#!/usr/bin/env python3
"""S4b 连续续跑驱动：一批跑完自动起下一批，直到全量补齐。

为什么要它：一匹 3 个请求 × 10s 地板，全量 3000 匹要 25 小时，
手点 `--limit` 会中断成很多段。这个脚本负责串起来，并且**不会**和前一个进程抢请求：
启动时先等 cache/s6_run.log 静默 200 秒（说明上一批真的结束了），再开始。

    python pipeline/s4b_loop.py                # 后台跑（推荐用 run_in_background）
    python pipeline/s4b_loop.py --batch 400    # 每批多少匹
    python pipeline/s4b_loop.py --wait 200     # 启动前等日志静默多少秒

日志：cache/s4b_loop.log；状态：python pipeline/s4b_status.py
遇到 RiskControlStop（退出码 2）不硬顶，停 30 分钟再试。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "cache"
DRIVER_LOG = CACHE / "s4b_loop.log"
# 只盯前一批（旧命令 tee 出来的）日志，不能盯自己的日志，否则心跳会把自己判成「还在跑」
PREV_LOG = CACHE / "s6_run.log"


def log(msg: str) -> None:
    line = f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with DRIVER_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def quiet_secs() -> float:
    if not PREV_LOG.exists():
        return 1e9
    return time.time() - PREV_LOG.stat().st_mtime


def wait_quiet(seconds: int) -> None:
    """等其他进程收尾；期间每分钟写一行心跳，让 status 知道驱动还活着。"""
    waited = 0
    while quiet_secs() < seconds:
        if waited % 60 == 0:
            log(f"等待前一批收尾（日志静默 {quiet_secs():.0f}s / 需 {seconds}s）")
        time.sleep(10)
        waited += 10
    if waited:
        log(f"前一批已停（静默 {quiet_secs():.0f}s），开始续跑")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=400)
    ap.add_argument("--wait", type=int, default=200)
    ap.add_argument("--pause-on-risk", type=int, default=1800, help="疑似风控时停多少秒")
    a = ap.parse_args(argv)

    wait_quiet(a.wait)

    rnd = 0
    while True:
        rnd += 1
        log(f"第 {rnd} 批开始（--limit {a.batch}）")
        # 必须逐行透传：一批 400 匹要跑三小时，若用 capture_output 攒到批次结束才写，
        # 这期间的 s4b_loop.log 是死的，半小时汇报会误判成「已停」。
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "pipeline" / "s4b_pedigree.py"), "--limit", str(a.batch)],
            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        last: list[str] = []
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            # 进度行（… 10/400）每隔十匹一条，只透传这行和摘要，请求流水不打进驱动日志
            if line.lstrip().startswith("…") or "本轮完成" in line or "还剩" in line\
                    or "无需补全" in line or "日本马" in line:
                log("  " + line)
            last.append(line)
            last[:] = last[-8:]
        rc = proc.wait()
        out = last

        if rc == 2:
            log(f"触发风控保护（rc=2），停 {a.pause_on_risk}s 再试")
            time.sleep(a.pause_on_risk)
            continue
        if rc != 0:
            log(f"异常退出 rc={rc}，30 分钟后重试")
            time.sleep(1800)
            continue
        if any("无需补全" in line for line in out):
            log("全量已补齐，驱动结束")
            return 0
        tail = next((line for line in reversed(out) if "还剩" in line), "")
        if "还剩 0 匹" in tail:
            log("全量已补齐，驱动结束")
            return 0
        time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
