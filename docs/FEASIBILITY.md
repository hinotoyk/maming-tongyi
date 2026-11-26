# 香港赛马会「出赛马匹名单」检索与 netkeiba 赛事关联可行性报告

**探索日期**：2026-11-24  
**触发样本**：[寶塚紀念賽日 – 宣佈出賽馬匹名單（2026-06-12）](https://racingnews.hkjc.com/chinese/2026/06/12/%E5%AF%B6%E5%A1%9A%E7%B4%80%E5%BF%B5%E8%B3%BD%E6%97%A5%E5%AE%A3%E4%BD%88%E5%87%BA%E8%B3%BD%E9%A6%AC%E5%8C%B9%E5%90%8D%E5%96%AE/)  
**范围**：HKJC 赛马新闻站点归档（中文/英文）、旧版 corporate 新闻、netkeiba 日/英版  
**结论一句话**：HKJC 现站可批量检索出马表，2017-07 起计约 958 篇严格标题命中（宽口径 1266 篇）；netkeiba 侧「日期 → 赛事 → 马匹 ID → 跨场赛事史」链路已实测跑通，**可行**。

> 注（立项后更新）：本文写于探索期，限速口径为「≥1s」。立项后已上调为 **HKJC ≥2s/请求、netkeiba ≥5s/请求**，以 [`AGENTS.md`](../AGENTS.md) 为准；对外引用改用 Internet Archive 存档链接，见 [`SCHEMA.md`](./SCHEMA.md) §4。

---

## 0. 结论速览

| 问题 | 结论 |
| --- | --- |
| 现在能检索出多少这样的出马表？ | 站内搜索 `出賽馬匹名單` 命中 **1266** 篇；按标题严格过滤（标题含「出賽馬匹名單」）为 **958 篇**；其中「最後宣佈出賽馬匹名單及排位」591 篇、「暫定」59 篇、「優先」24 篇、「獲選」64 篇。 |
| 最远能检索回几几年？ | **当前主站（racingnews.hkjc.com）从 2017-07-01 开始**，首篇出马表为 2017-07-07。更早的内容在旧版 `hkjc.com/chinese/corporate/` 归档中仍有存量：中文已验证到 **2016-12**，英文已验证到 **2015-11**；但旧站没有可用检索入口，只能按已知文件路径或外部搜索引擎命中，2011 年的路径现已跳转维护页。 |
| 能否按国家和地区分类？ | 能。958 篇按赛事名归类为 14 类，澳大利亚/新西兰 243、日本 236、英国/爱尔兰 172、法国 109 为四大类；完整逐条链接见随附 CSV。 |
| 能否按日期+赛事名接 netkeiba、再把马和马关联？ | **能**。实测 2026-06-14 宝塚纪念：日期页 → race_id `202609030411` → 18 匹马的 10 位 horse ID → 马匹档案页列出历届赛事 race_id，形成跨场图谱。HKJC 英文稿与 netkeiba 英文版马名可直接对齐（如 `MEISHO TABARU` ↔ `Meisho Tabaru`）。 |

---

## 1. 样本文章长什么样

用户给出的 2026-06-12 文章是典型的「**宣佈出賽馬匹名單**」：

- 标题：`寶塚紀念賽日 – 宣佈出賽馬匹名單`，发布时间 `12/06/2026 12:31`。
- 正文按赛事分段（该篇列出 7 组：两岁新马赛、1200 米泥地一捷马赛、2000 米草地一捷马赛、逆濑川特别赛、武库川特别赛、花径锦标、丝带赏），每段两张表头：**马名**、**练马师**。
- 马名格式固定：`英文马名 (产地代码) 中文译名 (产地字)`，如 `DYNAMIC (JPN) 鮮明新曲 (日)`；练马师为英文+中文对照。
- 这是**赛前宣布名单**；对应实际赛日为 2026-06-14（阪神），依据：[HKJC 越洋转播公告](https://www.hkjc.com/chinese/pressrelease/mcs01_showhtml.asp?SelType=NEWS&filename=20260612_103828_C_NEWS.htm)。
- 注意：HKJC 同日还会发「**最後宣佈出賽馬匹名單及排位**」，这类文章正文常不在 HTML 表格里，而是挂一个 `.docx` 附件，字段更全（马号、排位、参赛国/地区、国际评分、负磅、骑师、性别、配备等）。抓取时要分两种解析路径。

---

## 2. HKJC 出马表的数量、时间跨度与检索方式

### 2.1 数量（截至 2026-11-24）

主站是 WordPress，公开只读 REST API，响应头 `X-WP-Total` 可直接给出命中总数：

| 检索词 | 命中数 | 说明 |
| --- | ---: | --- |
| `出賽馬匹名單` | 1266 | 宽口径，正文命中也算 |
| `宣佈出賽` | 1528 | 含各类宣布/报名公告 |
| `最後宣佈出賽` | 889 | 最终名单及排位 |
| `獲選馬匹名單` | 73 | 国际赛获选马匹 |
| `出賽名單` | 4 | 少量异写 |
| 标题严格含 `出賽馬匹名單`（本次抓取 1633 篇候选去重后） | **958** | 本报告分类口径 |
| 英文站 `Entries` | 1111 | `racingnews.hkjc.com/english/` |

年度分布（严格口径 958 篇）：

| 2017 | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026* |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 22 | 53 | 60 | 65 | 84 | 87 | 107 | 128 | 149 | 203 |

\* 2026 年统计到 11-20；整体呈逐年增长，与 HKJC 越洋转播赛日增多一致。

### 2.2 时间跨度

- 主站最早文章：`2017-07-01`（非出马表）；最早出马表：`2017-07-07`。
- 主站最新文章：`2026-11-24`（当天仍在更新），出马表最新 `2026-11-20`。
- 旧版归档仍可访问（示例）：
  - 中文 2016-12-23：[有馬紀念賽日 - 宣佈出賽馬匹名單](https://www.hkjc.com/chinese/corporate/racing_news_item.asp?in_file=/chinese/news/2016-12/news_2016122301016.html) — 与样本格式完全一致，正文直接列 `马名 / 练马师`。
  - 英文 2015-11-25：[2015 HKIR Selections Announcement](https://www.hkjc.com/english/corporate/racing_news_item.asp?in_file=/english/news/2015-11/news_2015112502053.html)。
  - 更早年份：路径形如 `/chinese/news/YYYY-MM/news_YYYYMMDDNNNN.html`，但旧站 `racing_news.asp` 已 301 到新站，2011 年的旧路径现跳维护页，**没有可靠的全量索引**；需要靠搜索引擎或互联网档案馆补漏。
- 另有结构化 PDF 出马表（2012 年已在用）：[2012-11-10 越洋转播出赛马匹名名单 PDF](https://racing.hkjc.com/racing/content/PDF/RaceCard/CO22919/osc20121110_starter_all.pdf)；近年为 `consvc.hkjc.com/-/media/Sites/JCRW/FormGuide/Simulcast/.../OSC*_starter_*.pdf`。

### 2.3 可以怎么检索（4 条路，按推荐度排序）

1. **REST API 批量枚举（最推荐）**
   - 主查询：`https://racingnews.hkjc.com/chinese/wp-json/wp/v2/posts?search=出賽馬匹名單&per_page=100&page=1&_fields=id,date,link,title`
   - 响应头 `X-WP-Total`、`X-WP-TotalPages` 给总数与页数；翻页到 `X-WP-TotalPages` 即可。本次 13 页 ≈ 1.3k 条，几十秒可拉完。
   - 时间过滤：`&after=2017-07-01T00:00:00&before=2018-01-01T00:00:00`。
   - 按 ID 取正文：`&include=97143,87723&_fields=id,title,content`（最终名单的正文可能只是 shortcode，附件需另取）。
   - 站点的 `robots.txt` 为 `User-agent: * / Allow: /`；API 响应带 `X-Robots-Tag: noindex`，只读公开数据，仍建议限速。
2. **站内搜索页**：`https://racingnews.hkjc.com/chinese/?s=出賽馬匹名單`（浏览器可用，分页；不适合批量）。
3. **按年/月/分类归档**：`https://racingnews.hkjc.com/chinese/?ny=2026&nm=6&ncat=simulcast`，每页 10 条。分类值实测有 `all / local / simulcast / racecard / events / circulars / general`；`ny/nm` 可回溯到 2017。适合人工核对与补漏。
4. **旧站定点访问**：`https://www.hkjc.com/chinese/corporate/racing_news_item.asp?in_file=/chinese/news/YYYY-MM/news_YYYYMMDDNNNN.html`（需已知文件名）；英文站同理换 `/english/`。

额外：`https://racingnews.hkjc.com/english/wp-json/wp/v2/posts?search=Entries&per_page=100` 是英文桥接稿的入口，对第 4 节的跨站匹配很关键。

---

## 3. 按国家和地区分类（含检索链接）

分类方法：按标题里的赛马日/赛事名关键词映射（如「鄧格錦標」=澳洲玫瑰岗、「莊柏德大賽」=法国多维尔、「卓亨佐夫錦標」=法国隆尚、「女皇伊利沙伯二世盃」=日本 11 月）。共 958 篇，仅 2 篇（`冠軍挑戰錦標賽馬日`）无法可靠定位，放在「待核对」。

| 国家/地区 | 篇数 | 代表赛事 | 最新一篇（直接链接） | 该地区检索入口（API） |
| --- | ---: | --- | --- | --- |
| 澳洲/新西兰 | 243 | 墨尔本杯、觉士盾、考菲尔德杯、邓格锦标、诺顿锦标、大都会锦标 | [澳洲冠军锦标赛马日 最终名单（2026-11-06）](https://racingnews.hkjc.com/chinese/2026/11/06/%e6%be%b3%e6%b4%b2%e5%86%a0%e8%bb%8d%e9%8c%a6%e6%a8%99%e8%b3%bd%e9%a6%ac%e6%97%a5%e6%9c%80%e5%be%8c%e5%ae%a3%e4%bd%88%e5%87%ba%e8%b3%bd%e9%a6%ac%e5%8c%b9%e5%90%8d%e5%96%ae%e5%8f%8a%e6%8e%92%e4%bd%8d/) | [search=墨爾本盃](https://racingnews.hkjc.com/chinese/wp-json/wp/v2/posts?search=%E5%A2%A8%E7%88%BE%E6%9C%AC%E7%9B%83&per_page=100) |
| 日本 | 236 | 宝塚纪念、有马纪念、日本杯、安田纪念、春秋天皇赏、女皇伊利沙伯二世盃 | [一哩冠军赛 最终名单（2026-11-20）](https://racingnews.hkjc.com/chinese/2026/11/20/%e4%b8%80%e5%93%a9%e5%86%a0%e8%bb%8d%e8%b3%bd%e6%9c%80%e5%be%8c%e5%ae%a3%e4%bd%88%e5%87%ba%e8%b3%bd%e9%a6%ac%e5%8c%b9%e5%90%8d%e5%96%ae%e5%8f%8a%e6%8e%92%e4%bd%8d/) | [search=寶塚](https://racingnews.hkjc.com/chinese/wp-json/wp/v2/posts?search=%E5%AF%B6%E5%A1%9A&per_page=100) |
| 英国/爱尔兰 | 172 | 雅士谷金杯、叶森打吡、英皇锦標、爱尔兰打吡、皇席锦标 | [柯尔锦标 最终名单（2026-11-12）](https://racingnews.hkjc.com/chinese/2026/11/12/%e6%9f%af%e7%88%be%e9%8c%a6%e6%a8%99%e6%9c%80%e5%be%8c%e5%ae%a3%e4%bd%88%e5%87%ba%e8%b3%bd%e9%a6%ac%e5%8c%b9%e5%90%8d%e5%96%ae%e5%8f%8a%e6%8e%92%e4%bd%8d/) | [search=雅士谷](https://racingnews.hkjc.com/chinese/wp-json/wp/v2/posts?search=%E9%9B%85%E5%A3%AB%E8%B0%B7&per_page=100) |
| 法国 | 109 | 凯旋门大赛、法国打吡/橡树、隆尚磨坊、杰克莫华、庄柏德、卓亨佐夫 | [凯旋门大赛日 最终名单（2026-10-03）](https://racingnews.hkjc.com/chinese/2026/10/03/%e5%87%b1%e6%97%8b%e9%96%80%e5%a4%a7%e8%b3%bd%e6%97%a5%e6%9c%80%e5%be%8c%e5%ae%a3%e4%bd%88%e5%87%ba%e8%b3%bd%e9%a6%ac%e5%8c%b9%e5%90%8d%e5%96%ae%e5%8f%8a%e6%8e%92%e4%bd%8d/) | [search=凱旋門](https://racingnews.hkjc.com/chinese/wp-json/wp/v2/posts?search=%E5%87%B1%E6%97%8B%E9%96%80&per_page=100) |
| 中东（阿联酋/沙特） | 51 | 杜拜世界杯赛马日、杜拜超级星期六、沙特杯、金挑战 | [金挑战大赛日 最终名单（2026-06-05）](https://racingnews.hkjc.com/chinese/2026/06/05/%e9%87%91%e6%8c%91%e6%88%b0%e5%a4%a7%e8%b3%bd%e6%97%a5-%e6%9c%80%e5%be%8c%e5%ae%a3%e4%bd%88%e5%87%ba%e8%b3%bd%e9%a6%ac%e5%8c%b9%e5%90%8d%e5%96%ae%e5%8f%8a%e6%8e%92%e4%bd%8d/) | [search=杜拜](https://racingnews.hkjc.com/chinese/wp-json/wp/v2/posts?search=%E6%9D%9C%E6%8B%9C&per_page=100) |
| 德国/欧洲其他 | 50 | 拜仁大赛、巴登大赛、德国打吡、柏林大赛 | [拜仁大赛日 最终名单（2026-11-06）](https://racingnews.hkjc.com/chinese/2026/11/06/%e6%8b%9c%e4%bb%81%e5%a4%a7%e8%b3%bd%e6%97%a5%e6%9c%80%e5%be%8c%e5%ae%a3%e4%bd%88%e5%87%ba%e8%b3%bd%e9%a6%ac%e5%8c%b9%e5%90%8d%e5%96%ae%e5%8f%8a%e6%8e%92%e4%bd%8d/) | [search=拜仁](https://racingnews.hkjc.com/chinese/wp-json/wp/v2/posts?search=%E6%8B%9C%E4%BB%81&per_page=100) |
| 新加坡 | 26 | 新加坡金杯、克兰芝一哩赛（2024 后停办） | [克兰芝一哩赛日 最终名单（2024-05-17）](https://racingnews.hkjc.com/chinese/2024/05/17/%e5%85%8b%e8%98%ad%e8%8a%9d%e4%b8%80%e5%93%a9%e8%b3%bd%e9%a6%ac%e6%97%a5%e6%9c%80%e5%be%8c%e5%ae%a3%e4%bd%88%e5%87%ba%e8%b3%bd%e9%a6%ac%e5%8c%b9%e5%90%8d%e5%96%ae%e5%8f%8a%e6%8e%92%e4%bd%8d/) | [search=克蘭芝](https://racingnews.hkjc.com/chinese/wp-json/wp/v2/posts?search=%E5%85%8B%E8%98%AD%E8%8A%9D&per_page=100) |
| 香港 | 25 | 宝马香港打吡、香港经典盃/一哩赛、马会短途锦標 | [宝马香港打吡 2026 优先名单（2026-03-13）](https://racingnews.hkjc.com/chinese/2026/03/13/%e5%af%b6%e9%a6%ac%e9%a6%99%e6%b8%af%e6%89%93%e5%90%a1%e5%a4%a7%e8%b3%bd2026%e5%84%aa%e5%85%88%e5%87%ba%e8%b3%bd%e9%a6%ac%e5%8c%b9%e5%90%8d%e5%96%ae/) | [search=香港打吡](https://racingnews.hkjc.com/chinese/wp-json/wp/v2/posts?search=%E9%A6%99%E6%B8%AF%E6%89%93%E5%90%A1&per_page=100) |
| 美国/加拿大 | 14 | 育马者杯系列、加州冠冕 | [育马者杯赛马日 最终名单（2026-10-30）](https://racingnews.hkjc.com/chinese/2026/10/30/%e8%82%b2%e9%a6%ac%e8%80%85%e7%9b%83%e8%b3%bd%e9%a6%ac%e6%97%a5%e6%9c%80%e5%be%8c%e5%ae%a3%e4%bd%88%e5%87%ba%e8%b3%bd%e9%a6%ac%e5%8c%b9%e5%90%8d%e5%96%ae%e5%8f%8a%e6%8e%92%e4%bd%8d/) | [search=育馬者盃](https://racingnews.hkjc.com/chinese/wp-json/wp/v2/posts?search=%E8%82%B2%E9%A6%AC%E8%80%85%E7%9B%83&per_page=100) |
| 南非 | 12 | 德班七月让赛、南非打吡 | [德班七月让赛 最终名单（2026-07-03）](https://racingnews.hkjc.com/chinese/2026/07/03/%e5%be%b7%e7%8f%ad%e4%b8%83%e6%9c%88%e8%ae%93%e8%b3%bd%e6%9c%80%e5%be%8c%e5%ae%a3%e4%bd%88%e5%87%ba%e8%b3%bd%e9%a6%ac%e5%8c%b9%e5%90%8d%e5%96%ae%e5%8f%8a%e6%8e%92%e4%bd%8d/) | [search=德班](https://racingnews.hkjc.com/chinese/wp-json/wp/v2/posts?search=%E5%BE%B7%E7%8F%AD&per_page=100) |
| 韩国 | 11 | 韩国杯、韩国短途锦標 | [韩国短途锦標 最终名单（2026-09-04）](https://racingnews.hkjc.com/chinese/2026/09/04/%e9%9f%93%e5%9c%8b%e7%9f%ad%e9%80%94%e9%8c%a6%e6%a8%99%e6%9c%80%e5%be%8c%e5%ae%a3%e4%bd%88%e5%87%ba%e8%b3%bd%e9%a6%ac%e5%8c%b9%e5%90%8d%e5%96%ae%e5%8f%8a%e6%8e%92%e4%bd%8d/) | [search=韓國](https://racingnews.hkjc.com/chinese/wp-json/wp/v2/posts?search=%E9%9F%93%E5%9C%8B&per_page=100) |
| 南美 | 6 | 圣保罗赛马会大赛、拉丁美洲大赛 | [圣保罗+拉丁美洲大赛 最终名单（2025-10-17）](https://racingnews.hkjc.com/chinese/2025/10/17/%e8%81%96%e4%bf%9d%e7%be%85%e8%b3%bd%e9%a6%ac%e6%83%83%e5%a4%a7%e8%b3%bd%e5%8f%8a%e6%8b%89%e4%b8%81%e7%be%8e%e6%b4%b2%e5%a4%a7%e8%b3%bd%e6%9c%80%e5%be%8c%e5%ae%a3%e4%bd%88%e5%87%ba%e8%b3%bd%e9%a6%ac/) | [search=聖保羅](https://racingnews.hkjc.com/chinese/wp-json/wp/v2/posts?search=%E8%81%96%E4%BF%9D%E7%BE%85&per_page=100) |
| 中国内地 | 1 | 从化马场速度马术比赛 | [2019 从化马场速度马术比赛（2019-03-19）](https://racingnews.hkjc.com/chinese/2019/03/19/2019%e5%be%9e%e5%8c%96%e9%a6%ac%e5%a0%b4%e9%80%9f%e5%ba%a6%e9%a6%ac%e8%a1%93%e6%af%94%e8%b3%bd-%e5%ae%a3%e4%bd%88%e5%87%ba%e8%b3%bd%e9%a6%ac%e5%8c%b9%e5%90%8d%e5%96%ae/) | [search=從化](https://racingnews.hkjc.com/chinese/wp-json/wp/v2/posts?search=%E5%BE%9E%E5%8C%96&per_page=100) |
| 待核对 | 2 | 冠军挑战锦标赛马日（2018） | [2018-05-03 最终名单](https://racingnews.hkjc.com/chinese/2018/05/03/%e5%86%a0%e8%bb%8d%e6%8c%91%e6%88%b0%e9%8c%a6%e6%a8%99%e8%b3%bd%e9%a6%ac%e6%97%a5-%e6%9c%80%e5%be%8c%e5%ae%a3%e4%bd%88%e5%87%ba%e8%b3%bd%e9%a6%ac%e5%8c%b9%e5%90%8d%e5%96%ae%e5%8f%8a%e6%8e%92/) | 主查询 + 人工核对 |

> 全部 956 条的 `日期 / 地区 / 标题 / 链接` 见同目录 **`hkjc_entries_index.csv`**（UTF-8 BOM，可直接 Excel 打开）。地区是「赛事所属赛马日」口径，**不是参赛马产地**；一篇日本赛马日公告里也会出现 `(USA)`、`(IRE)` 马匹。马匹产地要靠附件马名字段里的 `(JPN)`/`(FR)` 后缀单独判定——这也是「只维护日本马译名」能自动执行的前提（见 `SCHEMA.md` §2.1）。

---

## 4. netkeiba 关联可行性：实测链路

以用户样本同日的 2026 宝塚纪念（G1，阪神 2200m）为例，全部请求普通 HTTP GET + 常规 UA 即返回 200，无需登录。

### 步骤 1：日期 → 当日全部赛事

- `https://db.netkeiba.com/race/list/20260614/` → 页面标题 `2026年06月14日のレース情報 | 競馬データベース - netkeiba`，共解析出 **81 场**（中央 + 地方）。
- 其中：`11R / 202609030411 / 宝塚記念(GI)`，即 race_id = `202609030411`。
- race_id 12 位结构：`YYYY(4) + 赛场(2) + 场次(2) + 日(2) + R(2)`；`09` = 阪神，`03` = 第 3 回，`04` = 第 4 日，`11` = 第 11 场。**注意它不直接编码日历日期**，所以要靠日期列表页做「日期 → race_id」映射，不能自己拼日期。

### 步骤 2：赛事 → 马匹 ID（两种语言都能对）

- 日文结果页：`https://db.netkeiba.com/race/202609030411/`，标题 `宝塚記念(GI)｜2026年6月14日`，解析出 **18 匹**马的 ID 与日文名，如 `2021103272 / メイショウタバル`。
- 英文结果页：`https://en.netkeiba.com/db/race/202609030411/`，标题 `TAKARAZUKA KINEN (G1) Full Result | 14 JUN 2026 R11 Hanshin`，同一批 ID，马名为英文：`Meisho Tabaru`（ID 仍为 `2021103272`）。
- HKJC 英文桥接稿：[Takarazuka Kinen Day – Declared Runners（2026-06-12）](https://racingnews.hkjc.com/english/2026/06/12/takarazuka-kinen-day-declared-runners/)，以及 [Takarazuka Kinen Day – Entries（2026-06-11）](https://racingnews.hkjc.com/english/2026/06/11/takarazuka-kinen-day-entries/)。HKJC 中文稿里的 `MEISHO TABARU (JPN) 名將田原 (日)` 与英文版 netkeiba 的 `Meisho Tabaru` 是同一个键（大写、去产地码即可匹配）。

### 步骤 3：马匹 ID → 跨场赛事史

- `https://en.netkeiba.com/db/horse/2021103272/`（Meisho Tabaru 档案页）实测列出 **9 条**带 race_id 的赛事链接，包括 `202509030411` ('25 TAKARAZUKA KINEN)、`202609030411` (TAKARAZUKA KINEN)、`202609020411` (OSAKA HAI)、`202506050811` (ARIMA KINEN) 等。
- 于是天然形成图：**HKJC 出马表 → race_id → horse_id → 该马所有 race_id → 其他马**。「把比赛和比赛关联起来、把马和马关联起来」在数据模型上是成立的。

### 步骤 4：还需要确认的边界

- 赛前 vs 赛后：宣布名单与实际出赛可能不同（退赛/更换骑师）。netkeiba 结果页是**实际出赛**；赛前卡用 `race.netkeiba.com/race/shutaba.html?race_id=...`（本次对已结束赛事返回 404，未来赛日需另验）。做历史关联（本报告场景）用结果页最稳。
- 只覆盖日本赛：JRA 中央 + NAR 地方都在日期列表里；HKJC 公告里的澳洲/欧洲/美国赛马日不能接到 netkeiba，需要各自本土数据源。
- 英文版 race 列表 URL 会 302，**用日文站做日期索引、英文站做名称/马名对齐**是可行组合。
- `db.netkeiba.com/robots.txt` 与 `www.netkeiba.com/robots.txt` 均为 404（未发布规则），并非「允许抓取」；请控制频率（立项后口径 **≥5 秒/请求**）、只取必要页面、遵守站点条款。HKJC `robots.txt` 明确 `Allow: /`。

---

## 5. 优化思路（如果要落地）

1. **英文桥接，绕开中日赛名翻译**：HKJC 中文稿和英文稿同日同赛事（slug 通用），先用日期 + HKJC 英文赛事名去 en.netkeiba 赛事页标题匹配，再用同一个 race_id 取日文页的马匹 ID。比「宝塚紀念 → 宝塚記念」的日汉转换稳。
2. **日期 + 场次号双重定位**：HKJC 稿常带 `S2-7`、`S1-6` 编号，netkeiba 有 R 番号；先用日期缩到 1 个赛马日，再用 R 番/赛事名二次确认，避免同名赛（如各地打吡）撞车。
3. **建立赛事名映射字典**：227 个标题前缀里 Top 20 覆盖大半；把「寶塚紀念賽 ↔ TAKARAZUKA KINEN ↔ 宝塚記念」这类固定映射存表，剩余走模糊匹配，命中率与可解释性都更好。
4. **马名规范化**：`upper → strip 产地码 → 去标点/连字符/所有格 → 去 HKJC 中文译名`；用 `(JPN)` 作为国家过滤；再用练马师姓氏（HKJC 英文 vs netkeiba 英文缩写，如 `Mitsumasa Nakauchida` / `M. Nakauchida`）做二次校验。
5. **区分两种正文解析**：内联 HTML 表格用 BeautifulSoup 直接抽；「最后宣布」类抽 `.docx` 附件链接（形如 `res.hkjc.com/racingnews/wp-content/uploads/sites/2/YYYY/MM/..._final-delc-C.docx`）后用 `python-docx` 读表；PDF 赛卡用 `pdfplumber`。统一落成 `race / horse / entry / evidence_url` 四张表。
6. **缓存与增量**：以 race_id 和 horse_id 为键落本地缓存（本项目可放 `data/` 之外的探索缓存），HKJC 按发布日期增量拉、netkeiba 按 race_id 补；不要每次全量重爬。
7. **交叉校验**：用 HKJC 赛果新闻或 netkeiba 结果页反查头马，核对「宣布名单匹数 == 结果页出赛匹数」，不一致则标记 `scratched` 而不是丢弃。
8. **旧站补漏**：2015–2016 中文/英文旧新闻用已知 file path 批量探测（合法的 HEAD 请求），命中后与 WP 数据合并；不要指望它有搜索 API。
9. **合规与稳态**：HKJC 可正常限流抓取；netkeiba 无 robots 规则，按「低频、只读、可追溯」处理，不做镜像式全站抓取；若未来要做产品化，优先找官方/授权数据源。
10. **与本站（絆）的接法**：本站在 `data/`、`config/sire.json` 口径中运行，探索脚本应放 `tests/`，产物先落探索目录验证；确认可用后再决定是否引入正式管线（不要改 `front/`、`scripts/` 这些随母版同步的目录）。

---

## 6. 风险清单

- 分类是基于赛事名的启发式，个别冷门赛（如「冠軍挑戰錦標」）未定位；CSV 中保留原始标题可随时重分类。
- 宽口径 1266 vs 严格 958 的差异来自正文提及；做统计时务必注明口径。
- 「最后宣布名单」多为附件，若只抓 HTML 会误判为空数据。
- 2017 年以前没有可靠的全量索引，所谓「最远」只能给到已验证的 2015/2016 存量，不能宣称完整覆盖。
- netkeiba 的 horse_id 是该站自有 ID，跨站（如 JRA 官方、HKJC）并非通用键，需要保留来源标识。

---

## 7. 复查与修订（2026-11-24 二次核对）

复核方法：把中文站 **19,368 篇**文章标题全量枚举（`orderby=id&order=asc`，194 页，2 分 45 秒，零失败）后重新统计，并对附件做了真实下载与解析。结论：**主结论成立，但有 7 处需要修正或补强**。

### 7.1 站住了的

| 项 | 复核结果 |
| --- | --- |
| 958 这个数字 | ✅ 全量枚举后标题含「出賽馬匹名單」的正好 **958** 篇，与关键词搜索结果完全一致——说明上一轮没有漏检，只是方法上"先猜词再验证"，不如全量枚举确定。 |
| 时间跨度 | ✅ 全站 2017-07-03 ~ 2026-11-24；出马表 2017-07-07 ~ 2026-11-20。 |
| 地区分类 | ✅ CSV 去重后 956 条（原 958 行含 2 条重复 URL，已修正）。 |

### 7.2 必须修正的

1. **附件域名是坑，上一轮没下载就下了结论。** 文章里的附件是**协议相对链接**（`href="//res.hkjc.com/racingnews/wp-content/uploads/..."`），绝对化时极容易拼成 `racingnews.hkjc.com/wp-content/...`——该地址返回 **500**。真实可下载域名有三个，必须做白名单：
   - `res.hkjc.com/racingnews/wp-content/uploads/sites/2/...`（2018 至今，主力）
   - `corporate.hkjc.com/image/YYYYMM/...`（较早期）
   - `racing.hkjc.com/image/YYYYMM/...`（零散）
2. **附件有两种格式，`.doc` 不能用 python-docx 读。** 抽样实测：`.docx` 是真 ZIP（`504b0304`），`.doc` 是 OLE2 复合文档（`d0cf11e0`）。2026 年仍有 `.doc` 与 `.docx` 混用，2017–2025 基本是 `.doc`。所以管线必须有 `soffice --headless --convert-to docx`（或 antiword，但表格支持差）这一步。上一轮写"用 python-docx 读表"是不完整的。
3. **字段比我写的全得多，价值被低估了。** 实际解析 2026-11-20 一哩冠军赛附件，表头是 13 列：`馬號 / 配備 / Horse Name(产地) / 馬名 / 排位 / 國際評分 / 配磅 / 性別 / 年齡 / Trainer / 練馬師 / Jockey / 騎師`，表前还有赛事抬头：`一哩冠軍賽 (一級賽) - 一六○○米 草地 / 二○二六年十一月二十二日星期日 / 日本京都競馬場 / 海外賽事第一組 第一場`。**一份附件 = 一场完整赛事 + 一次人工双语对齐**（20 匹马 × 马名/骑师/练马师三组对照）。这不是"附带数据"，这是港译词典的原始语料。
4. **体量估算要上调一个量级。** 抽样 6 篇 2026 年附件，每篇含 1–8 张表、20–132 行，**篇均约 79 条出赛记录**。958 篇 × 79 ≈ **7.5 万条 entry**（去重后不同马匹保守估计 2 万匹以上）。上一轮完全没做这个估算。
5. **漏掉了站内更大的语料。** 全量枚举发现除出马表外还有：`參賽馬匹簡評` **2838 篇**、`報名馬匹` 620 篇、`獸醫` 822 篇、`退出` 807 篇、`晨操` 340 篇、`試閘` 311 篇、`獲選馬匹` 65 篇、`優先出賽` 24 篇。其中简评是**逐马中文评述、没有英文名**——能做内容层和中文名扩充，但**不能**做中英对齐。
6. **分页不是无脑可翻的。** 连续快速请求会返回 **400**（本次首次枚举就是这样连挂 3 页），退避重试后全部恢复。上一轮"13 页几十秒拉完"的表述偏乐观；实际 194 页需要 ~2m45s 且必须带退避。另：`orderby=date&order=asc` 的分页比 `orderby=id&order=asc` 更易触发 400，建议后者。
7. **netkeiba 侧只验了一场，鲁棒性未验。** 只测了 2026-06-14 阪神 G1 一个日期。未验证：无赛事日、未来日期、地方赛（NAR）与中央赛混排、赛事取消、以及非 G1 场次的马名写法（G1 英文名规整，条件赛/让赛可能日英混杂）。

### 7.3 方法上该改的

8. **别用关键词搜索当全量统计。** 正确姿势是 `per_page=100&orderby=id&order=asc` 枚举全站标题（194 页），本地过滤。搜索接口（`X-WP-Total`）只适合快速探数量。
9. **"关联成功"需要先定指标再验。** 应分三层：race 级命中率、entry 级命中率、名称一致率。上一轮只有"跑通了"的定性结论，没有可验收的数字。
10. **没接已有资产。** 项目里已有 `tests/hk-name-match-2026-11-02.csv`（63 匹日本马的中日英对照）和 `tests/jockey-name-match-2026-11-02.csv`（483 名骑手），上一轮完全没用上——这些正是词典的种子数据。

---

## 附录：本次探索产物与复现

- `data-src/hkjc_entries_index.csv` — 956 条（去重后）文章的日期/地区/标题/URL。
- `pipeline/_explore/fetch_wp.py` — WordPress API 批量拉取脚本（探索期按关键词搜）。
- `pipeline/s0_enumerate_all.py` — 全站 19,368 篇标题全量枚举（带退避重试，复核用）。
- `pipeline/s1_classify.py` — 地区归类与 CSV 生成脚本。
- `data-src/samples/20261120_MileChampionship_FinalDecl_C.docx` — 实测下载的最终名单附件（13 列双语表）。

> 立项后本报告的产物已从 `kzn-web/tests/` 迁入本仓库，路径以上述为准。
- 关键实测 URL：
  - 样本文章：`https://racingnews.hkjc.com/chinese/2026/06/12/...`
  - WP 主查询：`https://racingnews.hkjc.com/chinese/wp-json/wp/v2/posts?search=出賽馬匹名單&per_page=100`
  - 日期归档：`https://racingnews.hkjc.com/chinese/?ny=2026&nm=6&ncat=simulcast`
  - netkeiba 日期列表：`https://db.netkeiba.com/race/list/20260614/`
  - netkeiba 宝塚纪念：`https://db.netkeiba.com/race/202609030411/`
  - netkeiba 英文同场：`https://en.netkeiba.com/db/race/202609030411/`
  - netkeiba 马匹档案：`https://en.netkeiba.com/db/horse/2021103272/`
