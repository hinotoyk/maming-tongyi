# 马名通译 · 全球赛马中文名典

> 港译 / 民间通译 / 机器译名的对照与溯源，连接中·日·英·法文马名与赛事。

同一个名字，中文世界里可能有三种写法：香港赛马会公告里的**港译**、大陆媒体与社区里的**通译**、以及机器生成的**拟译**。它们散落在 Word 附件、日文数据库和各家报道里，搜不到、链不上、对不齐。

本站把这层关系结构化：**每一个中文名都能查到原名、出处、战绩，以及它和谁同场。**

---

## 这是什么

| | |
| --- | --- |
| **定位** | 中文语境下的赛马名称中枢 + 赛事档案 |
| **主键** | 中文名（港译为准，民间与机器译名分级补位） |
| **核心原则** | 名称是「带出处的声明」，不是字段——每条译名都能点开看「谁说的、哪一天说的」 |
| **当前阶段** | P0：数据管线与语料结构化（站点尚未上线） |

## 数据从哪来

| 来源 | 内容 | 用途 |
| --- | --- | --- |
| 香港赛马会赛马新闻（公开） | 956 篇「宣佈出賽馬匹名單」及附件，展开为 2,883 场赛事、约 4.1 万条出赛记录 | 港译马名 / 骑师 / 练马师的中英对照，事实字段 |
| netkeiba（公开页面） | 赛事与马匹档案，`race_id` / `horse_id` | 日文原名、跨场关联、战绩 |
| 各国家/地区赛马机构 | 官方赛果 | P2 扩展（澳 / 英 / 法 / 美） |

**本站与上述机构无隶属关系，非官方站点。** 详细署名与免责见 [`NOTICE`](./NOTICE)。

## 目录结构

```
docs/            字段契约（SCHEMA.md v3）、执行手册（IMPL.md）、方案与可行性
data-src/        人工核对过的种子数据（CSV）与样本附件
pipeline/        Python 采集 / 解析 / 对齐管线
db/              SQLite 表结构（schema.sql 是唯一出处）
public/data/     构建产物：派生 JSON 分片（入库，前端直接读）
src/             Astro 站点
```

## 本地开发

```bash
npm install
npm run dev      # http://localhost:4321/maming-tongyi/
npm run build    # 产物在 dist/
```

数据管线（Python 3.11+）：

```bash
pip install zhconv          # 简体派生（逐字繁转简，长度必须不变）

python pipeline/s0_enumerate_all.py    # 全量枚举文章标题 → cache/
python pipeline/s1_classify.py         # 按赛事地区归类 → data-src/hkjc_entries_index.csv

python pipeline/s2_fetch_docs.py       # 抓正文 + 附件 → cache/docs/{race_uid}/（断点续抓）
python pipeline/s2b_archive.py         # 提交 Internet Archive → cache/archive.csv
python pipeline/s3_parse.py            # docx/doc/内联表 → cache/races.csv + entries.csv
python pipeline/s4_names.py            # 名称 + JBIS 检索页 → cache/names.csv + aliases.csv
python pipeline/s4b_pedigree.py        # JBIS 血統页 + 基本情報页 → cache/pedigree.csv（10s/匹，断点续跑）
python pipeline/build_db.py            # 按 db/schema.sql 重建 cache/maming.sqlite
python pipeline/s5_export.py           # 分片 + manifest + 搜索索引 → public/data/
python pipeline/check_data.py          # 数据体检，不通过 exit 1
npm run build                          # 站点（构建期读 public/data/ 分片）
```

每批抓完必须跑 `python pipeline/netguard.py report`，按报告的建议间隔开下一批。
`--help` 看单批参数（`--region` / `--from/--to` / `--limit` / `--only-failed`）。

## 部署

推 `main` 触发 GitHub Actions 构建并发布到 GitHub Pages。

> 项目页挂在子路径 `/maming-tongyi/` 下，`astro.config.mjs` 的 `base` 必须与之一致；站内链接一律用 `import.meta.env.BASE_URL` 拼，不要写死 `/`。绑自定义域名后把 `base` 改成 `/` 并加 `public/CNAME`。

## 许可

- **代码**：MIT（见 [`LICENSE`](./LICENSE)）
- **派生数据**（名称对照、赛事档案）：CC BY-NC 4.0，可转载须署名，禁止商用
- **原始附件**：不随仓库分发，仅在站上以来源链接指向原站

## 不做什么

不做赔率、不做贴士、不做投注入口、不做赛果预测。本站是资料库，不是彩票资讯站。
