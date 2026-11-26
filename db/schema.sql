-- 马名通译 · 工作库表结构（v3 · 唯一出处）
-- 改表先改这里，再用 pipeline/build_db.py 重建；不要手改 *.sqlite。
--
-- v3 与 v2 的差别（结构定型，勿回退）：
--   1. 名称统一进 name 表：四栏（original / en / hk / own）都是行，不是列。
--      horse / race / person 三张事实表里不再有 cn_name / local_name 这类名称列。
--   2. 简体 sc 不入库——它是 hk 的纯函数（逐字繁转简），落了库就会出现「hk 改了 sc 没改」的脏数据。
--   3. 新增 entity_alias：简称 / 缩写，多对一，人工维护，与名称层完全分开。
--   4. horse.curation 保留（curated / pending）：抓取全量、译名只维护日本。

PRAGMA foreign_keys = ON;

-- ── 赛事：一份公告 = 一场赛事 ─────────────────────────
CREATE TABLE IF NOT EXISTS race (
  race_uid      TEXT PRIMARY KEY,          -- 本站主键，如 jra-2026-mile-championship
  announce_date TEXT NOT NULL,             -- 公告发布日 YYYY-MM-DD
  race_date     TEXT,                      -- 赛期，来自公告表头（中文数字已转阿拉伯）
  race_time     TEXT,                      -- 开跑时间 HH:MM（24 小时制）
  session       TEXT,                      -- 场次，如「海外賽事第一組 第七場」
  grade         TEXT,                      -- G1 / G2 / G3 / Listed / Handicap
  grade_cn      TEXT,                      -- 公告原文写法，如「一級賽」
  venue         TEXT,                      -- 马场原文，如「京都競馬場」
  venue_country TEXT,                      -- 举办地（国籍列的取值）
  course        TEXT,                      -- 去掉国名的马场名
  distance      TEXT,                      -- 「1600 米」
  surface       TEXT,                      -- 草地 / 泥地 / 全天候 / 膠沙地
  first_seen_at TEXT NOT NULL              -- 首次入队时间
);

CREATE INDEX IF NOT EXISTS idx_race_date ON race(race_date);
CREATE INDEX IF NOT EXISTS idx_race_announce ON race(announce_date);

-- ── 马匹 ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS horse (
  horse_uid    TEXT PRIMARY KEY,           -- 前缀 = 产地小写：jp- / fr- / gb- / ire- …
  birth_year   INTEGER,                    -- 只填有出处的（JBIS 生年 / 公告可推）
  birth_date   TEXT,                       -- YYYY-MM-DD，只有 JBIS 基本情報页给得到
  sex          TEXT,                       -- M / F / G（牡 / 牝 / 骟）
  color        TEXT,                       -- 毛色，JBIS 原样（鹿毛 / 栗毛 / 芦毛 …）
  country_code TEXT,                       -- 产地 ISO-3166-1 alpha-3，公告后缀直取
  origin_basis TEXT,                       -- country_code 来源：docx_suffix / jbis / manual
  status       TEXT,                       -- active / retired / deceased / unknown
  sire_uid     TEXT,                       -- 父马 uid（同库内可空）
  dam_uid      TEXT,                       -- 母马 uid
  -- 译名维护级别：curated = 已人工核对；pending = 只有公告港译，未复核。
  -- 抓取范围与维护范围解耦：所有马都入库，只有 curated 进译名维护队列。
  curation     TEXT NOT NULL DEFAULT 'pending'
);

CREATE INDEX IF NOT EXISTS idx_horse_curation ON horse(curation, country_code);
CREATE INDEX IF NOT EXISTS idx_horse_birth ON horse(birth_year, country_code);

-- ── 血统图：一匹 62 格（父系 31 + 母系 31）────────────────
-- 为什么不塞进 horse 表：5 代是 62 行数据，塞 JSON 进来就没法按「父/母/母父」
-- 反查；拍平成行才能一行 SQL 回答「谁的父亲是キセキ」。
-- side 只有 父 / 母；depth 1..5；idx 0..2^(depth-1)-1（第 5 代 16 格）。
-- 空位不入库（JBIS 表尾缺格），页面按 depth 补空。
CREATE TABLE IF NOT EXISTS pedigree_node (
  horse_uid  TEXT NOT NULL,                -- 本库马 uid（不是 JBIS id）
  side       TEXT NOT NULL,                -- 父 / 母
  depth      INTEGER NOT NULL,             -- 1..5
  idx        INTEGER NOT NULL,
  name       TEXT NOT NULL,                -- JBIS 原样（假名 / 汉字 / 外来马带 (USA)）
  sex        TEXT,                         -- 牡 / 牝
  birth_year INTEGER,
  color      TEXT,
  jbis_id    TEXT,                         -- 节点自身的 JBIS id（有就能跳过去）
  PRIMARY KEY (horse_uid, side, depth, idx)
);

CREATE INDEX IF NOT EXISTS idx_ped_name ON pedigree_node(name);
CREATE INDEX IF NOT EXISTS idx_ped_jbis ON pedigree_node(jbis_id);

-- 血统一页的附加信息：ファミリーNo / クロス，取自 JBIS 血統页表尾
CREATE TABLE IF NOT EXISTS pedigree_meta (
  horse_uid  TEXT PRIMARY KEY,
  jbis_id    TEXT NOT NULL,
  fno        TEXT,                         -- ファミリーNo，如 F16-c
  cross      TEXT,                         -- クロス，如 サンデーサイレンス(USA)：S3×M4
  source_url TEXT NOT NULL,
  fetched_at TEXT NOT NULL
);

-- ── 人物：骑手 / 练马师 ───────────────────────────────
CREATE TABLE IF NOT EXISTS person (
  person_uid       TEXT PRIMARY KEY,       -- j-<slug> / t-<slug>，角色前缀防同名撞键
  role             TEXT NOT NULL,          -- jockey / trainer
  country_code     TEXT,                   -- 国籍（公告不给，日本籍按公告所属判定）
  nationality_basis TEXT                   -- docx_list / manual / jbis
);

CREATE INDEX IF NOT EXISTS idx_person_role ON person(role, country_code);

-- ── 名称：四栏都在这张表里 ───────────────────────────
-- 一条名称 = 一行。field 就是页面上的那一栏；缺栏就是没有行，不写空串。
-- sc（简体）不入库：导出时由 hk 逐字转写，见 docs/SCHEMA.md §3。
CREATE TABLE IF NOT EXISTS name (
  entity_type TEXT NOT NULL,               -- horse / race / person
  entity_uid  TEXT NOT NULL,
  field       TEXT NOT NULL,               -- original / en / hk / own
  lang        TEXT NOT NULL,               -- ja / zh-Hant / zh-Hans / en / fr
  value       TEXT NOT NULL,
  is_primary  INTEGER NOT NULL DEFAULT 0,  -- 同一 field 内只能有一个主名
  source_url  TEXT NOT NULL,               -- 出处：公告页 / 附件 / JBIS
  archive_url TEXT,                        -- 有存档就填回放地址，没有必须留空并在页面标注
  decided_at  TEXT NOT NULL,               -- 该名称定稿日（人工判断日 / 抓取日）
  PRIMARY KEY (entity_type, entity_uid, field, value),
  CHECK (field IN ('original', 'en', 'hk', 'own'))
);

CREATE INDEX IF NOT EXISTS idx_name_lookup ON name(entity_type, entity_uid, field);
CREATE INDEX IF NOT EXISTS idx_name_value ON name(value);

-- ── 简称 / 缩写：多对一，人工维护，与名称层无关 ─────────
-- 例：傲登龍門 → M.傲 / S.门。它不是译名、不进分级、不参与主名选择，
-- 只用于检索与写稿省略。seq 决定页面展示顺序（人工排的，不是字母序）。
CREATE TABLE IF NOT EXISTS entity_alias (
  entity_type TEXT NOT NULL,
  entity_uid  TEXT NOT NULL,
  seq         INTEGER NOT NULL DEFAULT 0,
  text        TEXT NOT NULL,
  note        TEXT,                        -- 为什么这么简写，可空
  updated_at  TEXT NOT NULL,
  PRIMARY KEY (entity_type, entity_uid, text)
);

CREATE INDEX IF NOT EXISTS idx_alias_text ON entity_alias(text);
CREATE INDEX IF NOT EXISTS idx_alias_entity ON entity_alias(entity_type, entity_uid, seq);

-- ── 出赛记录（唯一的事实表；不做赛果维护，finish 通常为空）──
CREATE TABLE IF NOT EXISTS entry (
  race_uid     TEXT NOT NULL REFERENCES race(race_uid) ON DELETE CASCADE,
  horse_uid    TEXT NOT NULL REFERENCES horse(horse_uid) ON DELETE CASCADE,
  saddle_no    INTEGER,                    -- 马号
  draw         INTEGER,                    -- 档位
  jockey_uid   TEXT,
  trainer_uid  TEXT,
  weight_lb    REAL,                       -- 负磅
  claim_lb     REAL,                       -- 骑师减磅标注（-3 = 减 3 磅；正数 = 超磅）；NULL = 告示板未标
  intl_rating  INTEGER,                    -- 评分
  gear         TEXT,                       -- 配戴装备，如 B
  sex          TEXT,
  age          INTEGER,
  finish       INTEGER,                    -- 只有抓到赛果才填；NULL = 没跑/未出赛，不用 0 代替
  scraped_from TEXT,                       -- hkjc_docx / hkjc_table
  doc_url      TEXT,
  PRIMARY KEY (race_uid, horse_uid)
);

CREATE INDEX IF NOT EXISTS idx_entry_horse ON entry(horse_uid);
CREATE INDEX IF NOT EXISTS idx_entry_jockey ON entry(jockey_uid);
CREATE INDEX IF NOT EXISTS idx_entry_trainer ON entry(trainer_uid);

-- ── 外部 ID 映射 ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS xid (
  entity_type TEXT NOT NULL,               -- horse / race / person
  entity_uid  TEXT NOT NULL,
  system      TEXT NOT NULL,               -- jbis / netkeiba / racingpost / hkjc
  external_id TEXT NOT NULL,
  confidence  REAL NOT NULL DEFAULT 1.0,
  PRIMARY KEY (entity_type, entity_uid, system, external_id)
);

CREATE INDEX IF NOT EXISTS idx_xid_external ON xid(system, external_id);

-- ── 对外引用的存档快照（Internet Archive）────────────
-- 原件只作解析中间件，不入库；对外「查看原文」指向 snapshot_url。
CREATE TABLE IF NOT EXISTS archive (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  target_url     TEXT NOT NULL,
  target_kind    TEXT NOT NULL,            -- article / attachment
  entity_type    TEXT,                     -- race / horse / person
  entity_uid     TEXT,
  status         TEXT NOT NULL DEFAULT 'pending',  -- pending / success / failed / skipped
  capture_method TEXT,                     -- save_get / spn2_api / cdx_existing
  job_id         TEXT,                     -- SPN2 任务号（capture_method = spn2_api 时有值）
  snapshot_ts    TEXT,                     -- 14 位快照时间戳
  snapshot_url   TEXT,                     -- https://web.archive.org/web/{ts}/{url}
  http_status    INTEGER,
  mimetype       TEXT,
  sha1           TEXT,                     -- 本地原件校验值，用于去重
  bytes          INTEGER,
  submitted_at   TEXT,
  checked_at     TEXT,
  error          TEXT,
  UNIQUE(target_url, target_kind)
);

CREATE INDEX IF NOT EXISTS idx_archive_entity ON archive(entity_type, entity_uid);
CREATE INDEX IF NOT EXISTS idx_archive_status ON archive(status);

-- ── 待人工确认：合并 / 译名 / 简称撞车 ─────────────────
CREATE TABLE IF NOT EXISTS review_queue (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  kind        TEXT NOT NULL,               -- merge / name / alias / xid
  entity_type TEXT,
  entity_uid  TEXT,
  payload     TEXT NOT NULL,               -- JSON
  reason      TEXT,
  status      TEXT NOT NULL DEFAULT 'open',
  created_at  TEXT NOT NULL,
  resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_review_open ON review_queue(status, kind);

-- ── 人工图层 ────────────────────────────────────────
-- 人工判断不落这张库，落在 data-src/name-overrides.json（进 git，可 review、可回溯）。
-- 管线每次重建最后套用；这里只留一份套用记录，方便排查「这个名是谁改的」。
CREATE TABLE IF NOT EXISTS override_apply (
  applied_at  TEXT NOT NULL,
  overrides_sha TEXT NOT NULL,             -- name-overrides.json 的 sha256
  entity_type TEXT NOT NULL,
  entity_uid  TEXT NOT NULL,
  field       TEXT NOT NULL,               -- hk / own / alias
  old_value   TEXT,
  new_value   TEXT
);

CREATE INDEX IF NOT EXISTS idx_override_entity ON override_apply(entity_type, entity_uid);
