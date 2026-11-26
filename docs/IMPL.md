# 实现手册（换会话照这个执行）

> **阅读顺序**：`AGENTS.md`（红线）→ `docs/SCHEMA.md`（结构与字段，v3 已定稿）→ 本文（怎么干）。
> 本文只管「落地的顺序、文件、函数、命令、判据」，不重复解释字段含义。
> 结构上有疑问一律以 `docs/SCHEMA.md` 为准；本文与它冲突时，是本文写错了。

---

## 0. 开工第一件事

```bash
cd /z/project/maming-tongyi
npm run build                                 # 必须通过（页数随数据量变，不再固定 58）
python pipeline/netguard.py selftest          # 出网闸门自检（不联网），必须全 ok
python pipeline/check_data.py --partial       # 当前数据健康度（分批阶段用 --partial）
```

跑完看一眼 `http://localhost:4400/maming-tongyi/`（`npm run preview -- --port 4400` 起服务）。
**当前站点读 `public/data/` 分片**：结构不变，数据量随 S2–S5 跑多少变多少；
S5 不跑就没有产物，`npm run build` 会因找不到 `manifest.json` 直接失败——这是有意的，不许拿旧产物顶。

---

## 1. 现状

### 已完成（可直接用，不要重写）

| 东西 | 位置 | 说明 |
| --- | --- | --- |
| 全量公告枚举 | `pipeline/s0_enumerate_all.py` | 19,368 条标题，194 页，2s/请求，约 6.5 分钟 |
| 出马表筛选 + 地区分类 | `pipeline/s1_classify.py` | 产出 `data-src/hkjc_entries_index.csv`，956 条 |
| 公告表头解析 | `pipeline/_explore/make_demo_json.py` 的 `parse_header()` / `cn_num()` | 级别、途程、场地、赛期、马场、场次、开跑时间，**零推断** |
| JBIS 血统抓取 | `pipeline/_explore/fetch_jbis_horses.py` / `fetch_jbis_retry.py` | 检索页一次取 5 字段，15/15 命中 |
| 名称模型 + 站点 | `src/`（58 页） | 六栏名称、简称、编辑页、来源页全部就位 |
| 简体派生 | `make_demo_json.simplified()` | 逐字转换 + 长度不变式 |
| 人工图层 | `data-src/name-overrides.json` | 已被管线读取，改 `hk` 会连带重算 `sc` |
| **出网闸门** | `pipeline/netguard.py` | 限速 + 风控识别 + 自适应退避 + 流水 + 收尾报告，见 §3。**所有抓取脚本只能用它的 `Guard`** |

### 待建（本手册的正文）

| 阶段 | 脚本 | 输入 → 输出 | 状态 |
| --- | --- | --- | --- |
| 共用层 | `pipeline/common.py` | 路径 / CSV / slug / 简体派生 / 表头与名单表解析 | ✅ |
| S2 | `pipeline/s2_fetch_docs.py` | `hkjc_entries_index.csv` → `cache/docs/{race_uid}/` | ✅ |
| S2b | `pipeline/s2b_archive.py` | `cache/docs/` → `archive` 表 + `cache/archive.csv` | ✅ |
| S3 | `pipeline/s3_parse.py` | `cache/docs/` → `cache/races.csv` + `cache/entries.csv` | ✅ |
| S4 | `pipeline/s4_names.py` | S3 产物 + JBIS + 覆盖层 → `cache/names.csv` | ✅ |
| S4b | `pipeline/s4b_pedigree.py` | JBIS 血統页 + 基本情報页 → `cache/pedigree.csv` + `cache/jbis_profile.csv` | ✅ 分批跑 |
| S5 | `pipeline/s5_export.py` | 上述全部 + `db/schema.sql` → `public/data/*.json` | ✅ |
| S6 | 站点切换 | `demo.json` → 分片数据（页面结构不变） | ✅ |
| S7 | `pipeline/check_data.py` | 全量产物 → 通过/退出码 1 | ✅ |

实现与实测的偏差、以及本轮跑批状态，见 §16。

**已作废**：`s4_align_netkeiba.py`（原计划的 netkeiba 对齐）。当前口径是「只记录抓到的比赛页 + 名称」，
netkeiba 对齐属于赛果/生涯阶段，不在本轮。`s4` 这个名字改给「名称与血统」。

---

## 2. 执行时必须记住的硬约束

1. **限速与风控**（`AGENTS.md` §2.5/§2.7 是下限，违反即返工；实现与监控见 §3）

   | 目标 | 间隔下限 | 自适应上限 | 备注 |
   | --- | ---: | ---: | --- |
   | HKJC 全域（`racingnews` / `res` / `corporate` / `racing`） | ≥ 2.0s | 30s | 快请求直接 400；空壳页 = 隐性限速 |
   | `www.jbis.or.jp` | ≥ 8.0s（现用 10.0s） | 60s | 检索页 / 基本情報页 / 5 代血統页都是普通页面；503 不算风控 |
   | `db.netkeiba.com` | ≥ 5.0s | 45s | 本轮不用 |
   | `web.archive.org` 提交 | ≥ 10.0s | 180s | 串行，`--max-time 90`，按 `Retry-After` 睡 |

   间隔只能因风控信号上调，**任何情况下不得低于下限**；不许并发、不许做提速探测。

2. **抓取全量、维护分级**：任何阶段都不得出现 `if region == '日本': continue`。地区只决定顺序。
3. **不猜**：抓不到就 `NULL`，页面显示「—」；不用 0 当「没跑」，不用罗马字反推假名。
4. **名称必带出处**：写 `name` 表的每一行都要有 `source_url` 与 `decided_at`。
5. **原件不入库**：`.docx` / `.doc` / HTML 只落 `cache/`（gitignore），对外只给 IA 回放地址。
6. **人工层只读**：脚本不许写 `data-src/name-overrides.json`。
7. **改前端必须 `npm run build` 并核对产物**，不能只看 dev server。
8. **不 commit / 不 push**，除非用户明确要求。

---

## 3. 风控与自适应限速（`pipeline/netguard.py`）

**这一节是硬要求，不是建议。** 所有出网只准走 `Guard.get()` / `Guard.download()`（S0、S2、S2b、S4 的 JBIS 全部适用）。
自己写 `curl` / `urlopen` 会让限速、识别、流水同时失效——出事时既查不出原因，也回答不了「为什么被封」。
S0 / S1 是早先写的（各自 `time.sleep`），不要求重写；但**新脚本一律不许再出现裸 `curl`**，重跑旧的时顺手改成 `Guard`。

```python
import sys
from netguard import Guard, RiskControlStop

g = Guard()                       # 自动读 cache/http/state.json，接着上批的节奏跑
try:
    for row in rows:
        r = g.get(row["url"], accept_language="zh-HK,zh;q=0.9")
        if not r.ok:
            manifest.append({**row, "status": r.outcome, "signal": r.signal})
            continue
        html = r.text
except RiskControlStop as e:
    log.error("%s", e)            # 状态已落盘，换时间重跑即可
    sys.exit(2)
```

### 3.1 五条铁律

1. **间隔只能因为看到风控信号上调**，永远不会因为「跑得慢」降到 `floor` 以下。`floor` 就是 `AGENTS.md` §2.5/§2.7 的红线。
2. **单线程**。并发 2 个连接等于把 2s 变成 1s，形式上没改间隔、实质上违规。
3. **挨打就抬、抬完再撞一次就停**。风控信号 → 间隔 ×1.6 + 重试 1 次；再挨打 → 熔断 `exit 2`。不许连环重试。
4. **不换 IP、不换 UA、不绕验证码、不模拟登录**。被风控的处理是「停」，不是「绕」。
5. **不做主动提速探测**（比如从 2s 一路降到 0.5s，看在哪一档被拦）。那是拿别人的站点做压测，会把整站 IP 段拉黑。只根据真实跑批的**被动观测**调整。

### 3.2 什么算风控（判定表）

判定顺序固定：`curl` 退出码 → HTTP 状态码 → 正文特征 → 正文长度。

| 目标 | 风控长什么样（实测/已知） | `outcome` | 处理 |
| --- | --- | --- | --- |
| `*.hkjc.com` | **HTTP 400**（不是 429）。快请求的签名，重发同样参数在合规间隔下就正常 | `throttle` / `http-400` | 抬间隔 |
| `*.hkjc.com` | HTTP 200 但正文是空壳（< 600 字符的 html） | `empty` | 抬间隔 + 重试 |
| `*.hkjc.com` | HTTP 403 | `throttle` | 抬间隔，连续两次即熔断 |
| `www.jbis.or.jp` | 检索页出现「しばらくお待ちください」「アクセスが集中」页 | `challenge` / `jp-wait` | 抬间隔 |
| `www.jbis.or.jp` | **`/horse/{id}/` 的 503 是结构性的，不是风控** | `server` / `jbis-detail-503` | **不抬间隔**，直接放弃该马 |
| `db.netkeiba.com` | 403 / 503 | `throttle` / `server` | 本轮不用 |
| `web.archive.org` | 429 + `Retry-After`；或 200 但正文写着 Too Many Requests；或 `blocked-url` | `throttle` / `challenge` | 按 `Retry-After` 睡，至少 10s |
| 任意 | Cloudflare `Just a moment...` / Akamai `Access Denied` / 正文含 `rate limit` | `challenge` | 抬间隔，两次即熔断 |

`notfound`(404/410) 和 `server`(5xx) **不算风控**，它们只影响数据完整度，不该让全站降速——
把这两种也当风控，跑一天下来间隔会被无意义地顶到上限，反而更容易真被拦。

### 3.3 自适应：升得快、落得慢，绝不破地板

```
interval = floor
  风控  → interval = min(ceiling, interval × 1.6)     # 立刻生效，下一次请求就用
  成功  → streak++；每满 20 次 interval = max(floor, interval × 0.9)
```

- 各站参数在 `POLICIES`：HKJC `2.0 → 30s`、JBIS `8 → 60s`（熔断 4 次）、netkeiba `5 → 45s`、IA `10 → 180s`（熔断 3 次、冷却 60 分钟）。
- 抖动只在**向上**一侧（0–10%），所以任何时刻的实际间隔 ≥ `floor`。
- 实测曲线（`selftest` 会跑一遍）：HKJC 2.0s 起跑，连挨 3 次 400 → **8.2s**；随后连顺 240 次 → **2.3s**。
- 状态落 `cache/http/state.json`，进程被杀不丢。**换时间重跑是从「抬高的值」继续**，不会一秒回到 2s 再挨一次打。

### 3.4 熔断 / 冷却 / 续跑

- 连续风控达到 `breaker`（HKJC 5 / JBIS 4 / IA 3）→ 抛 `RiskControlStop`，脚本 `exit 2`，本批停。
- 冷却 15–60 分钟再跑（`cooldown` 只写进异常文案与流水，不自动睡——让调用方决定）。
- 断点续抓照旧生效，所以「停 → 等 → 再跑」的成本只有几条请求。
- **一天内同一站熔断 ≥ 3 次**：当天停手，把 `report` 交给用户，由人决定是换时段还是减量。不要自己想办法突破。

### 3.5 跑批时怎么盯（实时监控）

- 每 50 条跑一次 `python pipeline/netguard.py status`：看当前间隔有没有被抬高、最近 12 条的 outcome。
- stderr 每行形如 `[ok  ] 200 536ms itv=2.0s (now 2.0s) racingnews.hkjc.com/chinese/...`。
- 见到一条 `[RISK]` 先别再往下塞活，看 `status` 里间隔跳了多少；**连续两条 `[RISK]`，或间隔已被抬到起始值的 2 倍以上，主动停机**，别等熔断。
- 判断「稳不稳」看两件事：成功率是否 ≥ 98%，间隔是否在回落（`status` 里 `当前间隔` 在降 = 站点没在为难你）。

### 3.6 跑完必须做的总结（本节交付物）

```bash
python pipeline/netguard.py report                       # 全量流水
python pipeline/netguard.py report --since 2026-11-24T10 # 只看本批
```

产出 `cache/http/ratelimit-report.md`，三块内容：

1. **结论表**：每站稳定性判定 + **建议间隔** + 依据一句话。
2. **逐站明细**：请求数/成功率、结果分布、延迟 p50/p90/p99、生效间隔 min/中位/max、风控次数、连续成功最长、**间隔档 × 风控率** 表。
3. **采纳方法**（见下）。

**建议间隔的算法**（固定，不许拍脑袋）：

```
候选 = { 间隔档 b | 该档风控次数 = 0 且样本 ≥ 50 }        # 0.5s 一档
建议 = clamp(min(候选) × 1.25, floor, ceiling)
候选为空 → 退回本批最后一次生效间隔，并标「样本不足」，不许据此提速
```

**稳定性判定**：样本 < 50 条 = `样本不足`（不下结论）；风控率 ≤ 1% 且最近 200 条零风控 = `稳定`；
≤ 5% = `基本稳定`；≤ 15% = `偏紧`；> 15% = `被风控`。

**采纳方式**：把结论数值写进脚本默认 `--interval`；若要改 `POLICIES` 的 `floor`，**必须先改 `AGENTS.md`**（下限是红线，不能在代码里偷偷放松）。
本批日志记一行结论，例如：`本批限速结论：hkjc 2.5s / jbis 8s / ia 12s（report 2026-11-25）`。

> 报告结论只对**上一批**有效。站点策略会变（换 CDN、加挑战页、节假日加严），每批跑完重跑一次 `report`，不许把上个月的数值当永久参数。

### 3.7 先减请求，再谈间隔

调参之前先看能不能少发：断点续抓（`manifest.csv` 有 `sha256` 就跳）、IA 先 CDX 探测（命中就不提交，省一次 `save`）、条件请求（带 `If-Modified-Since` / `ETag`）。
956 篇 × 2 请求里重复占比越高，被拦概率越大——**少发一个请求比把间隔从 2s 调到 3s 更有效**。

---

## 4. 目标目录

```
pipeline/
├── netguard.py             ✅ 出网闸门（限速/风控/流水/报告），先读 §3
├── s0_enumerate_all.py     ✅
├── s1_classify.py          ✅
├── s2_fetch_docs.py        ⬜ 抓正文 + 附件
├── s2b_archive.py          ⬜ 提交 IA 存档
├── s3_parse.py             ⬜ 解析 docx/doc/内联表 → races.csv + entries.csv
├── s4_names.py             ⬜ 名称（港译/原文/简体/简称）+ JBIS 血统
├── s5_export.py            ⬜ 导分片 + manifest + 搜索索引
├── build_db.py             ⬜ 按 db/schema.sql 重建 *.sqlite
├── check_data.py           ⬜ 数据体检
└── _explore/               ✅ 一次性验证脚本，不进正式链路

cache/                      gitignore，全部中间件
├── docs/{race_uid}/        article.html / attach.docx
├── http/                   journal.jsonl（请求流水）/ state.json（各站间隔）/ ratelimit-report.md
├── entries.csv  races.csv  names.csv  archive.csv
data-src/                   入库
├── hkjc_entries_index.csv  956 条公告索引（S1 产出）
├── name-overrides.json     人工图层（唯一人工入口）
public/data/                入库，站点读这里（全量后）
src/                        Astro 站点
```

---

## 5. S2 · 抓正文与附件

**输入** `data-src/hkjc_entries_index.csv`（`date,region,title,url`）
**输出** `cache/docs/{race_uid}/article.html`、`attach.docx|attach.doc`、`manifest.csv`

```python
def race_uid(row) -> str:            # jra-2026-mile-championship
    """地区前缀 + 年 + 赛事 slug。同一年同一赛事只留一条（取 announce_date 最近）。"""

def fetch_article(url) -> str:       # 落 article.html，返回 html
def find_attachments(html) -> list[tuple[str, str]]:
    """返回 [(绝对 url, 'attachment')]，解析协议相对链接 //res.hkjc.com/..."""

def download(url, dest) -> dict:     # {sha256, bytes, mimetype, http_status}
```

必须处理的三件事（实测坑）：

1. **协议相对链接**：`href="//res.hkjc.com/..."`。直接拼 `racingnews.hkjc.com` 会 500。
   主机白名单：`res.hkjc.com`、`corporate.hkjc.com`、`racing.hkjc.com`。
2. **两种扩展名**：`.docx` 是真 ZIP（魔数 `504b0304`），`.doc` 是 OLE2（`d0cf11e0`）。
   `.doc` 要 `soffice --headless --convert-to docx` 转一道再解析。
3. **正文形态三种**（各约 1/3）：`.docx` 附件 / `.doc` 附件 / **内联 HTML 表格**。
   只抓附件会漏掉内联那 1/3；只抓 HTML 会把「最後宣佈」类解析成空。

断点续抓：`cache/docs/{race_uid}/` 已存在且 `manifest.csv` 有 sha256 就跳过。
**串行**，不要并发轰站；正文 `Guard.get()`、附件 `Guard.download()`，两个不同主机各自记自己的间隔。

**命令**：`python pipeline/s2_fetch_docs.py --from 0 --to 956`（每 50 条落一次日志 + `netguard.py status` 一条）
**耗时**：956 篇 × 约 2 请求 × 2s ≈ 65 分钟（不含附件重试）；**按 §3.6 的建议间隔重算，别沿用这个数**
**判据**：956/956 目录齐、每篇至少 1 个可用数据源（附件或内联表）、失败条目写 `manifest.csv` 的 `status=failed` 并单独重试

风控口径（§3）：撞 `RiskControlStop` 就 `exit 2`，已完成的目录天然就是检查点；
**重试只对 `server` / `net` / `empty` 做**，`throttle` 交给 Guard 处理，脚本里不要自己写 `while True` 重试。

---

## 6. S2b · 提交 Internet Archive

**输入** `cache/docs/*/manifest.csv`
**输出** `archive` 表 + `cache/archive.csv`

```python
def cdx_probe(url) -> str | None:
    """CDX 查已有快照，命中返回 14 位 ts。命中就不提交。"""

def save_page(url, timeout=90) -> str:
    """GET https://web.archive.org/save/{url}，跟随 302，取 location 里的 ts。
       禁止用 POST /save（匿名 401）。"""
```

- 间隔 **≥10s**，单线程，同样走 `Guard`（`host=web.archive.org` 的政策是 10s 起、上限 180s、熔断 3 次）。
  956 篇约 1,430 个请求 ≈ 12 小时；先跑日本 235 篇（≈3 小时）。**跑完必须出 `report`**——IA 是全站最容易被 429 的，
  下一批的间隔应当以报告为准，而不是固定 10s。
- 归档对象按形态决定：内联表型只提 `article`；附件型 `article` + `attachment`。
- `capture_method`：`save_get` / `spn2_api` / `cdx_existing`。
- `401` = 匿名口径被收紧 → 整批停、切 SPN2（需账号），**不许记成「未存档」**。
- 5xx/超时 → `pending` 重试（最多 3 次）；4xx/`blocked-url` → `failed`，保留原链接。
- 页面显示规则见 `SCHEMA.md` §6：有存档给回放地址，没有就明说，不渲染成错误。

**判据**：`archive` 表内 `success / (success + failed) ≥ 0.9`，且每条 `snapshot_url` 回读 200。

---

## 7. S3 · 解析出赛事与名单

**输入** `cache/docs/`
**输出** `cache/races.csv`（956 行）、`cache/entries.csv`（约 1.3 万行）

```python
def parse_header(text) -> dict:      # 已有，直接搬：grade/grade_cn/distance/surface/race_date/venue/session/race_time
def read_docx_rows(path) -> list[dict]:
    """docx 第一张表的表头 = 标题行；数据行字段见下。"""
def read_inline_table(html) -> list[dict]:
    """内联 HTML 表格，列序与 docx 相同。"""
def split_name(raw) -> tuple[str, str | None]:
    """'Embroidery (JPN)' → ('Embroidery', 'JPN')。后缀即产地，origin_basis=docx_suffix。"""
```

行字段（与 `entry` 表一一对应）：

`no` 马号 / `draw` 档位 / `gear` 装备 / `horse_raw` 马名 / `jockey_zh` `jockey_en` /
`trainer_zh` `trainer_en` / `lb` 负磅 / `rating` 评分 / `sex` / `age`

三个必须做的归一：

1. **`race_uid`**：`{prefix}-{year}-{race_slug}`，`prefix` 取地区（`jra` / `hkjc` / `aus` …）。
2. **`horse_uid`**：`{国家小写}-{slug(en)}`，例 `jp-embroidery`、`fr-erdenali`。
   slug 规则见 `make_demo_json.slug()`（NFKD → ascii → 非字母数字转 `-` → 小写 → 压连字符）。
3. **`person_uid`**：`j-{slug(en)}` / `t-{slug(en)}`，**必须带角色前缀**，否则同名骑练撞键。

**判据**：
- 956 场全部有 `race_date` 和 `distance`；
- 每场 `entry` 行数 ≥ 8，合计 ≥ 1.1 万；
- `sex` 全部落在 `M/F/G`；`lb` 全部落在 100–130 之间（超范围说明列串了）；
- 抽 20 场人工核对：马号、负磅、评分、骑练姓名与附件一致。

---

## 8. S4 · 名称与血统

**输入** `cache/races.csv` + `cache/entries.csv` + JBIS + `data-src/name-overrides.json`
**输出** `cache/names.csv`（`entity_type, entity_uid, field, lang, value, source_url, decided_at`）+ `cache/aliases.csv`

顺序**必须**是：

```
港译 hk（公告） → 原文 original（日本马取 JBIS 馬名，其他国籍 = 拉丁名）
→ 覆盖层改 hk / own → 重算 sc → 覆盖层写 alias → 写 alias 撞车检查
```

```python
def simplified(text) -> str | None      # 逐字繁转简，len 必须相等，否则抛错
def apply_override(uid, names, overrides) -> dict
def jbis_lookup(en: str, birth_year: int) -> dict | None
```

JBIS 口径（**2026-11-25 修订**：三个页面各管一件事，别再照抄旧结论）：

| 页面 | URL | 给到什么 | 用在哪 |
| --- | --- | --- | --- |
| 検索結果 | `/horse/result/?keyword={name}` | 馬名（片假名）/ 10 位 id / 生年 / 父 / 母 / 調教師 | S4 定 id 与日文名 |
| 基本情報 | `/horse/{id}/` | 性別 / **生年月日** / 毛色 / 産地 / 馬主 / 生産牧場 / 調教師 | S4b 填生日与毛色 |
| 5 代血統 | `/horse/{id}/pedigree/` | 父系 31 格 + 母系 31 格（名字/性別/生年/毛色/id）+ ファミリーNo + クロス | S4b 出整张血统图 |

- 旧结论「`/horse/{id}/` 会 503，只能打检索页」**已过期**：11-25 复测两个页面都是 200，
  435 次请求零风控。503 仍按 `server/jbis-detail-503` 特判（不抬间隔），只是不再当作「打不开」。
- 消歧：`生年 = 公告年 − 年龄`，避免 `Sixpence` vs `Sixpence(IRE)`；同页 20 条，不做翻页。
- 间隔 10s（走 `Guard`），串行，一匹最多 3 个请求；抓不到就留空，不按罗马字反推假名。

**简称的三条实现要求**（容易漏）：

1. 只从覆盖层读，**不派生**。没有就 `[]`，页面不显示简称行。
2. 同一实体去重、保序；`seq` 按录入顺序（不是字母序）。
3. 撞车检查：同一 `text` 被两个 `entity_uid` 使用 → 写 `review_queue(kind='alias')`，
   两边**照常展示**，不做自动消歧。

**判据**：日本马 `pedigree` 覆盖率 ≥ 95%；全部 `hk` 都有 `sc` 且 `len(sc)==len(hk)`；
`names.csv` 每行都有 `source_url` + `decided_at`；`alias` 全部来自覆盖层（数量对得上）。

---

## 9. S5 · 导出

**输入** SQLite + 覆盖层
**输出**

```
public/data/
├── manifest.json                 v/generated_at/counts/curation/shards
├── horses/shard-000.json         每片 500
├── races/shard-000.json          每片 500
├── persons/shard-000.json        每片 500
└── search/name-00.json …         名称检索索引，按词首字符分片
```

要求：

1. **每个文件带 `_meta`**：`schema` / `updated` / `count`。
2. **分片是存储形态，不是路由**：仍是一实体一 URL，`uid` 直接进路径。
3. `sc` 在这一步算，**不落库**。
4. `curation` 的覆盖范围必须进 `manifest.json`（哪些地区校过、哪些没有，公开可查）。
5. `search/name-*.json`：`{归一化名称 → {kind, uid}}`，键包含 原文 / 英文 / 港译 / 简体 / 自译 / **简称**，
   归一化 = lower + 去空白 + 繁简双向展开（简体由 `sc` 提供，不需要跑两遍转换）。

**判据**：产物总量 < 60 MB；`manifest.counts` 与库内 `SELECT COUNT(*)` 一致；
任取 5 个 uid 能在分片里找到且字段完整。

---

## 10. S6 · 站点切换到全量

页面结构**不动**，只换数据源。三处改动：

1. `src/lib/demo.ts` → `src/lib/data.ts`：从 `import demo.json` 改为读分片，
   导出同名函数（`archive` / `nameCols` / `primaryName` / `aliasText` / `searchKey` / `href`），
   页面 import 换成新模块即可。
   注意：仓库里已有一个**未被引用**的旧 `src/lib/data.ts`（`dataUrl` / `loadJson` 那套 fetch 封装），
   迁移时直接替换掉它，不要留两个同名入口。
2. 索引页：**不能一次渲染 2 万行**。首屏 200 行（按类型分组截断），
   搜索改为 `fetch('search/name-*.json')` 后客户端渲染结果行；
   `?q=` 分享链接语义保留。
3. 路由规模：`getStaticPaths()` 从分片遍历。2 万页构建约 10–20 分钟，属可接受；
   若超时，先减到 `curation=curated` 的日本马 + 全部赛事，其余按需增量。

**判据**：`npm run build` 通过；索引页首屏 < 1.5 s；随机点 10 个马匹/赛事/人物页，名称六栏与简称均正确；
`/edit/` 改简称后，页面提示「已改 N 项」且 patch 里有 `alias` 数组。

---

## 11. S7 · 数据体检 `check_data.py`

不通过就 `exit 1`，不许「先上线再说」：

| 检查 | 阈值 |
| --- | --- |
| 赛事数 vs 索引 CSV | 必须相等（956） |
| 每场名单行数 | ≥ 8 |
| `entry.lb` 区间 | 100–130，超范围即错（列错位） |
| `name` 缺 `source_url` | 0 条 |
| `len(sc) != len(hk)` | 0 条 |
| `sc` 在覆盖层里出现 | 0 次（简体的唯一来源是派生） |
| 简称来源 | 100% 来自 `name-overrides.json` |
| 简称撞车 | 全部在 `review_queue` 里有记录 |
| 日本马血统覆盖 | ≥ 95% |
| 断链 | `href()` 拼出的站内链接 100% 有对应页面 |
| 整批为空字段 | 任何字段整列为空都要报错并说明原因 |
| 限速流水存在 | `cache/http/journal.jsonl` 非空（没流水 = 有脚本绕过了 `Guard`） |
| 低于下限的请求 | 0 条（任何 `interval < floor` 都是违规，直接失败） |
| 各站风控率 | ≤ 5%，超了打印 `report` 摘要并要求先调间隔 |
| 熔断次数 | 记录在案；同站 ≥ 3 次需人工确认后才允许继续跑 |

---

## 12. 验收清单

- [ ] 956 场赛事页、全部马匹页、全部人物页可访问，无 404
- [ ] 索引页九列：类型 / 原文 / 英文 / 港译 / 简体 / 自译 / 简称 / 国籍 / 状态
- [ ] 五段筛选：全部 / 马匹 / 赛事 / 骑手 / 练马师（计数与行数一致）
- [ ] 搜索：任一种写法都能命中（`巧繡` / `巧绣` / `M.傲` / `Embroidery` / `マイル` / `Lemaire`）
- [ ] 同形降级生效：`hk === sc` 时简体转灰；`original === en` 时英文转灰
- [ ] 详情页名称栏 5 列 + 简称行（无简称时整行不渲染）
- [ ] `/edit/` 能改港译 / 自译 / 简称，输出 `name-overrides/v2` 片段；恢复原值后「已改 0 项」
- [ ] 改完 `hk` 重建，`sc` 自动跟着变，且页面无残留
- [ ] `/sources/` 与实现一致（分级、维护范围、存档政策三块）
- [ ] `python pipeline/check_data.py` 通过
- [ ] `python pipeline/netguard.py selftest` 全 ok（不联网）
- [ ] 全量跑批结束后 `cache/http/ratelimit-report.md` 已生成，结论表三项（稳定性/建议间隔/依据）齐全
- [ ] 采纳后的间隔已写进脚本默认值 + 本批日志，且流水里无低于下限的请求

---

## 13. 已知坑（都是实测，别再踩）

1. **JBIS 详情页曾经 503**（旧记录，84s 后 503）。11-25 复测已恢复：基本情報页与血統页都 200，
   S4b 直接用；检索页仍是最省的一次请求（一次五字段）。
2. **同名马**：`Sixpence` vs `Sixpence(IRE)`。用「生年 = 公告年 − 年龄」消歧。
3. **IA 匿名只能 GET `/save/{url}`**；`POST /save` 直接 401。302 同步返回，没有 job 轮询。
4. **附件协议相对链接** + 错误主机 → 500。
5. **`.doc` 是 OLE2**，`python-docx` 读不了，先 `soffice` 转 docx。
6. **正文三种形态**，只处理一种会漏 2/3。
7. **Windows 路径**：脚本内一律 `pathlib.Path`；不要用 `.ps1` 写非 ASCII 路径。
8. **侧栏图标**：Windows 字体回退会把图标渲染成方框，导航用纯文字。
9. **Tailwind v4**：色值只在 `global.css` 的 `@theme` 定义，页面里不写 hex。
10. **简体不能用 `zh-cn`**：那会做词汇替换（`一哩 → 一英里`），必须 `zh-hans` + 长度校验。
11. **`sc` 与 `alias` 都不能在页面里算**：页面只读数据，计算全在管线，否则覆盖层一改就打架。
12. 演示站 `python -m http.server` 不支持子路径，一律 `npm run preview`。
13. **HKJC 的限速信号是 400，不是 429**。同一 URL、合规间隔下重发就好；把它当「参数错」去改代码，会改坏正常的 URL。
14. **JBIS `/horse/{id}/` 的 503 不是风控**，`netguard` 已特判（`server/jbis-detail-503`），
    不会因此把间隔顶上去。11-25 起该页可访问，特判保留只是兜底。
15. **血统网格不能用 31 行**：`grid-row: 1 / 16.5` 是非法值，浏览器会**整条丢弃**，
    格子全挤到顶部。总行数取 32（2 的幂），每代跨度才是整数。
16. **`empty`（200 但正文 <600 字符）也是限速**。不判它，抓到的会是一堆空壳页，解析阶段才发现，白跑一遍。
17. **IA 429 会带 `Retry-After`**，可能是几分钟到几小时；照它睡，别自己按 10s 硬撞，撞急了会直接一天不给提交。

---

## 16. 落地记录（2026-11-24 实现 + 小批量实测）

**这一节记录「实现时跟本文哪几句不一样、为什么」。改口径先改这里，再改代码。**

### 16.1 数字口径三处修正

| 本文原话 | 实测 | 处理 |
| --- | --- | --- |
| 956 篇公告 = 956 场赛事（§7 判据、§11 体检） | 同一赛事先发「宣佈出賽馬匹名單」（只有馬名/練馬師），几天后再发「最後宣佈…」（含马号/磅重）。956 篇按 `race_uid` 去重后是 **657 场公告**；而一篇公告又含多场（见 16.2），展开后是 **2,883 场** | S2 按 uid 去重，只抓最新一条；被覆盖的旧公告记 `cache/docs/duplicates.csv`（不入 races，存档阶段仍可提交）。`check_data` 判据是**库内场数 ≥ 去重后的公告数**（657），少于它才算丢场 |
| `entry.lb` 必须落在 100–130 | 日本让赛实测 131 / 132 磅属正常（`冠軍盃賽馬日`） | 体检区间放到 **100–140**；超范围仍判失败（列错位的信号） |
| 每场名单 ≥ 8 匹 | 「賽馬日」型公告里的小场次实测最少 **5 匹** | 硬门槛 ≥4（明显是列错位才失败），<8 匹只告警并列出来 |

### 16.2 一天多场的公告怎么落（**口径已改：一张表 = 一场**）

「XX賽馬日 – 最後宣佈…」一份公告里有 4–10 张表，是当天不同场次（途程/班次/开跑时间都不同）。
最初按「一行公告 = 一场赛事」只取一张表，实测 183 篇 docx 共 **890 张名单表**、单表公告只占 48 篇，
等于丢掉 3/4 的场次和名单 —— 这与站点目的（把马名撒得尽量广）相反，因此改为**一张表 = 一场**：

- 正赛那张（表内赛事名与公告标题同名，或公告只有一张表）**沿用公告 uid**，对外 URL 不变；
- 其余各表按表内赛事名另起 uid：`{prefix}-{year}-{mmdd}-{slug}`（副场名重复度极高——「三歲以上一捷馬賽」
  几乎每个赛马日都有——所以带月日；同日同名再用开跑时间消歧，如 `-1145`）；
- 同一场赛事被两份公告各覆盖一次时（「XX賽馬日」含正赛表 + 单独的「XX」），两份会产出同名 uid：
  解析期先加后缀避免主键撞车，`dedupe_same_race()` 再按「赛期 + 场次 + 途程 + 马场」并成一场，
  优先保留**不带后缀、且单表的那份**（`parse_report.csv` 记 `duplicate-of:`）；
- `races.csv` 的 `table_count` / `table_index` / `sheet_name` 保留，用来回溯「这场来自公告的第几张表」。

实测结果：654 篇可解析公告 → **2,883 场**（多表展开 2,661 场）/ 40,660 行名单。

### 16.3 解析时踩到并修掉的四个坑

| 现象 | 根因 | 处理 |
| --- | --- | --- |
| 附件 `.docx` 被静默丢弃，公告变成「无可用源」 | 文件名带撇号（`20211002_Prixdel'ArcdeTriompheDay_FinalDecl_OC_C.docx`），老正则 `href=["']([^"']+)["']` 在撇号处截断 | 改为按**定界符**匹配：`href\s*=\s*(?:"([^"]*)"\|'([^']*)')`，值内部允许另一种引号；`href` 里的换行也一并清掉（`/\n\n20221111_...DOC` 曾让 curl exit 3） |
| 内联表把页面导航当成表头，解出假途程（墨爾本盃 → 1650 米） | 内联公告没有信息行时，`html_tables_with_ctx` 的上下文是整页导航 | `clean_info()`：含「所有新聞消息/選擇其他新聞」或长度 >240 一律当空；全文兜底**只对附件**做，不再拿网页文字兜底 |
| 「暫定宣佈」类公告整篇没有赛期 | 表内只有赛名，无信息行 | 同一份公告 = 同一个赛马日：`race_date/venue/session` 在各表之间互补；**途程不互补**（每场不同）。整篇都没有的留空，写进体检告警，不猜 |
| `.doc` 转换 384 篇、每篇约 10 秒 | Word COM 串行 | 转换结果缓存在 `attach.converted/`，重跑直接命中（全量重跑从 34 分钟降到 47 秒） |

### 16.4 索引页搜索

`search/all.json`（归一化名称 → `{kind, uid}`）+ `search/rows.json`（渲染用的紧凑行）在**首次输入时懒加载**；
首屏只 SSR 200 行（马 80 / 赛事 60 / 人物 60，不足则补满）。

`search/name-XXXX.json`（按词首字符分片）**默认不再生成**：它是 `all.json` 的第三份副本，
2 万匹马就多出 3,051 个文件、11 MB，而客户端只取 `all.json + rows.json`。要按首字懒加载时加 `--char-shards`。

### 16.5 本轮跑批状态（2026-11-25 全量）

- **S2**：657 场全部抓完（ok 654 / partial 3），1,426 次请求**零风控**；报告 `cache/http/ratelimit-report.md`
  结论：四个 HKJC 子站稳定，建议间隔 2.5s（现用 2.0s，保持）；JBIS 稳定，建议 10.0s（现用 8.0s）。
  残余 3 条：`chn-2019-從化` 只有 PDF、`jkr-2023 沙田` 是「系统故障说明」非名单公告、
  `jra-2022-短途馬錦標` 附件是 `starters.zip`（6 个 docx，暂未展开）。
- **S3**：2,883 场 / 40,660 行；缺赛期 205、缺途程 192（集中在「純名單」型公告，源站确实没给）。
- **S4**：80,660 条名称（港译 28,984），21,535 匹马 / 4,566 位人物；港译多写法 27 条 → `review_queue(kind='name')`
  （原先只打提示、名会静默丢，已改为入队等人工判定）。
- **S5**：47.6 MB（16 个分片 + manifest + 两份搜索索引），建站 **28,987 页**、Astro 65–147 秒。
- **体检**：11 通过 / 3 告警 / 0 失败。磅重按地区放宽上限（德/英/爱/法 168、澳/美 165），
  个别越界（≤0.1%）降为告警——实测 92 行 141–152 磅全是欧洲让赛真实磅制，不是列错位。
- **JBIS**：436/3,013 = 14.5%（本批 300 匹 435 ok / 1 weak，零风控）。剩 2,577 匹 × 10s ≈ 7 小时，
  继续 `--jbis-limit 300` 分批即可；每 50 匹落一次 `cache/jbis.csv`（原先只在收尾写，中途崩掉整批白跑）。
- **IA**：仍只有 4 条（3 条 `cdx_existing` + 1 条 `save_get`），全量约 1,430 请求 ≈ 12 小时，未启动。

**调地板会误伤历史流水**：把 HKJC 2.0→2.5、JBIS 8.0→10.0 之后，体检拿「当前地板」去判历史请求，
立刻误报 1,422 条「低于下限」。已让 `netguard` 在每条流水里记 `floor`（当时的地板），
`check_data` 优先用记录值；没有该字段的老流水按「半地板」兜底（真破线才会触发）。

### 16.6 命令速查

```bash
python pipeline/s2_fetch_docs.py                   # 全量（日本队列优先），断点续抓
python pipeline/s2_fetch_docs.py --only-failed     # 只重跑非 ok
python pipeline/netguard.py status                 # 每 50 条看一眼
python pipeline/netguard.py report                 # 跑完必做，结论写进下一批间隔
python pipeline/s2b_archive.py --kind article --limit 20
python pipeline/s3_parse.py                        # .doc 转换已缓存，重跑约 1 分钟
python pipeline/s4_names.py --no-jbis              # 只出公告名称（秒级）
python pipeline/s4_names.py --jbis-limit 200       # 分批抓 JBIS，已有缓存会跳
python pipeline/build_db.py && python pipeline/s5_export.py
python pipeline/check_data.py                      # 分批阶段加 --partial
npm run build
```

### 16.7 首轮上线反馈修掉的四处（2026-11-25）

**1. 练马师 tab 点开是 0 条。** 首屏 200 行按 `kind`（horse/race/**person**）分组截断，60 条人物全给了骑手，
练马师一条没进首屏；而点 tab 只过滤首屏那 200 行，所以等于空表。
→ 当时先按 `filter` 分配配额（首页 70/45/42/43、改名页 80/30/45/45）止血；
**这个补丁已被 §16.8 的分页取代**——配额只解决「首屏有没有」，解决不了「后面 28,386 条怎么看到」。

**2. 骑手名带着 `(-3)`，同一个人变成三个。** 告示板写法 `Adam Caffrey (-3)`、`Logan Bates (a) (-3)`：
`(-N)` 是减磅、`(a)` 是見習騎師，都不是名字。按整串生成 `j-{slug}` 会让同一骑师因不同场次减磅拆成多个实体
（实测 1492 行、1788 → 1391 人）。
→ `common.strip_claim()` 从右往左剥三层标记，返回 `(干净名, claim)`；`entry.claim_lb` 存减磅
（负数减磅、正数超磅，NULL = 未标），赛事页负磅后跟一个小号数字（`152 -11`）。
同批还清掉 `-` / `—` / `N/A` 这类占位符（16 行骑手、16 行练马师），它们原先造出了空壳人物页。

**3. 首页那段「简体 / 简称」长篇说明删掉。** 它是解释性的，不是数据；页头 `lead` 一行带过即可。
简体与简称的口径不变：简体由港译逐字转写（派生、不可单独编辑、不进分级），简称多对一、人工维护、机器不生成。

**4. 国籍只有中文，且不能按国籍查。** 马匹 `origin` 本来就有 ISO 三字码（21,509/21,535 = 99.9%），
但索引页只显示中文，检索索引里也搜不到。
→ 国籍列改为「中文 + 代码」（`日本 JPN`，复用 `.orig` 样式）；`search/rows.json` 每行加 `cc`；
索引页加国籍下拉（按马匹数量降序，`爱尔兰 IRL（4993）` 这种写法）；
搜索框直接输入 `JPN` / `ARG` 也命中（索引键里没有国家码，走 `rows.json` 的 `cc` 回退）。
深链同时支持 `?q=`、`?kind=`、`?nat=`，可组合分享——这也是验收时用 headless 逐条核对的入口。

**验收方式**（不必手点）：`chrome --headless --dump-dom "...?kind=trainer"` 后数可见行，
实测 `?kind=trainer` → 43 行 / 总数 2777、`?nat=JPN` → 300 行 horse / 总数 3013、`?q=Caffrey` → 1 条 `j-adam-caffrey`。

### 16.8 分页（2026-11-25）

**问题**：索引页与改名页都只渲染首屏 200 行，其余「按关键词现查」——不知道名字就看不到，
等于 28,386 条数据没有入口。

**做法**：一页 200 行，SSR 输出第 1 页，客户端在全量上切片。

- **顺序必须只有一份**。`pipeline/s5_export.py` 按五条队列加权轮转写 `rows.json`
  （curated 马 3 / 其他马 2 / 赛事 1 / 骑手 1 / 练马师 1），`src/lib/data.ts` 用同一组
  `WEIGHTS` 生成 SSR 首屏。两边不一致，第 2 页就会和首屏重叠或跳行。
  实测校验：`dist/index.html` 的 200 个 `data-uid` 与 `rows.json[0:200]` **逐条相等**，
  第 2 页 = `[200:400]`、末页 = `[28400:28586]`，无重复无缺口。
- 加权轮转的理由：直接拼接时首屏全是马（赛事要翻 15 页才出现）；纯 1:1 又把维护队列
  稀释成每页 40 条。现在是 125 马 / 25 赛事 / 25 骑手 / 25 练马师。
- **改名页不做交错**，按「curated 马 → 其他马 → 赛事 → 骑手 → 练马师」拼接：那是干活的地方，
  先把 3013 匹待校订马铺完。客户端把 `rows.json` 按同一规则重排（分区是稳定的，顺序不变）。
- **改动的持久化**：翻页会重建 DOM，原先的改动会跟着消失。改成草稿层——
  `drafts: Map<uid, {hk, own, alias}>` + `base: Map<uid, 原值>`，输入即入草稿，
  重绘时回填、按 uid 比对生成 patch。浏览器实测：改名 → 翻到第 2 页 → 回第 1 页，
  输入仍在、计数仍为 1、保存只输出那一条。
- **首次翻页的坑**：首屏是 SSR 出来的，客户端 `view` 还是空数组，`Math.ceil(0/200)=1`，
  于是第一次点「下一页」原地不动（看起来像按钮坏了）。→ `goto()` 先 `primeView()`
  把全量视图算出来再跳。这个 bug 只有真点一次才会暴露，静态检查查不出来。
- `rows.json` 7 MB，只在**首次翻页 / 筛选 / 搜索**时加载；鼠标落到翻页键上先预取
  （`mouseenter` + `once`，静默失败）。无筛选且在第 1 页时完全不请求。

**验证**（headless 逐条，不靠手点）：`?page=2` → 200 行、201–400；`?page=143` → 186 行、28401–28586；
`?kind=trainer&page=2` → 200 行全 trainer、第 2/14 页；`?q=Caffrey` → 1 条。
按钮链路 1 → 2 → 143 → 142 与 `disabled` 状态均正确。

### 16.9 第二轮反馈：档案三行 + 血统图 + 血统检索（2026-11-25）

**① 原文列的「深浅不一」不是数据差异。**
把截图按原尺寸取像素，逐行统计原文栏的暗像素：18 行的文字色都是 `#171717`（灰度均值 61 ± 4），
没有第二种颜色、也没有第二套 class。深浅是在聊天窗口里被缩放显示时，拉丁细笔画与日文粗笔画
抗锯齿程度不同造成的错觉（`A Shin Fencer` 和 `A Shin Lucano` 同一行样式，缩略后一个深一个浅）。
结论：不改样式；真要消掉这种错觉，只能把字号调大或整列加粗，反而伤可读性。

**② 日本马的原文为什么是英文名。**
`original` 的口径没变（日本马取 JBIS 馬名，其他地区取拉丁名），问题在**覆盖率**：
上一轮 JBIS 只跑了 436/3013 = 14.5%，剩下的全落到 `en` 兜底，看起来就像「一律用英文当原文」。
这一轮把补全拆成独立阶段 **S4b**，一匹马最多三请求、10s 地板、断点续跑：

```bash
python pipeline/s4b_pedigree.py --limit 300     # 检索页 → 基本情報页 → 5 代血統页
python pipeline/s4_names.py --no-jbis           # 补完再重算名称（原文换成日文名）
```

成本要提前说清：`3013 × 2 请求 + 2577 次检索 ≈ 8600 请求 × 10s ≈ 24 小时`连续跑。
这是限速地板换来的，不接受「并发跑一下」的变通；分几天跑完，页面按已抓到的部分先显示。

**③ 档案只留三行。** 产地 / 性别（右侧带毛色）/ 出生日期（`2023.03.03`，只有年就写「2023 年生」）。
练马师、骑手、本场号位是「某一场」的事实，不是马的属性，留在赛事名单里看——马页不再重复。

**④ 血统图照 yunji-web 的做法。** `pedigree_node` 一行一格（62 格/匹，父系 31 + 母系 31），
导出时还原成 `tree.{父,母}[5][2^g]`，页面用 CSS Grid 画：5 列对应 5 代，格子按
`grid-row: start / end` 向下二分，牡浅蓝 / 牝浅粉，空位画斜纹，1-3 代带毛色与 JBIS 直链。
两个坑写进 §13 了：**总行数必须是 32（2 的幂）**，31 会让 2 代跨度变成 16.5 这种非法值，
浏览器整条丢弃 `grid-row`，整张图挤到顶部；深代（4-5 代）只留名字与生年，
否则 3013 匹马的分片会从 22 MB 涨到 30 MB 以上，逼近 60 MB 上限。

**⑤ 检索支持父 / 母 / 母父 / 父父。**
名称索引是「键 → 单个实体」，一个种马名挂几百匹产驹，塞不进去，所以 `search/all.json`
另开一节 `ped: {父: {名字: [马 uid…]}, 母: …}`；页面加一个「全部字段 / 名称 / 父 / 母 / 母父 / 父父」
下拉，深链 `?field=父&q=キセキ`。选中血统字段时自动切到「马匹」类型（别的类型必然空表）。
实测：`?field=父&q=キセキ` → 1 条（進路 / Access），`?field=母&q=トリニティプレイス` 同一条。

**验证记录**：`build_db` → `xid 453 · 血统格 1302 · 血统页 21`；`s5_export` 产物 50.2 MB；
`check_data --partial` 通过 13 / 告警 4（血统覆盖、出生日期、磅重越界、小场次）/ 失败 0；
headless 截图核对 `/horse/jp-access/`：档案三行、血统 5 列 62 格、父父=ルーラーシップ、母父=マンハッタンカフェ。

---

## 14. 建议的提交粒度

1. `feat(pipeline): netguard 出网闸门（限速/风控识别/自适应/流水/报告）`——**单独一个 commit，先落地**
2. `feat(pipeline): s2 抓取 + s2b 存档`（含 `manifest.csv`、`archive` 表）
3. `feat(pipeline): s3 解析 docx/doc/内联表 → races/entries`
4. `feat(pipeline): s4 名称与血统（含 sc 派生、alias 套用）`
5. `feat(pipeline): s5 导出分片 + manifest + 搜索索引`
6. `feat(web): 站点切全量数据源，索引领 200 行 + 搜索分片`
7. `chore: check_data.py + 文档更新`

（按 `AGENTS.md` §0：commit 只在用户明确要求时做，push 永远不做。）

---

## 15. 命名对照（中文 ↔ 代码）

| 中文 | 代码 | 说明 |
| --- | --- | --- |
| 原文 | `original` | 日文片假名 / 拉丁名本身 |
| 英文 | `en` | 拉丁拼写 |
| 港译 | `hk` | HKJC 公告译名（L1） |
| 简体 | `sc` | `f(hk)`，派生，不入库、不可编辑 |
| 自译 | `own` | 人工拟名（L3） |
| 简称 | `alias[]` | 人工简写，多对一，不是名称 |
| 骑手 | `jockey` / `role_cn = 骑手` | 术语统一用「骑手」，不用「骑师」 |
| 练马师 | `trainer` | |
| 已校订 | `curation = curated` | 日本籍 |
| 待维护 | `curation = pending` | 其他地区 |
| 待接入 | 页面态 | 字段结构在、数据未到 |

### 16.10 全量血统补完（2026-11-26 17:15）

`s4b_loop.py` 串了 8 批（每批 400 匹），22 小时 38 分跑完，**3013/3013 = 100%**，全程 0 风控、0 失败请求。

| 项 | 结果 |
| --- | --- |
| `cache/jbis.csv` | 3013 行全部 `ok`/`weak`，无 `no-match` |
| `cache/jbis_profile.csv` | 3013 行，其中 3012 有 `birth_date` |
| `cache/pedigree.csv` / `pedigree_node` | 186,799 格，平均 62.0 格/匹 |
| 名称 | 3003 匹原文为片假名；另 10 匹 JBIS 的「馬名」本身写拉丁（如 `Crowns Major(JPN)`），照抄不反推 |

**体检判据修正**：血统图原按「每匹 62 格」判失败，但这 10 匹里有 2 匹（`jp-duncan`、`jp-fighter`）在 JBIS 原页上就有空位——1950–60 年代老马祖先不详，解析时无名格不入库，56 格是**源数据**不是切错行。改判据为结构判定：两侧各有第 1 代、且 `MAX(depth) ≤ 5` 才失败；格数不足 62 降为告警并列出样例。

**产物超预算（待处理）**：导出 64.26 MB，超过 §9 定的 60 MB——`horses` 36.8 MB（其中 `pedigree` 字段 10.7 MB）、`races` 13 MB、`search` 12 MB。单文件最大 2.76 MB，离 25 MB 单文件上限还很远，所以不影响上线；要回到预算内，应把这批**只供构建用**的分片（马/赛/人的明细）移出 `public/`，页面是 SSG、浏览器运行时只读 `rows.json` 与 `search/all.json`。

### 16.11 首页默认马匹 + 按名栏检索 + chip 随结果变（2026-11-26）

1. **默认落在「马匹」**：SSR 首屏直接输出 `rows.json` 里 `f === 'horse'` 的前 200 条（与客户端同一套过滤顺序，逐条相同），
   页脚 1–200 / 21535、共 108 页；URL 不再默认带 `?kind=`。首次进入只要不碰筛选，仍然不拉 7 MB 的 `rows.json`。
2. **检索按名栏收敛**：`search/all.json` 升到 `search/v3`，每个键从 `{kind, uid}` 变成 `{kind, uid, m}`，
   `m` 是位掩码（原文 1 / 英文 2 / 港译 4 / 简体 8 / 自译 16 / 简称 32）。页面下拉可选
   全部字段 / 只查名称 / 原文 / 英文 / 港译 / 简体 / 自译 / 简称 / 父 / 母 / 母父 / 父父。
   不再为六个名栏各存一份索引，体积只涨 0.7 MB。
3. **多词 AND**：关键词按空格切词，每个词都要命中（`norm` 去空白后子串匹配，「A Shin」= a ∧ shin）；
   繁简双向展开仍在导出侧生成键，实测「巧绣」在「港译」栏能命中「巧繡生花」。
4. **chip 随检索变**：先算「关键词 + 国籍 + 字段」的全量（不含类型），再拿它同时喂 chip 数字和当前列表，
   例如「父 = ディープインパクト」时 chip 是 全部 159 / 马匹 159 / 赛事 0 / 骑手 0 / 练马师 0。
5. **浅色格不是缺数据**：简体格在「简体 = 港译」时画浅（港译本来就是简体字，逐字转写后没有差别）；
   英文格在「英文 = 原文」时画浅（非日本马的原文就是拉丁名）；`—` 是空栏。三者都表示「这一格没有独立信息」。

### 16.12 去掉「变浅」、性别用血统图配色、血统图改传统 5 代表（2026-11-26 19:00）

1. **不再用浅色表达「这一格没有独立信息」**。英文 = 原文、简体 = 港译时原本把字画成 `#c9c9c9`，`—` 也是浅灰；
   实际读起来像「缺数据」，容易误判。现在这几栏一律用正文色（`.en-cell / .sc-cell / .alias-cell` 改成 `color: inherit`），
   表格里所有 `dim` 分支全部删掉。要判断「有没有独立信息」，看两栏文字是否相同即可。
2. **性别改用血统图那套蓝 / 粉**：`.sex-m` 背景 `#ccf0fd` 字 `#1a5c8a`，`.sex-f` 背景 `#fddbd5` 字 `#ab3832`。
   马页「档案 · 性别」与赛事名单「性龄」都换成色块。注意 `sex_cn` 的取值是 **雄 / 雌 / 骟**（不是牡 / 牝），
   判色要用 `horse.sex` 的 `M/F/G`，拿「牝」字面比会永远落到蓝色。
3. **血统图改成 yunji-web 那张传统表**：一张 6 列网格（本体 + 1–5 代）× 32 行，
   父系占 1–16 行、母系占 17–32 行，每代向下二分（16 / 8 / 4 / 2 / 1 行），本体在第 1 列跨满 32 行并纵向居中。
   格内两行：第一行「名字（产地）+ 年份右对齐」，有毛色的（1–3 代）改成名字一行、`年份 · 毛色` 一行；
   配色照抄（牡 `#ccf0fd` / 牝 `#fddbd5`，文字 `#1a5c8a` / `#ab3832`，网格线 `#e5e5e5`），表头只留 FNo / クロス + 蓝粉色例。
   去掉原来的「父系 / 母系」两块分栏、代次标尺和左侧竖排标题。
4. **坑：全局 `.pcell` 把格子变成了纵向 flex**。`global.css` 里有一条旧版留下的
   `.pcell { display:flex; flex-direction: column; gap:1px }`（配 `.pcell .pen`，早已无人使用）。
   组件里新写的 `.pcell { align-items:center }` 被这条的主轴方向带偏：`align-items` 只管横向，
   纵向位置由 `justify-content` 决定，于是所有名字都贴在格子上沿。删掉这条死规则后恢复居中。
   排查手法：把页面存成静态 HTML，注入脚本读 `getComputedStyle(cell).flexDirection` 与 `getBoundingClientRect()`，
   比对着 CSS 猜快得多。
