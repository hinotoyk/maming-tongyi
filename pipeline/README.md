# 管线（P0）

单阶段单文件，阶段之间只通过文件传递，互不 import。所有产物落在仓库内的
`cache/`（中间件，gitignore）与 `data-src/`、`public/data/`（正式产物，入库）。

## 阶段

| 阶段 | 脚本 | 输入 | 输出 | 状态 |
| --- | --- | --- | --- | --- |
| S0-1 | `s0_enumerate_all.py` | 香港赛马会赛马新闻 API | `cache/all_titles.json`（19,368 条标题） | ✅ 已跑通 |
| S1 | `s1_classify.py` | `cache/all_titles.json` | `data-src/hkjc_entries_index.csv`（956 条，全量入队） | ✅ 已跑通 |
| 共用 | `common.py` | — | 路径 / CSV / slug / 简体派生 / 表头与名单表解析 | ✅ |
| 共用 | `jbis.py` | — | JBIS 三个页面的抓取与解析（检索 / 基本情報 / 5 代血統），出网仍走 netguard | ✅ |
| S2 | `s2_fetch_docs.py` | 索引 CSV（**956 条全量，不按地区过滤**） | `cache/docs/{race_uid}/` + `manifest.csv` | ✅ 小批量跑通 |
| S2b | `s2b_archive.py` | `cache/docs/manifest.csv` | `cache/archive.csv`（= `archive` 表） | ✅ 小批量跑通 |
| S3 | `s3_parse.py` | `cache/docs/`（docx / doc / 内联表） | `cache/races.csv` + `cache/entries.csv` | ✅ 小批量跑通 |
| S4 | `s4_names.py` | S3 产物 + JBIS 检索页 + 覆盖层 | `cache/names.csv` + `aliases.csv` + `horses.csv` + `persons.csv` | ✅ 小批量跑通 |
| S4b | `s4b_pedigree.py` | `cache/jbis.csv` + JBIS 血統页 / 基本情報页 | `cache/pedigree.csv` + `jbis_profile.csv` | ✅ 分批跑（10s/匹） |
| 建库 | `build_db.py` | 上述 CSV | `cache/maming.sqlite` | ✅ |
| S5 | `s5_export.py` | SQLite + JBIS + 覆盖层 | `public/data/`（分片 + manifest + 搜索索引） | ✅ |
| S7 | `check_data.py` | 全量产物 | 通过 / exit 1 | ✅ |

## 跑法

```bash
python pipeline/s0_enumerate_all.py   # 约 6.5 分钟，194 页，2s/请求
python pipeline/s1_classify.py        # 秒级

python pipeline/s2_fetch_docs.py --region 日本        # 抓正文 + 附件（断点续抓）
python pipeline/netguard.py report                   # 每批跑完必做
python pipeline/s2b_archive.py --kind article        # 提交 IA（≥10s/请求）
python pipeline/s3_parse.py                          # 解析 → races / entries
python pipeline/s4_names.py                          # 名称 + JBIS id（可 --jbis-limit 分批）
python pipeline/s4b_pedigree.py --limit 300          # 5 代血统 + 生日/毛色（断点续跑）
python pipeline/build_db.py && python pipeline/s5_export.py
python pipeline/check_data.py --partial              # 发布前去掉 --partial
```

## 处理顺序 vs 处理范围

**范围永远是全量。** `region` 只决定队列顺序，不决定是否入队：

```
S2/S3 队列：日本(235) → 香港(25) → 澳洲/新西兰(242) → 英国/爱尔兰(172)
            → 法国(109) → 中东(51) → 德国/欧洲(50) → 其他(~60)
```

先跑日本是为了早出可核对的内容，不是放弃其他地区。**任何阶段都不得出现
`if region == '日本': continue` 这类过滤**——地区只影响排序与 `curation` 初值。

「只维护日本马译名」落在 `horse.curation`，见 `docs/SCHEMA.md` §2.1：

| 阶段 | 对 `pending`（非日本）马的额外动作 |
| --- | --- |
| S2 / S3 | 无，照常抓、照常解析 |
| S4 | 跳过 netkeiba 对齐（`xid` 留空，不算失败） |
| S5 | 不生成 `l2_folk` / `l3_machine`，不进 `review_queue` |

## S2 开工前必须知道的三个坑

1. **附件是协议相对链接**：`href="//res.hkjc.com/racingnews/wp-content/uploads/..."`。
   绝对化时若拼成 `racingnews.hkjc.com/wp-content/...` 会返回 **500**。
   主机白名单：`res.hkjc.com`、`corporate.hkjc.com`、`racing.hkjc.com`。
2. **两种格式**：`.docx` 是真 ZIP（`504b0304`），`.doc` 是 OLE2（`d0cf11e0`）。
   `.doc` 必须转一道：`s3_parse.convert_doc()` 先试 `soffice --headless --convert-to docx`，
   没有 LibreOffice 就走 Word COM（Windows，`SaveAs2(...,16)`），两条都失败才记 `need-soffice`。
3. **正文形态三种**：`.docx` 附件 / `.doc` 附件 / 内联 HTML 表格，约占 1:1:1。
   只抓 HTML 会把「最後宣佈」类误判成空数据。

## 限速（硬约束，写在 AGENTS.md 里）

| 目标 | 取值 | 说明 |
| --- | ---: | --- |
| HKJC 全域（`racingnews` / `res` / `corporate` / `racing`） | **2.0s/请求** | 快请求会 400，退避重试不等于可以提速 |
| netkeiba（`db` / `en` / `race`） | **5.0s/请求** | 无 robots 规则 ≠ 允许高频 |
| archive.org 提交（`GET /save/{url}`，匿名） | **10.0s/请求** | 实测可用、同步返回；单页 7–37s，均值 20s。串行，不并发 |
| archive.org 提交（SPN2 API，认证） | ≤6/min | 仅在匿名口径被收紧时启用 |
| archive.org 读取（CDX / 回放） | ≤24/min | 与提交分开计 |

跑 S0 全量 194 页按 2s/请求约 6.5 分钟，比原来的 3 分钟慢，但这是换稳定性的必要成本。

## S2b 存档口径（2026-11-24 实测，无需账号）

**核心：`GET https://web.archive.org/save/{url}` 匿名可用，302 同步返回快照。**

```bash
curl -s -o /dev/null -D - --max-time 90 "https://web.archive.org/save/$URL"
# → HTTP/1.1 302 FOUND
# → location: https://web.archive.org/web/20261124080624/https://racingnews.hkjc.com/...
```

取 `location` 里 14 位 `ts`，整条 `location` 就是 `snapshot_url`。**没有 job_id，不用轮询。**

实测能力边界（决定归档策略）：

| 对象 | 实测 |
| --- | --- |
| 内联表格型正文页 | ✅ 93KB，8 表 107 行，`馬名/騎師/練馬師` 齐全 |
| `.docx` 附件 | ✅ 23.5KB，`Content-Type` 正确，zip 可解 17 项，双语对照完整 |
| 重复提交 | 返回已有 `ts`，不重抓（幂等） |
| `POST /save`（JSON 接口） | ❌ 401，必须登录——所以走 GET，不走 API |

口径：

- 只提交公开、可访问的原站 URL；主机白名单与 S2 一致。
- **按形态决定归档对象**：内联表型只存 `article`；附件型存 `article` + `attachment`。
- 提交前先 `CDX` 探一次（`/cdx/search/cdx?url={prefix}*&output=json`），命中写
  `capture_method = cdx_existing`，不发请求。
- 串行 + 10s 间隔；`--max-time 90`。时间预算：全量约 1,430 次 ≈ 12 小时，**必须可断点续跑**。
- `snapshot_url` 统一存 `https://web.archive.org/web/{ts}/{url}`，前端只认这一列。
- 失败不阻塞 S3：`failed` / `pending` 留在表里，下轮重试，最多 3 次。
- **401 是整批停信号**，不是单条失败：说明匿名口径被收紧，切 `spn2_api`。

## 实测补充（2026-11-25 全量跑完时的偏差）

- **956 篇 ≠ 956 场**：同一赛事先发「宣佈」再发「最後宣佈」，按 `race_uid` 去重后 **657 篇公告**；
  被覆盖的旧公告进 `cache/docs/duplicates.csv`（不进 races，存档阶段仍可提交）。
- **一篇公告也不止一场**：「XX賽馬日」一份公告含 4–10 张表，是当天不同场次。口径是**一张表 = 一场**，
  展开后 **2,883 场 / 40,660 行**（正赛沿用公告 uid，副场用 `{prefix}-{year}-{mmdd}-{slug}`）。
  实测 183 篇 docx 里 890 张名单表，只有 48 篇是单场公告——只取一张表会丢 3/4 的数据。
- **磅重**：日本/香港 100–140；越洋转播的欧洲卡按当地磅制上限更高（实测德国 152、澳洲 145），
  体检按地区放宽（德/英/爱/法 168、澳/美 165）。小场次可能只有 4–5 匹，<8 只告警。
- **附件识别**：文件名会带撇号（`Prixdel'Arc...docx`），href 解析必须按定界符匹配，否则整条链接被截断。
- **内联表**：没有信息行时上下文是整页导航，不能当表头（会把墨爾本盃解成 1650 米）。
- 详见 `docs/IMPL.md` §16。

## 探索脚本

`_explore/` 下是立项期的验证脚本（按关键词搜、标题前缀统计等），只作参考，
不参与正式管线。`s1` 已替代其中的归类逻辑。
