# 字段契约 · v3（结构已定稿）

> 这份文件是**唯一口径**。实现细节（阶段划分、函数、命令、验收）见 [`IMPL.md`](IMPL.md)。
> 表结构唯一出处是 [`db/schema.sql`](../db/schema.sql)；本文件解释「为什么这么定」。
> v2 及以前的「首页 / 赛果 / 生涯 / 译名对照 / 赛历 / 地区页」等设计**已作废**，不要再加回来。

---

## 0. 一页速览

| 维度 | 定稿 |
| --- | --- |
| 实体 | 只有三种：马匹 `horse`、赛事 `race`、人物 `person`（`jockey` / `trainer`） |
| 名称 | 四栏可写 + 一栏派生 + 一栏人工简写：`original` / `en` / `hk` / `own` / **`sc`（派生）** / **`alias[]`（简写）** |
| 记什么 | 抓到的**比赛页**（赛期、马场、途程、名单）+ **名称**；不记生涯、赛果、名次、赔率 |
| 抓多少 | 全量 956 篇公告，全地区；**译名只人工维护日本籍**（`curation`） |
| 人名 / 马名出处 | 一律 HKJC 公告；日本马血统补 JBIS |
| 原件 | 不进库、不对外分发；对外只给 `web.archive.org` 回放地址 |
| 人工判断 | 只落在 `data-src/name-overrides.json`，脚本只读不写 |
| 站点 | Astro 静态：`/` 名称档案（平铺一张表）、三个实体页、`/edit/` 批量改名、`/sources/` 说明 |

一句话：**名称是带出处的声明，简称是人的省略，简体是字的转换——三件事互不冒充。**

---

## 1. 实体与关系

```
race ──< entry >── horse
        │           │
        └── person  └── name(1:N)  entity_alias(1:N)
                        ↑ 三张实体表共用 name / entity_alias（entity_type 区分）
xid          任意实体的外部 ID 映射（jbis / netkeiba / racingpost / hkjc）
archive      对外引用的存档快照（Internet Archive 回放地址）
review_queue 待人工确认：合并、译名、简称撞车
```

### 1.1 三条不能改的设计

1. **名称是行，不是列。** 一个实体天然有原文、英文、港译、自译，且每一栏都要单独记出处与定稿日。
   用字段存就只能记一条 `source`，`(JPN)` 后缀来的港译和 JBIS 来的假名会共用一个出处，等于没出处。
   `horse` / `race` / `person` 三张表里**不许再出现** `cn_name` / `local_name` 这类名称列。
2. **简体不落库。** `sc` 是 `hk` 的纯函数（逐字繁转简）。落库必然出现「hk 改了、sc 还是旧的」，
   而这正是本项目最怕的那类脏数据。导出时现算，一次构建一份，永远不会不一致。
3. **简称不进名称表。** 简称是**多对一**的简写（傲登龍門 → `M.傲`、`S.门`），没有语种、没有置信度、
   不能当主名。塞进 `name` 表会让「一匹马几个名字」和「一个名字几种写法」混成一件事。

### 1.2 `curation`：抓取范围 ≠ 维护范围

| 维度 | 范围 | 决定它的是 |
| --- | --- | --- |
| 抓取 / 解析 | 全部 956 篇公告、全部地区 | `data-src/hkjc_entries_index.csv`，**禁止按地区过滤** |
| 名称维护 | **仅日本籍马** | `horse.curation = curated` |
| 对外发布 | 全量（含 `pending`） | 导出产物 |

| 值 | 含义 | 名称数据 | 进人工队列 |
| --- | --- | --- | :---: |
| `curated` | 日本籍，已核对 | 港译 + 日文原名 + 自译（人工，可为空）+ 简称（人工，可为空） | ✓ |
| `pending` | 其他地区，**只落不维** | 只有公告自带的港译 + 原文名；不生成自译、不接血统 | ✗ |

两个常被误解的点：

- **非日本马不是「没有中文名」**。HKJC 附件本身是双语对照（实测 `Embroidery (JPN) | 巧繡生花`），
  港译免费获得。`pending` 只是「没做交叉核对」。
- **扩区只是翻一个字段**：把某地区 `curation` 从 `pending` 改成 `curated`，它就进维护队列，
  不用重抓、不用改管线。

---

## 2. 名称六栏

### 2.1 栏位定义

| key | 页面标签 | 内容 | 来源 | 可编辑 | 进检索 | 进分级 |
| --- | --- | --- | :---: | :---: | :---: |
| `original` | 原文 | 所属语区的原始书写（日文片假名 / 拉丁名本身） | 公告附件；日本马取 JBIS 馬名 | 覆盖层 | ✓ | 否（基准） |
| `en` | 英文 | 拉丁拼写 | 公告附件 `Name (JPN)` 去后缀 | 覆盖层 | ✓ | 否（基准） |
| `hk` | 港译 | 香港赛马会公告译名 | HKJC 公告 / 排位表 | **页面可改** | ✓ | L1 · 0.95 |
| `sc` | 简体 | 港译**逐字**转简体，`f(hk)` | 派生，不入库 | ✗ 永不 | ✓ | 否（派生） |
| `own` | 自译 | 本站 / 社区通译，人工拟名 | 人工 | **页面可改** | ✓ | L3 · 0.30 |
| `alias[]` | 简称 | 稿中简写 / 缩写，多对一 | **人工，机器不生成** | **页面可改** | ✓ | 否（不是名称） |

页面上的「几栏名称」永远指前五栏里的四栏（原文/英文/港译/自译）+ 派生的简体；
简称单独一行，不占名称栏位置。

### 2.2 分级（只对中文译名）

| 级别 | 值 | 含义 | 置信起点 | 页面标识 |
| --- | --- | --- | --- | --- |
| L1 | `hk` | 港译 | 0.95 | 港译 |
| L2 | —（**P0 不落字段**） | 民间通译（媒体、社区、出版物） | 0.60 | 通行译名 |
| L3 | `own` | 自译（人工拟名） | 0.30 | 自译 · 待确认 |

- **L2 暂不实现**：没有稳定、可指回原件的中文来源时不单开字段，避免「用 `own` 冒充 L2」。
  将来若接入，新增 `name.field = 'folk'`，与 `hk` / `own` 平级，不动现有数据。
- L3 永不覆盖 L1：`hk` 存在时，`own` 只作为并列的另一套译名展示，不替换主名。
- `curation = pending` 是**维护状态**，不是名称级别。页面表现为「待维护」虚线徽章，
  不新增 `l0` 之类级别，分级只有三档。

### 2.3 简体 `sc`：派生规则（禁止变动）

**定义**：`sc = T2S(hk)`，逐字繁→简，只换字形。

硬约束：

1. **长度不变**：`len(sc) == len(hk)`。不等就是「改了词」（`一哩 → 一英里`），直接抛错、不入库。
2. **不做词汇替换**：用字形级转换表（`zhconv` 的 `zh-hans`），**不用** `zh-cn`（那是大陆用词替换）。
3. **不做人工修润**：`sc` 不允许出现在覆盖层、不允许在编辑页修改。
   `hk` 一改，`sc` 下次重建自动跟着改。
4. **空就是空**：没有 `hk` 就没有 `sc`，不拿 `original` 或 `en` 代填。

实现（`pipeline/_explore/make_demo_json.py`，全量阶段移到 `pipeline/s4_names.py`）：

```python
def simplified(text):                       # text = hk
    if not text:
        return None
    out = zhconv.convert(text, "zh-hans")
    if len(out) != len(text):               # 长度变了 → 视为改词
        raise ValueError(f"简体转换改变了长度（{text!r} → {out!r}）")
    return out
```

验收：现有 55 条名称，27 条字形有变（`巧繡生花 → 巧绣生花`）、28 条同形（`三浦皇成`）。
同形的在页面上转灰（`.dim`），表示「没有新信息」，与「英文同形转灰」同一套约定。

### 2.4 简称 `alias`：人工简写（多对一）

**是什么**：写稿、检索、口播时用的省略式，例如：

| 实体 | 简称 |
| --- | --- |
| 傲登龍門 `fr-erdenali` | `M.傲`、`S.门` |
| 巧繡生花 `jp-embroidery` | （待人工填） |

**规则**：

1. **多对一**：一个实体 0..n 个简称，n 个简称都指回同一条 `uid`；展示顺序 = 人工录入顺序（`seq`）。
2. **人工维护，机器不生成**。不截取港译、不取英文首字母、不按规则推导——推导出来的叫「猜」。
3. **不是名称**：不进分级、不参与主名选择、不参与 `curation`、不覆盖 `hk`。
4. **格式**：1–8 个字符；同一实体内去重；不要求与港译不同（`M.傲` 这种本来就只是省略）。
5. **全局唯一性不强制，但冲突要记**：两个实体撞同一个简称时，两边照常展示，
   同时往 `review_queue`（`kind = alias`）写一条，因为简称的主要用途是检索，歧义必须可查。
6. **展示**：索引表一列（最多 3 条，余下折 `+N`）；详情页名称栏下方一行胶囊标签；
   检索串里与四栏名称平级（搜 `M.傲` 直接命中该马）。

**存储**：人工层只写 `data-src/name-overrides.json` 的 `entries[uid].alias`；
导出到 JSON 时变成 `names.alias: [{ "text": "M.傲", "note": null }]`。
`note` 是「为什么这么简写」，可空，不展示成正文、只做 `title`。

### 2.5 名称的「同形转灰」

页面只有一处视觉降级，两条规则：

| 情况 | 处理 | 理由 |
| --- | --- | --- |
| `original === en`（拉丁马名、海外骑练） | 英文栏转灰 `.dim` | 两栏同值不是两条信息 |
| `hk === sc`（本来就是简体字） | 简体栏转灰 `.dim` | 转换没有产生新字形 |

---

## 3. 人工图层 `data-src/name-overrides.json`

**这是全站唯一的人工改名义入口**，进 git、可 review、可回溯。抓取与解析可以天天重跑，
人的判断必须落在独立文件里，构建时最后套用，否则下一次重建就被抹掉。

```json
{
  "_meta": { "schema": "name-overrides/v2", "updated": "2026-11-24" },
  "entries": {
    "fr-erdenali": {
      "hk": "傲登龍門",
      "alias": ["M.傲", "S.门"],
      "_note": "示例：用户举例的两种简写"
    },
    "jp-embroidery": {
      "own": "巧绣生花",
      "alias": [{ "text": "巧绣", "note": "稿件常用" }]
    }
  }
}
```

| 键 | 类型 | 含义 |
| --- | --- | --- |
| 顶层 `entries` | `{ uid: patch }` | `uid` 可以是 `horse` / `race` / `person` 任一实体的 uid |
| `hk` | `string` | 改港译；**改完必须重算 `sc`**（管线自动做） |
| `own` | `string` | 写自译 |
| `alias` | `string[]` / `{text, note}[]` | 简称，**全量替换**（不是追加）；顺序 = 展示顺序 |
| `_note` / `_*` | 任意 | 人工备注，`_` 前缀一律不参与套用 |

规则：

1. **脚本只读，永不写**。编辑页只生成要合并进去的 `entries` 片段，落盘由人确认。
2. **空值不清空**：`""` / `null` = 「不覆盖」，要清空请显式写 `[]`（简称）或删掉整个 patch。
3. **只允许改名称层**：`hk` / `own` / `alias`。赛事事实字段（磅重、名次、赛期）一律以公告为准，
   要改就改解析器，不改覆盖表。
4. **套用顺序**：抓取结果 → `hk` / `own` 覆盖 → 重算 `sc` → `alias` 覆盖 → 导出。
5. 套用记录写 `override_apply` 表（`old_value` / `new_value` / 文件 `sha256`），
   用于回答「这个名是谁改的、哪次构建生效的」。

---

## 4. 导出产物

### 4.1 演示期（当前）：`src/data/demo.json`

由 `pipeline/_explore/make_demo_json.py` 生成，**可重跑、不手抄**。Schema `names/v3`。

```json
{
  "_meta": { "schema": "names/v3", "updated": "2026-11-24", "note": "…" },
  "race": {
    "uid": "jra-2026-mile-championship",
    "names": {
      "original": "マイルチャンピオンシップ", "en": "Mile Championship",
      "hk": "一哩冠軍賽", "sc": "一哩冠军赛", "own": null, "alias": []
    },
    "announce_date": "2026-11-20", "race_date": "2026-11-22", "race_time": "14:40",
    "session": "海外賽事第一組 第七場", "grade": "G1", "grade_cn": "一級賽",
    "venue": "日本京都競馬場", "venue_country": "日本", "course": "京都競馬場",
    "distance": "1600 米", "surface": "草地",
    "entries": [ { "no": 1, "draw": 1, "gear": null, "horse_uid": "fr-erdenali",
                   "jockey_uid": "j-mickael-barzalona", "trainer_uid": "t-francis-henri-graffard",
                   "lb": 126, "rating": 124, "sex": "H", "age": 3 } ]
  },
  "horses": [ {
    "uid": "fr-erdenali", "no": 1, "draw": 1, "gear": null,
    "origin": "FR", "country": "法国", "sex": "H", "sex_cn": "雄", "age": 3,
    "rating": 124, "lb": 126,
    "names": { "original": "Erdenali", "en": "Erdenali", "hk": "傲登龍門",
               "sc": "傲登龙门", "own": null,
               "alias": [ { "text": "M.傲", "note": null }, { "text": "S.门", "note": null } ] },
    "pedigree": null,
    "status": "pending",
    "jockey_uid": "j-mickael-barzalona", "trainer_uid": "t-francis-henri-graffard",
    "race_uid": "jra-2026-mile-championship"
  } ],
  "persons": [ {
    "uid": "j-christophe-lemaire", "role": "jockey", "role_cn": "骑手",
    "nationality": "FR", "nationality_cn": "法国",
    "names": { "original": "Christophe Lemaire", "en": "Christophe Lemaire",
               "hk": "李慕華", "sc": "李慕华", "own": null, "alias": [] },
    "horses": ["jp-embroidery"]
  } ],
  "sources": { "article": {…}, "attachment": {…}, "jbis": {…} },
  "regions": [ { "region": "日本", "announcements": 235, "status": "curated" } ],
  "index_total": 956
}
```

字段裁剪（`pending` 实体）：`pedigree = null`；`names.own = null`；`names.alias = []`；
事实字段照常。**没有「占位数字」**：不知道就 `null`，页面显示「—」。

### 4.2 全量后：`public/data/` 分片

- 一实体一 URL 不变，但**存储分片**：`horses/shard-000.json` 每片 500 条，
  `races/`、`persons/` 同理。2 万个碎文件会拖死 Windows 文件系统与 git 索引。
- `manifest.json` 是唯一清单：版本、生成时间、各实体计数、分片数、`curation` 覆盖范围。
- 每个产物带 `_meta`：`schema` + `updated` + `count`。裸数组无法判断新旧。
- 构建期由 `src/lib/demo.ts`（全量后改名 `data.ts`）一次性 `import`，运行期不 fetch 实体数据。

### 4.3 检索

- 演示期：行级 `data-q`（原文+英文+港译+简体+自译+简称+国籍，小写空格拼接），
  纯前端 `includes` 过滤，一次渲染 55 行没有压力。
- 全量后：索引页**不能**一次渲染 2 万行。改为
  「默认 200 行 + 搜索走构建期生成的 `public/search/name-*.json`（按名词首字符分片）+
  命中后客户端渲染」，分页与筛选仍是静态页，不引入服务端。

---

## 5. 数据库

结构以 [`db/schema.sql`](../db/schema.sql) 为准（v3）。要点：

| 表 | 一句话 | 不要做的事 |
| --- | --- | --- |
| `race` / `horse` / `person` | 事实字段 | 不放名称列 |
| `name` | 四栏名称，一行一栏，带 `source_url` / `archive_url` / `decided_at` | 不存 `sc`；同日同栏只能一个 `is_primary` |
| `entity_alias` | 简称，多对一，`seq` 定顺序 | 不进 `name` 表 |
| `entry` | 出赛名单（这场比赛的这匹马） | 不做赛果维护，`finish` 多为 `NULL`，不用 0 代替 |
| `xid` | 外部 ID（jbis / netkeiba） | 不拿外部 ID 当主键 |
| `archive` | IA 快照 | 原件不入库 |
| `review_queue` | 待人工确认 | 简称撞车、名称合并都进这里 |
| `override_apply` | 人工层套用流水 | 覆盖本身不落库，落 `name-overrides.json` |

`name.field` 有 `CHECK` 约束（`original` / `en` / `hk` / `own`）；加 `folk` 时要先改这里。

---

## 6. 存档（Internet Archive）

对外引用**只有一种**：`https://web.archive.org/web/{ts}/{url}`。

| 项 | 定稿 |
| --- | --- |
| 提交方式 | `GET https://web.archive.org/save/{url}`（匿名即可，**302 同步返回快照**，无 job 轮询） |
| 禁止 | `POST /save`（匿名 401 `You need to be logged in`） |
| 间隔 | **≥10s**，单线程串行，`--max-time 90` |
| 幂等 | 同一 URL 重复提交返回已有 `ts`，不重抓 |
| 先探 | `CDX` 查询命中则记 `capture_method = cdx_existing`，不再提交 |
| 归档对象 | 正文内联表格型 → 只提 `article`；仅有附件链接型 → `article` + `attachment` |
| 失败 | 4xx / `blocked-url` → `failed` 并保留原链接；5xx / 超时 → `pending`，下轮重试（最多 3 次） |
| 401 | 匿名口径被收紧 → 整批停、切 SPN2（需账号）；**不得记成「未存档」** |
| 页面 | 有存档 → 「存档 ↗」指向回放；无存档 → 「原件未存档」+ 原始链接，不渲染成错误 |

`archive` 表空着不影响站点可用：`source.url` 永远在。

---

## 7. 页面契约

### 7.1 路由（现役 6 类 / 58 页）

| 路由 | 页面 | 数据 |
| --- | --- | --- |
| `/` | 名称档案：搜索 + 5 段筛选 + 平铺一张表 | `demo.json` |
| `/horse/{uid}/` | 马匹：名称五栏 + 简称 + 档案 + 血统 + 出处 | 同上 |
| `/race/{uid}/` | 赛事：名称五栏 + 简称 + 比赛页记录 + 名单 | 同上 |
| `/person/{uid}/` | 人物：名称五栏 + 简称 + 档案 + 本场关联 | 同上 |
| `/edit/` | 批量改名：改名 / 自译 / 简称，输出覆盖片段 | 同上 |
| `/sources/` | 来源、分级、维护范围、存档政策 | 静态 + `regions` |

**作废路由**（不要再建）：`/races/`、`/horses/`、`/persons/`、`/dict/`、`/name/`、
`/calendar/`、`/country/`、`/curation/`，以及首页。

### 7.2 版式骨架（对齐 yunji-web，逐值同源）

**令牌**（`src/styles/global.css` 的 `@theme`）

| 语义 | 值 | 用在哪 |
| --- | --- | --- |
| `primary` | `#0aa7a0` | 栏目标题竖条、选中态竖条、链接 |
| `accent` / `accent-foreground` | `#e6f6f5` / `#0b6b64` | 侧栏选中底、L1 港译标签 |
| `foreground` / `secondary-foreground` | `#0a0a0a` / `#171717` | 正文 / 标题 |
| `muted-foreground` | `#737373` | 标签、次要文字 |
| `border` | `#e5e5e5` | 全部发丝线 |
| 等级色 | G1 `#d5302a` / G2 `#0a3b7e` / G3 `#087220` | 行业语义色，唯一例外 |

**骨架**

| 部位 | 规格 |
| --- | --- |
| 左侧导航 | 220px 固定；logo 30×30 圆角 8 实心 primary；站名 15/600 + 11px 副名；分组标签 11.5/600 字距 1.5；导航项 14px `padding:9px 10px` 圆角 8；选中 = `accent` 底 + 3px 竖条（伪元素） |
| 页标题 | 14/700 字距 1 + 4×16 圆角 2 竖条；`padding:18px 24px 16px` |
| 栏目标题 `.sec` | 同款竖条 + 14/700；`margin:36px 0 12px`；右侧 `.sec-right`（11.5px 灰）放计数或筛选 |
| 名称栏 `.names` | 斜杠分隔：标签 10.5/600 字距 1.5 大写，值 19/600，**主名 30/700 且标签转 primary**；空槽写「—」 |
| 简称行 `.aliasline` | 名称栏下方 12px；标签同上小标；值用 `.chip` 胶囊，`title` 挂 `note` |
| 缝线资料表 `.info` | `display:grid; gap:1px` + 容器底色 = 1px 发丝线；行 `padding:9px 16px`，键 74px / 11.5 灰，值 13.5/500 |
| 数据表 | 13.5px；表头 12.5/600 底色 `muted/40` `padding:9px 13px`；单元格 `padding:8px 13px`；末行去下边框 |
| 分段控件 `.seg` | 32px 高、圆角 8、1px 边框、内格 1px 分割；选中 = primary 实底白字 600 |
| 宽表 | 九列名称档案表在 `.card.scroll` 内，`min-width:1060px`，窄屏整表横向滚动，不裁列 |
| 断点 | 单断点 860px：导航横向滚动、内容单列 |

三条不可退让的原则：**不用卡片堆内容**（分区靠 36px 留白）、**发丝线只有一种**（1px `#e5e5e5`）、
**强调只有一处**（primary 只给竖条、主名标签、选中态）。

### 7.3 索引表列序（定稿）

`类型 | 原文 | 英文 | 港译 | 简体 | 自译 | 简称 | 国籍 | 状态`

- 类型：马匹 / 赛事 / 骑手 / 练马师（不再带「· 2 号」这类赛事上下文）。
- 国籍：马匹取产地、赛事取举办地、人物取国籍。**只放国籍，不塞其他副信息**（旧的「说明」列已删）。
- 状态：已校订 / 待维护 / 待接入。
- 筛选 5 段：全部 / 马匹 / 赛事 / 骑手 / 练马师。

### 7.4 空态与未确认态

| 情况 | 展示 |
| --- | --- |
| 缺某一栏名称 | 值写「—」，灰色；不写「待接入」长句 |
| `curation = pending` | 马匹页顶部细提示「本区译名未纳入维护」；索引状态「待维护」 |
| 无简称 | 简称列写「—」；不显示「暂无简称」 |
| 无血统 | 空态卡「本区暂不维护血统」/「JBIS 待接入」，说明字段同形可扩 |
| 无存档 | 「原件未存档」+ 原始链接，不渲染成错误 |
| 未抓赛果 | 不建「赛果」区块（本阶段根本没有这个区块） |

### 7.5 组件

| 组件 | 输入 | 输出 |
| --- | --- | --- |
| `Names.astro` | `nameCols(names)` | 名称栏（5 列，含简体的灰/亮两态） |
| `Aliases.astro` | `names` | 简称行（无简称时整块不渲染） |
| `Info.astro` | `rows[]` | 缝线资料表 |
| `Base.astro` | `title` / `nav` | 外壳、侧栏、页脚 |

---

## 8. 变更记录

| 版本 | 日期 | 变更 |
| --- | --- | --- |
| v3 | 2026-11-24 | 结构定稿：只剩名称档案 + 编辑；名称拆六栏（含派生简体、人工简称）；删除首页 / 赛果 / 生涯 / 对照 / 赛历 / 地区页；人物拆骑手与练马师 |
| v2 | 2026-11-24 | 新增 `curation` 分层（抓取全量 / 维护日本）；JBIS 血统接检索页 |
| v1 | 2026-11-23 | 初版：三级译名 + 全站页面规划 |
