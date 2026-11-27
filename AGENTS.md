# AGENTS.md · 马名通译项目规则（AI 助手必读）

## 0. Git 协作（最高优先级）

- **commit**：仅在用户明确要求时执行；未要求不得自行 `git commit`。
- **push**：永远不执行 `git push`；推送只能由用户本人操作。
- **禁止**：`git reset --hard`、`push --force`、改写历史。
- `dist/`、`node_modules/`、`cache/`、`*.sqlite` 一律不入库。

## 1. 项目定位

- 站点：**马名通译 · 全球赛马中文名典**，中文赛马名称中枢 + 赛事档案。
- 当前阶段 **P0**：数据管线与语料结构化，站点只有壳。
- 字段契约（结构定稿）：`docs/SCHEMA.md`（v3）；**执行手册：`docs/IMPL.md`**——换会话开工先读它。
- 可行性依据：`docs/FEASIBILITY.md`。`docs/PLAN.md` 的页面规划（首页 / 赛果 / 生涯 / 译名对照 / 赛历 / 地区页）已于 2026-11-24 作废，只保留采写与可行性判断，不要按它建页面。

## 2. 数据红线（比代码规则更重要）

1. **只存事实字段**（马名 / 号码 / 排位 / 磅重 / 评分 / 年龄 / 性别 / 名次），**不转载**公告正文、赛事简评原文。
2. **名称必须带出处**：任何写入 `*_name` 的记录都要有 `source` 与 `decided_at`，没有出处的名字不入库。
3. **三级译名不得混级**：港译(L1) / 民间通译(L2) / 机器拟译(L3)，L3 永不覆盖 L1，且页面上必须显示「待确认」。
4. **原件不入库**：抓到的 `.docx`/`.doc`/HTML 落 `cache/`（gitignore），只入库派生的 JSON。
5. **限速**：所有 HKJC 域（`racingnews` / `res` / `corporate` / `racing`）**≥2s/请求**，netkeiba **≥5s/请求**；失败退避重试，不并发轰站。存档提交到 `archive.org` 另有一套限速，见第 7 条。
6. **不做博彩向内容**：赔率、贴士、心水、预测、投注入口，一律不加。
7. **原件只存链接**：抓取原件仅作解析中间件，落 `cache/` 且可清理；对外引用一律指向 `web.archive.org` 回放地址，无存档则显式标注「原件未存档」。
   - 提交用 `GET https://web.archive.org/save/{url}`（**匿名可用，实测 302 同步返回**），间隔 **≥10s**、串行、`--max-time 90`；**不要**用 `POST /save`（401，需登录）。
   - 读取（CDX / 回放）≤24/min；提交前先 CDX 探测，命中不重提。
   - 401 = 整批停并切 SPN2 API（需 `IA_ACCESS_KEY` / `IA_SECRET_KEY`，走环境变量，不入库）；不得把 401/5xx 记成「未存档」。
8. **抓取范围全量、维护范围分级**：所有公告、所有地区一律抓取与解析，**禁止按地区跳过**；`region` 只决定队列顺序。译名维护当前只做日本籍马（`horse.curation = curated`），其他地区 `pending`，只保留公告港译与原文名，不生成 L2/L3、不进人工队列。详见 `docs/SCHEMA.md` §2.1。
9. **出网必须过闸门（风控红线）**：所有请求只能走 `pipeline/netguard.py` 的 `Guard.get()/download()`，不许自己 `curl`/`urlopen`。间隔**只因风控信号上调**，永不低于第 5 条的下限；单线程；撞上风控 = 抬间隔 → 熔断停机 → 冷却后再跑，**不许换 IP/UA、不许绕验证码、不许做主动提速探测**。每批跑完必须 `python pipeline/netguard.py report`，按报告结论（零风控最小档 ×1.25）确定下一批间隔。详见 `docs/IMPL.md` §3。
10. **人物归一必须分层**（`docs/SCHEMA.md` §2.1.1–2.1.2）：
    - **字符层**（撇号 `’`/`'`、全角空格、大小写）在 `common.fold_name()` 里全自动折叠，`slug` / 检索键都走它——只改字符，不改名字。
    - **实体层**（`A P O'Brien` = `Aidan O'Brien`）只允许发生在 `pipeline/s3b_identity.py`：分桶用「角色 + 港译」，判定用「拉丁姓氏 + 名字首字母」，判不了的一律进 `/edit/merge/` 复核页，**不许在别处写启发式合并**。
    - 人工结论只落 `data-src/entity-merges.json`（确认合并）与 `data-src/person-splits.json`（确认不合并），脚本只读不写；否决优先于任何自动规则。
    - 合并只改 uid：被并掉的写法必须作为 `name` 的非主名行保留（带出处、可检索），不许丢名；跨角色同人（既骑且练）不合并，两页互链。

## 3. 代码约定

- 站点：**Astro + Tailwind CSS v4**，静态输出。禁止引入运行时框架（React/Vue）做纯展示页。
- 样式走 Tailwind utility；站点色 token 在 `src/styles/global.css` 的 `@theme` 里定义，页面里不写死 hex。
- 站内链接一律 `import.meta.env.BASE_URL` 拼接，**禁止写死 `/`**（项目页部署在子路径下）。
- 数据读取统一走 `src/lib/data.ts` 的封装，不在组件里裸 `fetch`。
- 管线：Python 3.11+，单文件单阶段（`s0_` `s1_` `s2_` …），阶段之间只通过文件/数据库传递，不 import 彼此的私有函数。
- 表结构唯一出处 = `db/schema.sql`；改表先改它，再用 `build_db.py` 重建，不手改 SQLite。

## 4. 工作流程

- 探索脚本与一次性验证放 `pipeline/_explore/`，验证通过再提炼进 `s*_` 阶段脚本。
- 改前端后必须 `npm run build` 并核对产物，不能只看 dev server。
- 数据变更后跑 `python pipeline/check_data.py`（P0 落地后补），断链、覆盖率骤降、字段整批为空都要报错。
- 新增约定写进本文件或 `docs/PLAN.md`，不要新起过程文档。

## 5. 命名规范

- 仓库名：`maming-tongyi`（ASCII、连字符断词）。
- 站名固定写法：**马名通译**（繁体场景写「馬名通譯」）。
- 页面 `<title>` 模板：`{中文名}（{原名}）· 马名通译`。
