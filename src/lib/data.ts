/**
 * 站点数据入口（构建期读分片，运行期不 fetch 实体数据）。
 *
 * 数据由 pipeline/s5_export.py 生成到 public/data/：
 *   manifest.json              版本 / 计数 / curation 覆盖 / 分片数
 *   horses|races|persons/shard-NNN.json   每片 500，带 _meta
 *   search/all.json            名称检索索引（归一化名称 → {kind, uid}）
 *   search/rows.json           索引页客户端渲染用的紧凑行
 *
 * 分片只是**存储形态**：页面上仍是一实体一 URL，uid 直接进路径。
 * 模型不变：每个实体四栏名称（原文/英文/港译/自译）+ 派生简体 + 人工简称。
 */
import manifestRaw from '../../public/data/manifest.json';

export type Alias = { text: string; note: string | null };

export type Names = {
  original: string | null;
  en: string | null;
  hk: string | null;
  /** 港译逐字转简体，派生列，不可编辑 */
  sc: string | null;
  own: string | null;
  /** 简称 / 缩写：多对一，人工维护，不属于「几栏名称」，也不参与分级 */
  alias: Alias[];
};

/** 血统格：深代只有名字与生年，1-3 代才带毛色与自身 JBIS id（见 s5_export 的取舍） */
export type PedigreeNode = {
  /** 名字，JBIS 原样 */
  n: string;
  /** 牡 / 牝 */
  s?: string;
  /** 生年 */
  y?: number;
  /** 毛色 */
  c?: string;
  /** 节点自身的 JBIS id，可跳转 */
  id?: string;
};

export type Pedigree = {
  source: string;
  jbis_id: string | null;
  birth_date: string | null;
  birth_year: number | null;
  color: string | null;
  /** ファミリーNo */
  fno: string | null;
  /** クロス */
  cross: string | null;
  sire: string | null;
  dam: string | null;
  /** 父父（祖父） */
  siresire: string | null;
  /** 母父（外祖父） */
  damsire: string | null;
  /** 父系 31 格 / 母系 31 格，行深 1/2/4/8/16，空位 null */
  tree: { 父: (PedigreeNode | null)[][]; 母: (PedigreeNode | null)[][] };
  fetched_at: string;
};

export type Entry = {
  race_uid: string;
  no: number | null;
  draw: number | null;
  gear: string | null;
  horse_uid?: string;
  jockey_uid: string | null;
  trainer_uid: string | null;
  lb: number | null;
  /** 骑师减磅标注：-3 = 减 3 磅，正数 = 超磅，null = 告示板未标 */
  claim?: number | null;
  rating: number | null;
  sex: string | null;
  age: number | null;
};

export type Horse = {
  uid: string;
  names: Names;
  origin: string;
  country: string;
  sex: string | null;
  sex_cn: string;
  /** 出生日期 / 生年 / 毛色：只有 JBIS 给得到，取不到就是 null */
  birth_date: string | null;
  birth_year: number | null;
  color: string | null;
  age: number | null;
  rating: number | null;
  lb: number | null;
  no: number | null;
  draw: number | null;
  gear: string | null;
  pedigree: Pedigree | null;
  curation: 'curated' | 'pending';
  status: 'curated' | 'pending';
  jockey_uid: string | null;
  trainer_uid: string | null;
  race_uid: string | null;
  entries: Entry[];
};

export type Race = {
  uid: string;
  names: Names;
  announce_date: string;
  race_date: string | null;
  race_time: string | null;
  session: string | null;
  grade: string | null;
  grade_cn: string | null;
  venue: string | null;
  venue_country: string | null;
  course: string | null;
  distance: string | null;
  surface: string | null;
  entries: Entry[];
  source: {
    article: { url: string; archive_url: string };
    attachment: { url: string; archive_url: string };
  };
};

export type Person = {
  uid: string;
  role: 'jockey' | 'trainer';
  role_cn: string;
  nationality: string;
  nationality_cn: string;
  names: Names;
  horses: string[];
};

type Shard<T> = { _meta: { schema: string; updated: string; count: number; kind: string }; items: T[] };

/** 分片在构建期一次性 import；运行期不 fetch 实体数据。 */
function collect<T>(mods: Record<string, unknown>, kind: string): T[] {
  return Object.keys(mods)
    .filter((p) => p.includes(`/${kind}/shard-`))
    .sort()
    .flatMap((p) => (mods[p] as Shard<T>).items);
}

const horseShards = import.meta.glob<Shard<Horse>>('../../public/data/horses/shard-*.json', { eager: true });
const raceShards = import.meta.glob<Shard<Race>>('../../public/data/races/shard-*.json', { eager: true });
const personShards = import.meta.glob<Shard<Person>>('../../public/data/persons/shard-*.json', { eager: true });

export const manifest = manifestRaw;
export const horses = collect<Horse>(horseShards, 'horses');
export const races = collect<Race>(raceShards, 'races');
export const persons = collect<Person>(personShards, 'persons');

export const regions = manifestRaw.curation.regions;
export const indexTotal = manifestRaw.index_total;
export const curated = manifestRaw.curation.curated;
export const pendingCount = manifestRaw.curation.pending;

const horseMap = new Map(horses.map((h) => [h.uid, h]));
const raceMap = new Map(races.map((r) => [r.uid, r]));
const personMap = new Map(persons.map((p) => [p.uid, p]));

export const horseByUid = (uid: string | null | undefined) => (uid ? horseMap.get(uid) : undefined);
export const raceByUid = (uid: string | null | undefined) => (uid ? raceMap.get(uid) : undefined);
export const personByUid = (uid: string | null | undefined) => (uid ? personMap.get(uid) : undefined);

export const jockeys = persons.filter((p) => p.role === 'jockey');
export const trainers = persons.filter((p) => p.role === 'trainer');

const BASE = import.meta.env.BASE_URL;

/** 站内链接：统一补 base，避免子路径部署 404。 */
export function href(path = ''): string {
  const base = BASE.endsWith('/') ? BASE : `${BASE}/`;
  return base + path.replace(/^\/+/, '');
}

/** 数据产物的运行期地址（索引页的搜索索引才用 fetch）。 */
export function dataUrl(path: string): string {
  return href(`data/${path.replace(/^\/+/, '')}`);
}

/** 日期显示：2026-11-20 → 2026.11.20 */
export function fmtDate(date: string | null): string {
  return date ? date.replaceAll('-', '.') : '—';
}

export type ArchiveFilter = 'horse' | 'race' | 'jockey' | 'trainer';

export type ArchiveRow = {
  kind: 'horse' | 'race' | 'person';
  /** 筛选用：人物按骑手 / 练马师拆开 */
  filter: ArchiveFilter;
  kind_cn: string;
  uid: string;
  names: Names;
  /** 行尾状态：已校订 / 待维护 / 待接入 */
  status: string;
  status_cls: string;
  /** 国籍 / 所在地，唯一的副信息列 */
  country: string;
  /** 国家代码（JPN / ARG…）；赛事拿不到，留空。用于「日本 JPN」双写与按国籍筛选 */
  cc: string;
  path: string;
};

/** 名称档案的平铺数据：五条队列加权轮转，顺序即翻页顺序。 */
export function archiveRows(): ArchiveRow[] {
  const horseRow = (h: Horse): ArchiveRow => ({
    kind: 'horse',
    filter: 'horse',
    kind_cn: '马匹',
    uid: h.uid,
    names: h.names,
    status: h.curation === 'curated' ? '已校订' : '待维护',
    status_cls: h.curation === 'curated' ? 'lv-l1' : 'lv-l3',
    country: h.country,
    cc: h.origin || '',
    path: `horse/${h.uid}/`,
  });

  const raceRow = (r: Race): ArchiveRow => ({
    kind: 'race',
    filter: 'race',
    kind_cn: '赛事',
    uid: r.uid,
    names: r.names,
    status: '公告直取',
    status_cls: 'lv-l2',
    country: r.venue_country ?? '—',
    cc: '',
    path: `race/${r.uid}/`,
  });

  const personRow = (p: Person): ArchiveRow => ({
    kind: 'person',
    filter: p.role,
    kind_cn: p.role_cn,
    uid: p.uid,
    names: p.names,
    status: '公告直取',
    status_cls: 'lv-l2',
    country: p.nationality_cn || '—',
    cc: p.nationality || '',
    path: `person/${p.uid}/`,
  });

  return paginate([
    horses.filter((h) => h.curation === 'curated').map(horseRow),
    horses.filter((h) => h.curation !== 'curated').map(horseRow),
    races.map(raceRow),
    persons.filter((p) => p.role === 'jockey').map(personRow),
    persons.filter((p) => p.role !== 'jockey').map(personRow),
  ]);
}

/** 一页多少行。翻页与首屏共用，改这里等于改所有分页。 */
export const PAGE_SIZE = 200;

/**
 * 五条队列加权轮转（curated 马 3 / 其他马 2 / 赛事 1 / 骑手 1 / 练马师 1）。
 *
 * 为什么是轮转而不是直接拼接：拼接时首屏 200 行全是马，赛事与人物要翻 15 页才出现；
 * 纯 1:1 轮转又把维护队列（日本马）稀释成每页 40 条。加权轮转让首屏以马为主、
 * 其余类型同页可见，且**顺序完全确定**——SSR 首屏就是它前 200 条，客户端第 N 页是同一数组的切片。
 *
 * 等价实现：pipeline/s5_export.py 的 WEIGHTS，改一处必须同步另一处。
 */
const WEIGHTS = [3, 2, 1, 1, 1];

export function paginate(queues: ArchiveRow[][]): ArchiveRow[] {
  const out: ArchiveRow[] = [];
  const cursor = queues.map(() => 0);
  let more = true;
  while (more) {
    more = false;
    queues.forEach((q, i) => {
      const take = q.slice(cursor[i], cursor[i] + (WEIGHTS[i] ?? 1));
      cursor[i] += take.length;
      out.push(...take);
      if (cursor[i] < q.length) more = true;
    });
  }
  return out;
}

export const archive = archiveRows();

/** 国籍下拉：按马匹数量降序，只列确有马匹的代码。 */
export function nationOptions(): { code: string; cn: string; count: number }[] {
  const m = new Map<string, { code: string; cn: string; count: number }>();
  for (const h of horses) {
    const code = h.origin;
    if (!code) continue;
    const cur = m.get(code) ?? { code, cn: h.country, count: 0 };
    cur.count += 1;
    m.set(code, cur);
  }
  return [...m.values()].sort((a, b) => b.count - a.count);
}

/** 四个筛选组的计数，页面与筛选按钮共用一份 */
export function countBy(filter: ArchiveFilter): number {
  return archive.filter((r) => r.filter === filter).length;
}

/** 名称栏的展示标签，全站统一。简体是派生列，简称是人工简写，两者都不单独成级。 */
export const NAME_COLS = [
  { key: 'original' as const, label: '原文', note: '所属语区原始书写' },
  { key: 'en' as const, label: '英文', note: '拉丁拼写' },
  { key: 'hk' as const, label: '港译', note: '香港赛马会' },
  { key: 'sc' as const, label: '简体', note: '港译逐字转简体' },
  { key: 'own' as const, label: '自译', note: '本站或社区通译' },
  { key: 'alias' as const, label: '简称', note: '稿中简写，人工维护' },
];

/**
 * 名称栏 → Names 组件的列定义。
 * 两处「同形转灰」：英文与原文相同、简体与港译相同（只是字形没变，不算新信息）。
 */
export function nameCols(n: Names) {
  const enDup = !!n.original && n.original === n.en;
  const scDup = !!n.hk && n.hk === n.sc;
  return [
    { lbl: '原文', v: n.original || '—', empty: !n.original },
    { lbl: '英文', v: n.en || '—', empty: !n.en || enDup },
    { lbl: '港译', v: n.hk || '—', big: true, empty: !n.hk },
    { lbl: '简体', v: n.sc || '—', empty: !n.sc || scDup },
    { lbl: '自译', v: n.own || '—', empty: !n.own },
  ];
}

/** 主名：优先港译，其次原文，再次英文 */
export function primaryName(n: Names): string {
  return n.hk || n.original || n.en || '—';
}

/**
 * 简称展示：一条线里最多 max 条，其余折成「+N」。
 * 简称是多对一的人工简写，页面只负责陈列，不做任何截断或生成。
 */
export function aliasText(n: Names, max = 3): string {
  const list = n.alias ?? [];
  if (!list.length) return '';
  const head = list.slice(0, max).map((a) => a.text).join(' · ');
  return list.length > max ? `${head} +${list.length - max}` : head;
}

/** 检索用：把各栏名称与简称拼成一个可搜字符串（简体、简称都进索引） */
export function searchKey(n: Names): string {
  return [n.original, n.en, n.hk, n.sc, n.own, ...(n.alias ?? []).map((a) => a.text)]
    .filter(Boolean)
    .join(' ')
    .toLowerCase();
}

/** 赛事页：这场比赛的名单按马号排（缺号的排最后，不补 0）。 */
export function raceEntries(r: Race) {
  return [...r.entries].sort((a, b) => (a.no ?? 999) - (b.no ?? 999));
}

/** 人物页：这个人在哪些比赛里出现（一赛事一行，日期倒序） */
export function personRaces(p: Person) {
  const seen = new Set<string>();
  const out: { race: Race; horses: Horse[] }[] = [];
  for (const uid of p.horses) {
    const h = horseByUid(uid);
    if (!h) continue;
    for (const e of h.entries) {
      const race = raceByUid(e.race_uid);
      if (!race || seen.has(race.uid)) continue;
      const inRace = race.entries
        .filter((x) => x.jockey_uid === p.uid || x.trainer_uid === p.uid)
        .map((x) => horseByUid(x.horse_uid))
        .filter(Boolean) as Horse[];
      if (!inRace.length) continue;
      seen.add(race.uid);
      out.push({ race, horses: inRace });
    }
  }
  return out.sort((a, b) => (b.race.race_date || '').localeCompare(a.race.race_date || ''));
}
