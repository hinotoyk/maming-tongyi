#!/usr/bin/env python3
"""人物身份归一：判断公告里两个写法是不是同一个人（实体层，不是字符层）。

两层必须分开，混在一起就会想用一条规则解决两个问题：

  字符层（common.fold_name / slug）
    `O’Brien` 与 `O'Brien` 是同一个字符串的两种码位，折叠后 slug 相同 → 本来就是同一个 uid。
    这一层全自动，不需要判据，也不需要人工。

  实体层（本模块）
    `A P O'Brien` 与 `Aidan O'Brien` 是两个字符串、同一个爱尔兰练马师。
    字符串不同，所以只能靠判据 + 人工确认，且必须留证据、可回退。

判据（按强度）：
  · 港译 hk 相同        —— HKJC 人工分配的中文名，是最强的**分桶键**，但不是判定：
                            父子可能共用（`伍活` 同时是 Benny 与 Ronald Woodworth）。
  · 拉丁姓氏相同        —— 必要条件，姓氏不同直接判为两个人。
  · 名字首字母兼容      —— `A P` ↔ `Aidan`、`D` ↔ `Donnacha` 命中；`A` ↔ `Joseph` 不命中。
  · 全名形态            —— 两侧全是缩写（`J` vs `J`）时无法判定，进人工。
  · 同场共现（骑手）    —— 硬否决：一个骑手一场只能骑一匹。练马师一练多马很常见，不能当否决。
  · 后缀 Jnr / Jr       —— 父子风险，出现即进人工，不自动并。

本模块只做纯函数，不读写文件；落库与写复核队列在 s3b_identity.py。
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common as C

# 世代后缀：出现即说明可能是父子两代，自动合并风险高
SUFFIXES = {"jnr", "jr", "snr", "sr", "ii", "iii", "iv"}

# 称谓：不参与姓名结构。Sir / Dame / Bt 是同一人的头衔，剥掉不影响身份；
# Mr / Mrs / Mme 可能指配偶（「Mrs John Harrington」与「John Harrington」是两个人），
# 剥离后仍要进人工，不能自动并。
TITLES_SAFE = {"sir", "dame", "dr", "lord", "lady", "bt", "bart", "prof"}
TITLES_SPOUSE = {"mr", "mrs", "ms", "mme", "mlle", "miss", "mx"}
# 介词 / 部落名：属于书写习惯，不参与「名」的比较（Ahmad bin Harmash ↔ Ahmad Harmash）
PARTICLES = {"bin", "ibn", "binti", "al", "el", "de", "da", "di", "del", "della",
             "van", "von", "der", "den", "la", "le", "du", "des", "dos", "das", "ten", "ter"}


# ── 拉丁名结构 ──────────────────────────────────────────────────────────
def _tokens(name: str | None) -> list[str]:
    t = C.fold_name(name).replace(".", " ")
    toks = [re.sub(r"[^A-Za-z'\-\u00c0-\u024f]", "", x) for x in re.split(r"\s+", t) if x]
    return [x for x in toks if x]


def latin_parts(name: str | None) -> tuple[str, tuple[str, ...]] | None:
    """'A P O’Brien' → ('obrien', ('a', 'p'))；'Sir Mark Prescott Bt' → ('prescott', ('mark',))。

    姓氏 = 去掉世代后缀、头衔与尾部单字母缩写后的最后一个词；其余为名（介词不算名）。
    取不到两个词时按单名处理（姓氏即全名、名字为空），不猜中间名。
    """
    toks = _tokens(name)
    if not toks:
        return None
    while len(toks) > 1 and toks[0].lower() in TITLES_SAFE | TITLES_SPOUSE:
        toks.pop(0)
    while len(toks) > 2 and (toks[-1].lower() in SUFFIXES | TITLES_SAFE or len(toks[-1]) == 1):
        toks.pop()
    sur = toks[-1].lower().replace("'", "")
    given = tuple(x.lower() for x in toks[:-1] if x.lower() not in PARTICLES)
    return sur, given


def has_generation_suffix(name: str | None) -> bool:
    return any(t.strip(",").lower() in SUFFIXES for t in _tokens(name))


def has_spouse_title(name: str | None) -> bool:
    return any(t.strip(",").lower() in TITLES_SPOUSE for t in C.fold_name(name).split())


def is_initials_only(name: str | None) -> bool:
    """名字部分全是单字母（`J P`）→ 无法与任何全名区分，只能人工判。"""
    p = latin_parts(name)
    if not p:
        return True
    _, given = p
    return all(len(x) == 1 for x in given)


def _first_ok(a: str, b: str) -> bool:
    if a == b:
        return True
    if len(a) == 1 and b.startswith(a):
        return True
    if len(b) == 1 and a.startswith(b):
        return True
    return False


def compatible(a: str | None, b: str | None) -> bool:
    """两个拉丁名是否指向同一人：姓氏相同 + 名字首词兼容 + 其余全名部分一致。

    中间名首字母（`A P` 的 P、`Jose A da Silva` 的 A）不参与比较——短写法本来就会省略。
    两侧剩下的**非缩写**词必须完全一致，否则宁可不判（进人工），也不合并。
    """
    pa, pb = latin_parts(a), latin_parts(b)
    if not pa or not pb:
        return False
    (sa, ga), (sb, gb) = pa, pb
    if sa != sb:
        return False
    if not ga or not gb:                       # 单名 vs 带名字：不是同一书写形态
        return ga == gb
    if not _first_ok(ga[0], gb[0]):
        return False
    ra = [x for x in ga[1:] if len(x) > 1]
    rb = [x for x in gb[1:] if len(x) > 1]
    return ra == rb


# ── 实体 ────────────────────────────────────────────────────────────────
@dataclass
class Person:
    uid: str
    role: str
    names: dict[str, int] = field(default_factory=dict)   # en 写法 → 出现场次
    hks: dict[str, int] = field(default_factory=dict)     # 港译 → 出现场次
    races: set[str] = field(default_factory=set)
    first_date: str = "9999-99-99"
    last_date: str = "0000-00-00"
    sample: list[str] = field(default_factory=list)       # 几场赛事名，供人工对照

    @property
    def name(self) -> str:
        return max(self.names, key=lambda k: (self.names[k], len(k))) if self.names else ""

    @property
    def hk(self) -> str:
        return max(self.hks, key=lambda k: (self.hks[k], len(k))) if self.hks else ""

    def add(self, en: str, hk: str, race_uid: str, date: str, sample: str) -> None:
        if en:
            self.names[en] = self.names.get(en, 0) + 1
        if hk:
            self.hks[hk] = self.hks.get(hk, 0) + 1
        self.races.add(race_uid)
        self.first_date = min(self.first_date, date)
        self.last_date = max(self.last_date, date)
        if sample and len(self.sample) < 3 and sample not in self.sample:
            self.sample.append(sample)


def block_key(role: str, hk: str) -> str:
    """分桶键：角色 + 港译（繁简归一）。港译缺失时退化成拉丁名折叠串。"""
    t = C.fold_name(hk)
    if not t:
        return ""
    try:
        t = C.simplified(t)
    except Exception:
        pass
    return f"{role}|{C.norm_key(t)}"


# ── 判定 ────────────────────────────────────────────────────────────────
@dataclass
class Verdict:
    verdict: str            # auto / review / split
    reason: str
    canonical: str
    members: list[str]
    detail: str = ""


def pick_canonical(members: list[Person]) -> str:
    """主名选「信息最全」的那条：全名 > 无世代后缀 > 场次多 > 出现早 > uid 字典序。

    全部是确定性规则，重跑结果一致——否则同一批数据每次重建 uid 都会漂移。
    """
    def score(p: Person) -> tuple:
        return (
            not is_initials_only(p.name),          # 有全名优先
            not has_generation_suffix(p.name),     # 不带 Jnr 优先
            len(p.races),                          # 场次多优先
            -int(p.first_date.replace("-", "") or 99999999),
            p.uid,
        )
    return max(members, key=score).uid


def judge(members: list[Person], splits: set[tuple[str, str]] | None = None) -> Verdict:
    """一组同港译同角色的人 → auto / review / split。"""
    splits = splits or set()
    uids = sorted(p.uid for p in members)
    canonical = pick_canonical(members)
    if len(members) < 2:
        return Verdict("split", "单条，无可并", canonical, uids)

    for i in range(len(members)):
        for j in range(i + 1, len(members)):
            pair = tuple(sorted((members[i].uid, members[j].uid)))
            if pair in splits:
                return Verdict("split", "人工已判定为不同人", canonical, uids)

    bad = [(a.uid, b.uid) for i, a in enumerate(members) for b in members[i + 1:]
           if not compatible(a.name, b.name)]
    if bad:
        return Verdict("review", "拉丁名结构不一致（可能父子 / 同名不同人 / 拼写差异）",
                       canonical, uids, f"{len(bad)} 对不兼容")

    if any(has_generation_suffix(p.name) for p in members) and \
            len({has_generation_suffix(p.name) for p in members}) > 1:
        return Verdict("review", "一侧带 Jnr / Jr 后缀，父子风险", canonical, uids)

    if len({has_spouse_title(p.name) for p in members}) > 1:
        return Verdict("review", "一侧带 Mr / Mrs / Mme 称谓，可能是配偶", canonical, uids)

    if all(is_initials_only(p.name) for p in members):
        return Verdict("review", "两侧都是缩写，无法判定", canonical, uids)

    return Verdict("auto", "同港译 + 同角色 + 姓氏与名字形态一致", canonical, uids)


def group_persons(persons: dict[str, Person]) -> dict[str, list[Person]]:
    """按 (角色, 港译) 分桶；没有港译的不参与自动合并。"""
    groups: dict[str, list[Person]] = {}
    for p in persons.values():
        key = block_key(p.role, p.hk)
        if not key:
            continue
        groups.setdefault(key, []).append(p)
    return {k: sorted(v, key=lambda p: p.uid) for k, v in groups.items() if len(v) > 1}
