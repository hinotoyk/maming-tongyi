#!/usr/bin/env python3
"""统一出网闸门：限速 + 风控识别 + 自适应退避 + 请求流水 + 收尾报告。

所有抓取脚本（S0 / S2 / S2b / S4 的 JBIS）**只准**通过 `Guard.get()` 出网，
不许自己 `curl` / `urlopen`，否则限速、风控识别和流水全部失效，出事时无从复盘。

设计只有一条主线：
    间隔只能因为「看到风控信号」而上调，永远不会低于 AGENTS.md 的硬地板。

    floor ──(连续成功 20 次 ×0.9)──▶ 缓慢回落 ◀──
      ▲                                        │
      └──(429/403/400/挑战页 ×1.6)─────────────┘  ceiling

用法：
    from netguard import Guard
    g = Guard()
    r = g.get("https://racingnews.hkjc.com/...")      # r.ok / r.body / r.outcome
    g.download(url, dest)                             # 大文件（docx/doc）

    python pipeline/netguard.py status                # 各站当前间隔、最近 20 条
    python pipeline/netguard.py report                # 从流水生成间隔建议（跑完就跑）
    python pipeline/netguard.py tune --host h --interval 3.5   # 人工临时调整
    python pipeline/netguard.py selftest              # 不联网，验证判定与报告

被风控打停时抛 `RiskControlStop`，脚本 `except` 后 `sys.exit(2)` 即可；
状态已落盘，换时间重跑会从「已抬高的间隔」继续，不会一秒回到地板。
"""
from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "cache/http"
JOURNAL = CACHE / "journal.jsonl"
STATE = CACHE / "state.json"
REPORT = CACHE / "ratelimit-report.md"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# ── 每站策略 ────────────────────────────────────────────────────────────
# floor 来自 AGENTS.md §2.5/§2.7，改这里等于改红线，必须先改 AGENTS.md。
# ceiling 是自适应的上限，不是许可：到顶还在挨打就说明今天不该继续跑。


@dataclass(frozen=True)
class Policy:
    floor: float
    ceiling: float
    backoff: float = 1.6          # 挨打一次，间隔乘这个数
    recover_after: int = 20       # 连续成功多少次才允许降一档
    recover_factor: float = 0.9   # 每次降 10%，慢慢回到 floor
    breaker: int = 5              # 连续挨打几次 → 熔断停机
    max_retries: int = 2          # 空转/超时重试次数（风控信号只重试 1 次）
    cooldown: float = 900.0       # 熔断后建议冷却秒数（写进日志，不自动睡）


POLICIES: dict[str, Policy] = {
    # 起始间隔 = cache/http/ratelimit-report.md 的「建议间隔」（IMPL.md §3.6：
    # 下一批的间隔以报告为准，而不是沿用上一批的手感值）。
    "hkjc.com": Policy(floor=2.5, ceiling=30.0),
    "jbis.or.jp": Policy(floor=10.0, ceiling=60.0, breaker=4, cooldown=1800.0),
    "netkeiba.com": Policy(floor=5.0, ceiling=45.0),
    "web.archive.org": Policy(floor=10.5, ceiling=180.0, breaker=3, cooldown=3600.0),
}
DEFAULT_POLICY = Policy(floor=3.0, ceiling=60.0)


def policy_for(host: str) -> Policy:
    """按后缀匹配（racingnews.hkjc.com → hkjc.com），未知站点按 3s 保守走。"""
    h = host.lower().split(":")[0]
    for suffix, pol in POLICIES.items():
        if h == suffix or h.endswith("." + suffix):
            return pol
    return DEFAULT_POLICY


# ── 风控信号 ────────────────────────────────────────────────────────────
# 判定顺序：curl 失败 → HTTP 状态码 → 正文特征。正文特征最后查，因为要读 body。
CHALLENGE_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("cloudflare", re.compile(r"Just a moment\.\.\.|Attention Required|__cf_chl|cf-browser-verification", re.I)),
    ("akamai", re.compile(r"Access Denied|Reference #\d+\.[0-9a-f]{6,}", re.I)),
    ("incapsula", re.compile(r"Incapsula|_Incapsula_Resource", re.I)),
    ("rate-limit-page", re.compile(r"Too Many Requests|Request Rate Threshold|rate limit exceeded", re.I)),
    ("jp-wait", re.compile(r"しばらく(?:時間|お待ち)|アクセスが集中|混雑しており")),
    ("ia-blocked", re.compile(r"blocked-url|This URL has been excluded", re.I)),
]

# 这些是「风控」，要抬间隔；其它失败不抬。
RISK_OUTCOMES = {"throttle", "challenge"}


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _pct(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * q
    lo, hi = int(pos), min(int(pos) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


@dataclass
class Reply:
    url: str
    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    latency_ms: int = 0
    outcome: str = "ok"          # ok / throttle / challenge / notfound / server / net / empty
    signal: str = ""              # 具体是哪条规则命中，写进流水
    interval: float = 0.0         # 本次生效的间隔（含抖动后的实际等待）
    attempts: int = 1
    effective: str = ""           # 跟随跳转后的最终地址（存档提交靠它取 /web/{ts}/）

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "ignore")

    def raise_for_risk(self) -> "Reply":
        if self.outcome in RISK_OUTCOMES:
            raise RiskControlStop(self.url, self.outcome, self.signal, self.status)
        return self


class RiskControlStop(RuntimeError):
    """撞上风控且熔断。调用方停批、落日志、换时间再跑；不要当场狂重试。"""

    def __init__(self, url: str, outcome: str, signal: str, status: int):
        self.url, self.outcome, self.signal, self.status = url, outcome, signal, status
        super().__init__(
            f"风控熔断：{outcome}/{signal or status} @ {url}\n"
            f"→ 本批停止。状态已落盘，冷却后再跑（脚本会从抬高后的间隔继续）。"
        )


class Guard:
    def __init__(
        self,
        journal: Path = JOURNAL,
        state: Path = STATE,
        *,
        jitter: float = 0.10,
        dry_run: bool = False,
        verbose: bool = True,
    ) -> None:
        self.journal = Path(journal)
        self.state_path = Path(state)
        self.jitter = jitter
        self.dry_run = dry_run
        self.verbose = verbose
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        self._state = self._load_state()

    # ── 状态 ────────────────────────────────────────────────────────────
    def _load_state(self) -> dict:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        return {"hosts": {}}

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _host(self, host: str) -> dict:
        h = self._state["hosts"].setdefault(
            host,
            {
                "interval": policy_for(host).floor,
                "streak": 0,        # 连续成功次数
                "risk_streak": 0,   # 连续风控次数
                "breaker_trips": 0,
                "last_ts": 0.0,
                "last_outcome": "",
                "updated": _now(),
            },
        )
        return h

    def interval(self, host: str) -> float:
        return float(self._host(host)["interval"])

    def floor(self, host: str) -> float:
        """当前地板。写进流水是为了让「有没有低于地板」按**当时**的地板判，
        否则以后调高地板会把历史上的合规请求全判成违规（实测踩过）。"""
        return policy_for(host).floor

    def tune(self, host: str, interval: float | None = None, *, reset: bool = False) -> float:
        """人工临时调整。只能往「更慢」改，往快改会被 floor 拦住（这是有意的）。"""
        pol = policy_for(host)
        st = self._host(host)
        if reset:
            st["interval"], st["streak"], st["risk_streak"] = pol.floor, 0, 0
        elif interval is not None:
            st["interval"] = max(pol.floor, min(pol.ceiling, float(interval)))
        st["updated"] = _now()
        self._save_state()
        return float(st["interval"])

    # ── 出网 ────────────────────────────────────────────────────────────
    def get(
        self,
        url: str,
        *,
        timeout: int = 90,
        accept_language: str = "",
        want_body: bool = True,
    ) -> Reply:
        return self._fetch(url, timeout=timeout, accept_language=accept_language,
                           want_body=want_body, dest=None)

    def download(self, url: str, dest: Path, *, timeout: int = 180) -> Reply:
        """附件下载：直接落盘，不把 docx 读进内存；仍然记流水、照样限速。"""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        r = self._fetch(url, timeout=timeout, accept_language="", want_body=False, dest=dest)
        if r.ok:
            r.headers["sha256"] = sha256(dest.read_bytes()).hexdigest()
            r.headers["bytes"] = str(dest.stat().st_size)
        return r

    def _fetch(
        self,
        url: str,
        *,
        timeout: int,
        accept_language: str,
        want_body: bool,
        dest: Path | None,
    ) -> Reply:
        host = re.sub(r"^https?://", "", url).split("/")[0]
        pol = policy_for(host)
        st = self._host(host)

        total_attempts = 0
        last: Reply | None = None
        while total_attempts <= pol.max_retries:
            total_attempts += 1
            wait = self._respect_interval(host, pol, st)
            last = self._once(url, host, timeout=timeout,
                              accept_language=accept_language,
                              want_body=want_body, dest=dest)
            last.interval = round(wait, 2)
            last.attempts = total_attempts

            self._apply_outcome(host, pol, st, last)
            self._log(host, last)
            if self.verbose:
                self._print(host, last, st)

            if last.ok:
                return last

            if last.outcome in RISK_OUTCOMES:
                # 已经抬过间隔了；只再试一次，还挨打就熔断，绝不当场连环重试。
                if st["risk_streak"] >= pol.breaker:
                    st["breaker_trips"] += 1
                    self._save_state()
                    raise RiskControlStop(url, last.outcome, last.signal, last.status)
                if total_attempts >= 2:
                    self._save_state()
                    return last
                ra = self._retry_after(last)
                time.sleep(max(ra, self.interval(host)))
                continue

            if last.outcome in {"server", "net", "empty"} and total_attempts <= pol.max_retries:
                time.sleep(min(60.0, 2.0 ** total_attempts))
                continue
            return last

        assert last is not None
        return last

    def _respect_interval(self, host: str, pol: Policy, st: dict) -> float:
        """睡到「上次请求 + 间隔」；抖动只往上抖，保证任何时刻都不低于 floor。"""
        target = float(st["interval"]) * random.uniform(1.0 - self.jitter, 1.0 + self.jitter)
        target = max(pol.floor, min(pol.ceiling, target))
        elapsed = time.time() - float(st["last_ts"] or 0.0)
        if elapsed < target:
            delay = target - elapsed
            if not self.dry_run:
                time.sleep(delay)
            else:
                target = elapsed or target
        st["last_ts"] = time.time()
        return target

    def _once(
        self,
        url: str,
        host: str,
        *,
        timeout: int,
        accept_language: str,
        want_body: bool,
        dest: Path | None,
    ) -> Reply:
        hdr_file = Path(tempfile.mkstemp(prefix="ng-h-", suffix=".txt")[1])
        body_file = dest or Path(tempfile.mkstemp(prefix="ng-b-", suffix=".bin")[1])
        cmd = [
            "curl", "-sS", "-L", "--max-time", str(timeout),
            "-A", UA,
            "-H", "Accept-Language: " + (accept_language or "zh-HK,zh;q=0.9,en;q=0.8"),
            "-o", str(body_file), "-D", str(hdr_file),
            "-w", "%{http_code} %{time_total} %{size_download} %{url_effective}",
            url,
        ]
        t0 = time.perf_counter()
        p = subprocess.run(cmd, capture_output=True)
        latency_ms = int((time.perf_counter() - t0) * 1000)

        status, headers, effective = 0, {}, ""
        raw = p.stdout.decode("utf-8", "ignore").strip().split()
        if len(raw) >= 3 and raw[0].isdigit():
            status = int(raw[0])
        if len(raw) >= 4:
            effective = raw[3]
        try:
            headers = _parse_headers(hdr_file.read_text("utf-8", "ignore"))
        except OSError:
            pass

        body = b""
        if status and want_body and dest is None:
            try:
                body = body_file.read_bytes()
            except OSError:
                body = b""

        outcome, signal = self._classify(url, status, headers, body, p.returncode,
                                        body_file if dest is None else None)

        # 清理临时件（dest 是调用方的文件，不能删）
        if dest is None:
            for f in (hdr_file, body_file):
                try:
                    f.unlink()
                except OSError:
                    pass
        else:
            try:
                hdr_file.unlink()
            except OSError:
                pass

        return Reply(url=url, status=status, body=body, headers=headers,
                     latency_ms=latency_ms, outcome=outcome, signal=signal, effective=effective)

    @staticmethod
    def _classify(
        url: str,
        status: int,
        headers: dict[str, str],
        body: bytes,
        rc: int,
        body_path: Path | None,
    ) -> tuple[str, str]:
        if rc != 0 and status == 0:
            # curl: 28 超时 / 6 DNS / 7 连接 / 35 TLS / 52 空回包
            return ("net", f"curl-{rc}")

        if status == 429:
            return ("throttle", "http-429")
        if status == 403:
            return ("throttle", "http-403")
        if status == 400:
            # HKJC 的快请求签名：不是参数错，是节奏错（实测，见 docs/IMPL.md）
            return ("throttle", "http-400" if "hkjc.com" in url else "http-400-other")
        if status in (301, 302, 303, 307, 308):
            return ("net", f"redirect-unfollowed-{status}")

        text = ""
        if body:
            text = body[:200_000].decode("utf-8", "ignore")
        elif body_path is not None and body_path.exists() and body_path.stat().st_size < 400_000:
            text = body_path.read_text("utf-8", "ignore")

        if text:
            for name, pat in CHALLENGE_MARKERS:
                if pat.search(text):
                    return ("challenge", name)
            # 正文短得不像内容页：HKJC 限速时会给一张空壳页，别当成功往下走
            looks_html = "text/html" in headers.get("content-type", "") or text.lstrip().startswith("<")
            if status == 200 and len(text) < 600 and looks_html:
                return ("empty", "short-html")

        if status in (404, 410):
            return ("notfound", f"http-{status}")
        if status == 503:
            # JBIS 详情页 503 是结构性的（详情页本来就打不开），不算风控节奏问题
            if "jbis.or.jp" in url and re.search(r"/horse/\d+/?$", url):
                return ("server", "jbis-detail-503")
            return ("server", "http-503")
        if 500 <= status < 600:
            return ("server", f"http-{status}")
        if status == 200 or status in (201, 202, 204):
            return ("ok", "")
        return ("server", f"http-{status}")

    def _apply_outcome(self, host: str, pol: Policy, st: dict, r: Reply) -> None:
        if r.outcome in RISK_OUTCOMES:
            st["interval"] = min(pol.ceiling, float(st["interval"]) * pol.backoff)
            st["risk_streak"] += 1
            st["streak"] = 0
        elif r.ok:
            st["risk_streak"] = 0
            st["streak"] += 1
            if st["streak"] % pol.recover_after == 0 and float(st["interval"]) > pol.floor:
                st["interval"] = max(pol.floor, float(st["interval"]) * pol.recover_factor)
        st["last_outcome"] = r.outcome
        st["updated"] = _now()
        self._save_state()

    @staticmethod
    def _retry_after(r: Reply) -> float:
        v = r.headers.get("retry-after", "")
        try:
            return max(0.0, float(v))
        except ValueError:
            return 0.0

    def _log(self, host: str, r: Reply) -> None:
        rec = {
            "ts": _now(),
            "epoch": round(time.time(), 3),
            "host": host,
            "url": r.url,
            "status": r.status,
            "latency_ms": r.latency_ms,
            "bytes": int(r.headers.get("bytes") or len(r.body)),
            "outcome": r.outcome,
            "signal": r.signal,
            "interval": r.interval,
            "floor": self.floor(host),
            "attempts": r.attempts,
        }
        with self.journal.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _print(self, host: str, r: Reply, st: dict) -> None:
        mark = {
            "ok": "ok  ", "throttle": "RISK", "challenge": "RISK",
            "notfound": "404 ", "server": "5xx ", "net": "NET ", "empty": "EMPTY",
        }.get(r.outcome, r.outcome)
        print(
            f"[{mark}] {r.status or '---':>3} {r.latency_ms:>6}ms "
            f"itv={r.interval:>5.1f}s (now {st['interval']:.1f}s) "
            f"{host}{_path(r.url)}"
            + (f"  ← {r.signal}" if r.signal else ""),
            file=sys.stderr,
        )

    # ── 收尾报告 ────────────────────────────────────────────────────────
    def report(self, *, since: str | None = None, min_samples: int = 50,
               path: Path = REPORT) -> Path:
        recs = _load_journal(self.journal, since)
        lines: list[str] = []
        lines.append("# 限速与风控体检报告\n")
        try:
            jpath = self.journal.relative_to(ROOT).as_posix()
        except ValueError:
            jpath = str(self.journal)
        lines.append(
            f"生成时间 {_now()}　流水 `{jpath}`　"
            f"样本 {len(recs)} 条" + (f"（since {since}）" if since else "") + "\n"
        )
        if not recs:
            lines.append("\n> 没有流水，先生成流量再来看。\n")
            path.write_text("\n".join(lines), encoding="utf-8")
            return path

        hosts = sorted({r["host"] for r in recs})
        verdicts: list[tuple[str, str, float, str]] = []

        lines.append("\n## 结论\n")
        lines.append("| 站点 | 稳定性 | 建议间隔 | 依据 |")
        lines.append("| --- | --- | ---: | --- |")
        for h in hosts:
            rs = [r for r in recs if r["host"] == h]
            stats = _host_stats(h, rs, min_samples)
            verdicts.append((h, stats["verdict"], stats["recommend"], stats["basis"]))
            lines.append(
                f"| `{h}` | {stats['verdict']} | **{stats['recommend']:.1f}s** | {stats['basis']} |"
            )

        lines.append("\n## 明细\n")
        for h in hosts:
            rs = [r for r in recs if r["host"] == h]
            s = _host_stats(h, rs, min_samples)
            lines.append(f"\n### `{h}`\n")
            lines.append(f"- 请求 {len(rs)} 条，成功 {s['ok']} 条（{s['ok_rate']:.1%}）")
            lines.append(
                "- 结果分布：" + "　".join(f"{k} {v}" for k, v in sorted(s["outcomes"].items()))
            )
            lines.append(
                f"- 延迟 p50 {s['p50']:.0f}ms / p90 {s['p90']:.0f}ms / p99 {s['p99']:.0f}ms"
            )
            lines.append(
                f"- 生效间隔 min {s['itv_min']:.1f}s / 中位 {s['itv_med']:.1f}s / max {s['itv_max']:.1f}s"
                f"，硬地板 {policy_for(h).floor:.1f}s"
            )
            lines.append(f"- 风控事件 {s['risk']} 次，连续成功最长 {s['best_streak']} 次")
            if s["buckets"]:
                lines.append("\n| 间隔档 | 请求数 | 风控次数 | 风控率 |")
                lines.append("| ---: | ---: | ---: | ---: |")
                for b, n, rk in s["buckets"]:
                    lines.append(f"| {b:.1f}s | {n} | {rk} | {(rk / n if n else 0):.1%} |")
            lines.append("")

        lines.append("\n## 怎么用这份报告\n")
        lines.append(
            "1. 「建议间隔」= 本批**零风控**的最小实测档 × 1.25，并夹在 `[floor, ceiling]` 之间；\n"
            "   样本不足 50 的档位不采信，会退回当前值并标「样本不足」。\n"
            "2. 采纳方式：把结论表的数值抄进各脚本的默认 `--interval`（或 `POLICIES` 的 floor），\n"
            "   下一批就以新节奏开跑；`state.json` 里的自适应值仍会在挨打时继续往上顶。\n"
            "3. 结论是「上一批」的，不是永久真理——站点策略会变，每批跑完重跑一次 `report`。\n"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")

        print(f"报告已写入 {path}")
        for h, v, rec, basis in verdicts:
            print(f"  {h:<28} {v:<6} 建议 {rec:.1f}s  （{basis}）")
        return path


# ── 报告计算 ────────────────────────────────────────────────────────────
def _load_journal(p: Path, since: str | None) -> list[dict]:
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if since and str(r.get("ts", "")) < since:
            continue
        out.append(r)
    return out


def _host_stats(host: str, rs: list[dict], min_samples: int) -> dict:
    pol = policy_for(host)
    n = len(rs)
    ok = [r for r in rs if r["outcome"] == "ok"]
    lat = [r["latency_ms"] for r in ok] or [r["latency_ms"] for r in rs]
    itv = [float(r["interval"]) for r in rs]
    outcomes: dict[str, int] = {}
    for r in rs:
        outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1
    risk = sum(v for k, v in outcomes.items() if k in RISK_OUTCOMES) + \
        sum(1 for r in rs if r["outcome"] == "empty")

    # 连续成功最长
    best = cur = 0
    for r in rs:
        cur = cur + 1 if r["outcome"] == "ok" else 0
        best = max(best, cur)

    # 间隔→风控率分档（0.5s 一档）
    buckets_map: dict[float, list[int]] = {}
    for r in rs:
        b = round(float(r["interval"]) * 2) / 2
        pair = buckets_map.setdefault(b, [0, 0])
        pair[0] += 1
        if r["outcome"] in RISK_OUTCOMES:
            pair[1] += 1
    buckets = sorted((b, v[0], v[1]) for b, v in buckets_map.items())

    # 建议值：零风控且样本够的最小档 ×1.25
    safe = [b for b, cnt, rk in buckets if rk == 0 and cnt >= min_samples]
    if safe:
        recommend = max(pol.floor, min(pol.ceiling, min(safe) * 1.25))
        basis = f"零风控最小档 {min(safe):.1f}s ×1.25"
    elif buckets:
        recommend = max(pol.floor, float(itv[-1]))
        basis = f"仅 {n} 条样本 / 各档 <{min_samples}，先维持 {recommend:.1f}s"
    else:
        recommend = pol.floor
        basis = "无样本，按地板"

    ok_rate = len(ok) / n if n else 0.0
    risk_rate = risk / n if n else 0.0
    tail = rs[-200:]
    tail_risk = sum(1 for r in tail if r["outcome"] in RISK_OUTCOMES)
    if n < min_samples:
        # 8 条全绿不等于「稳定」——样本不够就不下结论，免得下一批照着一个偶然值开跑
        verdict = "样本不足"
    elif risk_rate <= 0.01 and tail_risk == 0:
        verdict = "稳定"
    elif risk_rate <= 0.05:
        verdict = "基本稳定"
    elif risk_rate <= 0.15:
        verdict = "偏紧"
    else:
        verdict = "被风控"

    return {
        "ok": len(ok), "ok_rate": ok_rate, "outcomes": outcomes,
        "p50": _pct(lat, 0.50), "p90": _pct(lat, 0.90), "p99": _pct(lat, 0.99),
        "itv_min": min(itv), "itv_max": max(itv), "itv_med": statistics.median(itv),
        "risk": risk, "best_streak": best, "buckets": buckets,
        "recommend": round(recommend * 2) / 2, "basis": basis, "verdict": verdict,
    }


def _parse_headers(raw: str) -> dict[str, str]:
    """-L 会有多个响应块，取最后一块。"""
    blocks = re.split(r"\r?\n\r?\n", raw.strip())
    h: dict[str, str] = {}
    for block in reversed(blocks):
        if not block.strip().upper().startswith("HTTP/"):
            continue
        for line in block.splitlines()[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                h[k.strip().lower()] = v.strip()
        break
    return h


def _path(url: str, n: int = 56) -> str:
    u = re.sub(r"^https?://[^/]+", "", url)
    return u if len(u) <= n else u[: n - 1] + "…"


def _short(url: str, n: int = 64) -> str:
    u = re.sub(r"^https?://", "", url)
    return u if len(u) <= n else u[: n - 1] + "…"


# ── CLI ─────────────────────────────────────────────────────────────────
def _cmd_status(g: Guard) -> None:
    st = g._state.get("hosts", {})
    if not st:
        print("还没有任何出网记录。")
        return
    print(f"{'站点':<28}{'当前间隔':>9}{'连续成功':>9}{'连续风控':>9}{'熔断':>5}  最近")
    for h, v in sorted(st.items()):
        print(
            f"{h:<28}{v['interval']:>8.1f}s{v['streak']:>9}{v['risk_streak']:>9}"
            f"{v['breaker_trips']:>5}  {v['last_outcome'] or '-'}"
        )
    recs = _load_journal(g.journal, None)[-12:]
    if recs:
        print("\n最近 12 条：")
        for r in recs:
            print(
                f"  {r['ts'][11:19]} {r['outcome']:<9} {r['status']:>3} "
                f"{r['latency_ms']:>5}ms itv={r['interval']:>4.1f}s {_short(r['url'], 56)}"
            )


def _cmd_selftest() -> int:
    """不联网，验证判定表与报告数学。"""
    cases = [
        ("https://racingnews.hkjc.com/x", 400, "", 0, "", "throttle"),
        ("https://racingnews.hkjc.com/x", 200, "text/html", 0, "<html><body>ok</body></html>", "empty"),
        ("https://www.jbis.or.jp/horse/1234567890/", 503, "", 0, "", "server"),
        ("https://db.netkeiba.com/x", 503, "", 0, "", "server"),
        ("https://db.netkeiba.com/x", 403, "", 0, "", "throttle"),
        ("https://web.archive.org/save/x", 200, "", 0,
         "<h1>Too Many Requests</h1>", "challenge"),
        ("https://web.archive.org/save/x", 200, "", 0, "blocked-url: excluded", "challenge"),
        ("https://x.example/a", 0, "", 28, "", "net"),
        ("https://x.example/a", 404, "", 0, "", "notfound"),
        ("https://www.jbis.or.jp/horse/result/?keyword=x", 200, "", 0,
         "<html>" + "あ" * 800 + "</html>", "ok"),
    ]
    bad = 0
    for url, status, ctype, rc, body, want in cases:
        hdrs = {"content-type": ctype} if ctype else {}
        got, sig = Guard._classify(url, status, hdrs, body.encode(), rc, None)
        flag = "ok " if got == want else "FAIL"
        if got != want:
            bad += 1
        print(f"[{flag}] {status or rc:>3} → {got:<9} (期望 {want})  {_short(url, 46)}")

    # 自适应数学：抬得上去、落得回地板、永不越界
    pol = policy_for("racingnews.hkjc.com")
    st = {"interval": pol.floor, "streak": 0, "risk_streak": 0,
          "breaker_trips": 0, "last_ts": 0, "last_outcome": "", "updated": ""}
    g = Guard(state=Path(tempfile.mkdtemp()) / "s.json", journal=Path(tempfile.mkdtemp()) / "j.jsonl",
              dry_run=True, verbose=False)
    for _ in range(3):
        g._apply_outcome("racingnews.hkjc.com", pol, st,
                         Reply("u", 400, outcome="throttle", signal="http-400"))
    up = st["interval"]
    for _ in range(pol.recover_after * 12):
        g._apply_outcome("racingnews.hkjc.com", pol, st, Reply("u", 200))
    down = st["interval"]
    print(f"\n自适应：{pol.floor:.1f}s --3 次风控--> {up:.1f}s --240 次成功--> {down:.1f}s")
    ok_math = up > pol.floor * 2 and pol.floor <= down < up
    print(("ok  " if ok_math else "FAIL") + " 间隔能抬能落，且不破地板")

    # 报告数学
    fake = []
    for i, (itv, oc) in enumerate(
        [(2.0, "throttle")] * 5 + [(2.0, "ok")] * 20 + [(3.5, "ok")] * 80 + [(5.0, "ok")] * 30
    ):
        fake.append({"ts": f"2026-11-24T10:{i // 60 :02d}:{i % 60 :02d}", "epoch": i,
                     "host": "racingnews.hkjc.com", "url": "u", "status": 200 if oc == "ok" else 400,
                     "latency_ms": 300, "bytes": 1000, "outcome": oc, "signal": "",
                     "interval": itv, "attempts": 1})
    s = _host_stats("racingnews.hkjc.com", fake, 50)
    print(f"\n报告：建议 {s['recommend']:.1f}s（{s['basis']}），稳定性判定「{s['verdict']}」")
    ok_rep = s["recommend"] >= 3.5 and s["verdict"] in ("基本稳定", "偏紧", "稳定")
    print(("ok  " if ok_rep else "FAIL") + " 建议值取零风控档 3.5s 之上")
    return 0 if (bad == 0 and ok_math and ok_rep) else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="统一出网闸门：限速 / 风控 / 报告")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="各站当前间隔与最近请求")
    p_rep = sub.add_parser("report", help="从流水生成间隔建议")
    p_rep.add_argument("--since", default=None, help="只看该时间之后的流水，ISO 前缀，如 2026-11-24T10")
    p_rep.add_argument("--min-samples", type=int, default=50)
    p_tune = sub.add_parser("tune", help="人工调整某站间隔")
    p_tune.add_argument("--host", required=True)
    p_tune.add_argument("--interval", type=float, default=None)
    p_tune.add_argument("--reset", action="store_true", help="清掉自适应值，回到地板")
    sub.add_parser("selftest", help="不联网自检")

    a = ap.parse_args(argv)
    g = Guard(verbose=False)

    if a.cmd == "status":
        _cmd_status(g)
        return 0
    if a.cmd == "report":
        g.report(since=a.since, min_samples=a.min_samples)
        return 0
    if a.cmd == "tune":
        v = g.tune(a.host, a.interval, reset=a.reset)
        print(f"{a.host} → {v:.1f}s（地板 {policy_for(a.host).floor:.1f}s）")
        return 0
    if a.cmd == "selftest":
        return _cmd_selftest()
    return 1


if __name__ == "__main__":
    sys.exit(main())
