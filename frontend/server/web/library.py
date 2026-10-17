"""资料库（data/characters.db，永久保存）：角色库、画师库与服装库。

每个库 = 资料表 + 名称映射表。联网检索结果自动存入；手动改过的字段记为「已编辑」，自动检索永远不会覆盖。
角色的造型变体（Danbooru 单独建的角色标签，如 aris_(maid)_(blue_archive)）也是普通角色行，parent_tag 指向本体；
本体行的 variants 缓存变体列表（见 web/variant_cache.py）。
服装库存 Danbooru 单独建的服装标签（普通标签，不挂在角色下，如 mischievous_straight_uniform_(blue_archive)），见 web/outfits.py。
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import unicodedata

from .. import config
from .danbooru import _VARIANT2

DB_PATH = config.DATA_DIR / "characters.db"     # 沿用最初的文件名，已有角色数据保持不变
_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS characters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tag TEXT NOT NULL UNIQUE,                -- Danbooru 正式角色标签（手动新增的原创角色可自定）
    name TEXT NOT NULL,                      -- 显示名称
    series_tag TEXT NOT NULL DEFAULT '',
    post_count INTEGER NOT NULL DEFAULT 0,
    other_names TEXT NOT NULL DEFAULT '[]',
    wiki TEXT NOT NULL DEFAULT '',
    appearance TEXT NOT NULL DEFAULT '[]',   -- [{tag, freq}]，freq 为 null 表示手动添加
    notes TEXT NOT NULL DEFAULT '',          -- 补充说明，原样发给 LLM
    source TEXT NOT NULL DEFAULT 'danbooru', -- danbooru / manual
    edited TEXT NOT NULL DEFAULT '[]',       -- 手动改过的字段
    fetched_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS names (
    norm TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    character_id INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_names_char ON names(character_id);

CREATE TABLE IF NOT EXISTS artists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tag TEXT NOT NULL UNIQUE,                -- Danbooru 正式画师标签
    name TEXT NOT NULL,
    post_count INTEGER NOT NULL DEFAULT 0,
    other_names TEXT NOT NULL DEFAULT '[]',
    notes TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'danbooru',
    edited TEXT NOT NULL DEFAULT '[]',
    fetched_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS artist_names (
    norm TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    artist_id INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_artist_names ON artist_names(artist_id);

CREATE TABLE IF NOT EXISTS outfits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tag TEXT NOT NULL UNIQUE,                -- Danbooru 服装标签（普通标签，如 mischievous_straight_uniform_(blue_archive)）
    name TEXT NOT NULL,
    series_tag TEXT NOT NULL DEFAULT '',
    post_count INTEGER NOT NULL DEFAULT 0,
    other_names TEXT NOT NULL DEFAULT '[]',
    wiki TEXT NOT NULL DEFAULT '',
    appearance TEXT NOT NULL DEFAULT '[]',   -- 常见标签 [{tag, freq}]
    members TEXT NOT NULL DEFAULT '[]',      -- 常穿的角色 [{tag, freq}]，freq 为 null 表示来自 wiki
    notes TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'danbooru',
    edited TEXT NOT NULL DEFAULT '[]',
    fetched_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS outfit_names (
    norm TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    outfit_id INTEGER NOT NULL REFERENCES outfits(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_outfit_names ON outfit_names(outfit_id);
-- 作品服装目录（缓存，不是服装库）：某个作品在 Danbooru 上的服装标签列表
-- items = [{tag, post_count, other_names, wiki, members, generic, appearance?, related_members?, profiled_at?}]
CREATE TABLE IF NOT EXISTS outfit_series (
    series_tag TEXT PRIMARY KEY,
    items TEXT NOT NULL DEFAULT '[]',
    fetched_at REAL
);
-- 作品的中文名与常用简称（服装库「用 AI 起中文名」时一起起），绘图页服装检索栏按作品搜索用
CREATE TABLE IF NOT EXISTS series_names (
    series_tag TEXT PRIMARY KEY,
    names TEXT NOT NULL DEFAULT '[]',
    updated_at REAL
);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


_VARIANT_COLS = (("parent_tag", "TEXT NOT NULL DEFAULT ''"),   # 变体行：本体标签；本体为空
                 ("variants", "TEXT NOT NULL DEFAULT '[]'"),     # 本体行：变体列表 [{tag, post_count, removed?}]
                 ("variants_at", "REAL"))                         # 变体列表获取时间，NULL = 还没取过


def norm(name: str) -> str:
    """名称映射的比较键：全角 / 半角统一（Ｋ子 = K子）、不分大小写、去掉空格与常见分隔符。"""
    return re.sub(r"[\s_\-·・.@]+", "", unicodedata.normalize("NFKC", name or "").casefold())


def _fz(name: str) -> str:
    """模糊比对用：在 norm 的基础上再去掉括号。"""
    return re.sub(r"[()（）\[\]【】「」『』]", "", norm(name))


def _covers(q: str, k: str) -> bool:
    """q 的每个字（按次数）都出现在 k 里。"""
    pool = list(k)
    for ch in q:
        if ch not in pool:
            return False
        pool.remove(ch)
    return True


def _migrate(c: sqlite3.Connection) -> None:
    """旧库补列；库里已有的 本体_(变体)_(作品) 挂到本体下（不联网）；名称映射按当前的 norm 重算（只改有变化的）。"""
    cols = {r[1] for r in c.execute("PRAGMA table_info(characters)")}
    for col, ddl in _VARIANT_COLS:
        if col not in cols:
            c.execute(f"ALTER TABLE characters ADD COLUMN {col} {ddl}")
    c.execute("CREATE INDEX IF NOT EXISTS idx_char_parent ON characters(parent_tag)")
    tags = {r[0] for r in c.execute("SELECT tag FROM characters")}
    for (t,) in c.execute("SELECT tag FROM characters WHERE parent_tag = ''").fetchall():
        m = _VARIANT2.match(t)
        if m and not t.endswith("_(cosplay)") and f"{m.group(1)}_{m.group(2)}" in tags:
            c.execute("UPDATE characters SET parent_tag = ? WHERE tag = ?", (f"{m.group(1)}_{m.group(2)}", t))
    for table in ("names", "artist_names", "outfit_names"):
        keys = {r[0] for r in c.execute(f"SELECT norm FROM {table}")}
        for old, name in c.execute(f"SELECT norm, name FROM {table}").fetchall():
            new = norm(name)
            if new == old:
                continue
            if new in keys or not new:      # 新写法已有映射（如 Ｋ子 与 K子）：留先有的那条
                c.execute(f"DELETE FROM {table} WHERE norm = ?", (old,))
            else:
                c.execute(f"UPDATE {table} SET norm = ? WHERE norm = ?", (new, old))
                keys.add(new)
            keys.discard(old)


with _conn() as _c:
    _c.executescript(_SCHEMA)
    _migrate(_c)


class Library:
    def __init__(self, table: str, names_table: str, fk: str, auto: tuple[str, ...], editable: tuple[str, ...],
                 search_cols: tuple[str, ...], has_variants: bool = False, init: tuple[str, ...] = ()):
        self.table, self.names_table, self.fk = table, names_table, fk
        self.auto = auto            # 从 Danbooru 获取、未被手动改过时会更新的字段
        self.init = init            # 只在新建时从 Danbooru 资料里取、以后不自动更新的字段
        self.editable = editable    # 页面上可编辑的字段
        self.search_cols = search_cols
        self.has_variants = has_variants   # 角色库：造型变体挂在本体下

    # ── 内部 ──
    def _row(self, c: sqlite3.Connection, r: sqlite3.Row, family: bool = False) -> dict:
        d = dict(r)
        for k in ("other_names", "appearance", "edited", "variants", "members"):
            if k in d:
                d[k] = json.loads(d[k])
        d["names"] = [x["name"] for x in c.execute(
            f"SELECT name FROM {self.names_table} WHERE {self.fk} = ? ORDER BY name", (d["id"],))]
        if family and self.has_variants:
            p = c.execute(f"SELECT id, name, tag FROM {self.table} WHERE tag = ?", (d["parent_tag"],)).fetchone() \
                if d["parent_tag"] else None
            d["parent"] = dict(p) if p else ({"id": None, "name": "", "tag": d["parent_tag"]} if d["parent_tag"] else None)
            d["variant_rows"] = [dict(x) for x in c.execute(
                f"SELECT id, name, tag, post_count FROM {self.table} WHERE parent_tag = ? ORDER BY post_count DESC",
                (d["tag"],))]
        return d

    def _one(self, c: sqlite3.Connection, rid: int) -> dict:
        return self._row(c, c.execute(f"SELECT * FROM {self.table} WHERE id = ?", (rid,)).fetchone(), True)

    def _claim_names(self, c: sqlite3.Connection, rid: int, tag: str, names) -> None:
        """本体自己的名字被它的变体先占了（变体先存入）时，归还给本体。"""
        for n in names:
            k = norm(n)
            if k:
                c.execute(f"UPDATE {self.names_table} SET {self.fk} = ? WHERE norm = ? AND {self.fk} IN "
                          f"(SELECT id FROM {self.table} WHERE parent_tag = ?)", (rid, k, tag))

    def _add_names(self, c: sqlite3.Connection, rid: int, names, replace: bool = False) -> None:
        verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
        for n in names:
            n = (n or "").strip().lstrip("@")
            k = norm(n)
            if k:
                c.execute(f"{verb} INTO {self.names_table} (norm, name, {self.fk}) VALUES (?,?,?)", (k, n, rid))

    @staticmethod
    def _dump(k: str, v):
        return json.dumps(v, ensure_ascii=False) if k in ("appearance", "other_names", "members") else v

    # ── 查询 ──
    def lookup(self, name: str) -> dict | None:
        k = norm(name)
        if not k:
            return None
        with _lock, _conn() as c:
            r = c.execute(f"SELECT t.* FROM {self.names_table} n JOIN {self.table} t ON t.id = n.{self.fk} "
                          f"WHERE n.norm = ?", (k,)).fetchone()
            return self._row(c, r) if r else None

    def get(self, rid: int) -> dict | None:
        with _lock, _conn() as c:
            r = c.execute(f"SELECT * FROM {self.table} WHERE id = ?", (rid,)).fetchone()
            return self._row(c, r, True) if r else None

    def get_by_tag(self, tag: str) -> dict | None:
        with _lock, _conn() as c:
            r = c.execute(f"SELECT * FROM {self.table} WHERE tag = ?", (tag,)).fetchone()
            return self._row(c, r) if r else None

    def tags_present(self, tags) -> set[str]:
        tags = list(tags)
        if not tags:
            return set()
        with _lock, _conn() as c:
            return {r[0] for r in c.execute(f"SELECT tag FROM {self.table} WHERE tag IN ({', '.join('?' * len(tags))})",
                                            tags)}

    def fuzzy(self, query: str, accept=None, limit: int = 20) -> list[dict]:
        """名称映射的模糊比对（本地，不联网），返回 [{row, matched, score}]，好的在前。

        - 中日文：输入的每个字都出现在某个名字里（「睡衣优香」→「早濑优香(睡衣)」、「优香」→「早濑优香」），score > 1；
        - 英文：输入是某个名字的一部分（「aris maid」→「aris (maid) (blue archive)」），score > 1；
        - 反过来某个名字包含在输入里（「K子老师」里有「K子」），score < 1，只在前两种都没有时才算。
        accept(row)：额外筛选（如核对作品）。
        """
        q = _fz(query)
        if len(q) < 2:
            return []
        with _lock, _conn() as c:
            best: dict[int, tuple[float, str]] = {}
            for k, name, rid in c.execute(f"SELECT norm, name, {self.fk} FROM {self.names_table}"):
                k = _fz(k)
                if not k:
                    continue
                if (q in k) if q.isascii() else _covers(q, k):
                    s = 1 + len(q) / len(k)
                elif len(k) >= 2 and k in q and len(k) / len(q) >= 0.34:
                    s = len(k) / len(q)
                else:
                    continue
                if s > best.get(rid, (0, ""))[0]:
                    best[rid] = (s, name)
            out = []
            for rid, (s, name) in best.items():
                row = self._row(c, c.execute(f"SELECT * FROM {self.table} WHERE id = ?", (rid,)).fetchone())
                if accept is None or accept(row):
                    out.append({"row": row, "matched": name, "score": s})
        out.sort(key=lambda x: (-x["score"], -(x["row"]["post_count"] or 0)))
        if any(x["score"] > 1 for x in out):
            out = [x for x in out if x["score"] > 1]
        return out[:limit]

    def variants_of(self, tag: str) -> list[dict]:
        """本体下已存入的变体（完整资料，图片多的在前）。"""
        if not self.has_variants:
            return []
        with _lock, _conn() as c:
            return [self._row(c, r) for r in c.execute(
                f"SELECT * FROM {self.table} WHERE parent_tag = ? ORDER BY post_count DESC", (tag,))]

    def search(self, q: str, offset: int = 0, limit: int = 50) -> dict:
        q = (q or "").strip()
        with _lock, _conn() as c:
            if q:
                like, under, nk = f"%{q}%", f"%{q.replace(' ', '_')}%", f"%{norm(q)}%"
                conds = ["t.name LIKE ?"] + [f"t.{col} LIKE ?" for col in self.search_cols]
                where = (f"WHERE {' OR '.join(conds)} OR t.id IN "
                         f"(SELECT {self.fk} FROM {self.names_table} WHERE norm LIKE ?)")
                args = (like, *([under] * len(self.search_cols)), nk)
            else:
                where, args = "", ()
            if self.has_variants:
                return self._search_grouped(c, where, args, offset, limit)
            total = c.execute(f"SELECT COUNT(*) FROM {self.table} t {where}", args).fetchone()[0]
            rows = c.execute(f"SELECT t.* FROM {self.table} t {where} ORDER BY t.updated_at DESC LIMIT ? OFFSET ?",
                             (*args, limit, offset)).fetchall()
            return {"total": total, "items": [self._row(c, r) for r in rows]}

    def _search_grouped(self, c: sqlite3.Connection, where: str, args: tuple, offset: int, limit: int) -> dict:
        """角色库列表按「本体 + 它的变体」成组：命中本体或任一变体都显示整组；total 为组数。
        有搜索词时，命中搜索的条目标 hit（页面据此展开 / 高亮含命中变体的组）。"""
        tags = {r[0] for r in c.execute(f"SELECT tag FROM {self.table}")}
        fams: dict[str, float] = {}
        hits: set[str] = set()
        for r in c.execute(f"SELECT t.tag, t.parent_tag, t.updated_at FROM {self.table} t {where}", args):
            k = r["parent_tag"] if r["parent_tag"] in tags else r["tag"]
            fams[k] = max(fams.get(k, 0), r["updated_at"] or 0)
            hits.add(r["tag"])
        keys = sorted(fams, key=lambda k: -fams[k])
        items = []
        for k in keys[offset:offset + limit]:
            items.append(self._row(c, c.execute(f"SELECT * FROM {self.table} WHERE tag = ?", (k,)).fetchone()))
            items += [self._row(c, v) for v in c.execute(
                f"SELECT * FROM {self.table} WHERE parent_tag = ? ORDER BY post_count DESC", (k,))]
        for d in items:
            d["hit"] = bool(where) and d["tag"] in hits
        n_var = c.execute(f"SELECT COUNT(*) FROM {self.table} WHERE parent_tag IN "
                          f"({', '.join('?' * len(keys)) or 'NULL'})", keys).fetchone()[0]
        return {"total": len(keys), "variant_total": n_var, "items": items}

    # ── 写入 ──
    def save_from_danbooru(self, prof: dict, display_name: str, query_names: list[str], parent_tag: str = "") -> dict:
        """保存检索结果：已存在的只更新未被手动改过的字段；同时记下名称映射。parent_tag：作为该本体的变体保存。"""
        now = time.time()
        tag = prof["tag"]
        auto_names = [*query_names, *prof.get("other_names", []), tag, tag.replace("_", " ")]
        with _lock, _conn() as c:
            r = c.execute(f"SELECT * FROM {self.table} WHERE tag = ?", (tag,)).fetchone()
            if r:
                edited = set(json.loads(r["edited"]))
                sets = {"post_count": prof["post_count"], "other_names": self._dump("other_names", prof["other_names"]),
                        "fetched_at": now, "updated_at": now}
                sets.update({k: self._dump(k, prof[k]) for k in self.auto if k not in edited})
                if parent_tag and self.has_variants and not r["parent_tag"]:
                    sets["parent_tag"] = parent_tag
                c.execute(f"UPDATE {self.table} SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
                          (*sets.values(), r["id"]))
                rid = r["id"]
            else:
                cols = {"tag": tag, "name": display_name or tag.replace("_", " "), "post_count": prof["post_count"],
                        "other_names": self._dump("other_names", prof["other_names"]), "source": "danbooru",
                        "fetched_at": prof.get("fetched_at", now), "updated_at": now,
                        **{k: self._dump(k, prof[k]) for k in (*self.init, *self.auto)}}
                if parent_tag and self.has_variants:
                    cols["parent_tag"] = parent_tag
                rid = c.execute(f"INSERT INTO {self.table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                tuple(cols.values())).lastrowid
            self._add_names(c, rid, auto_names)
            if self.has_variants:
                self._claim_names(c, rid, tag, auto_names)
            return self._one(c, rid)

    # ── 造型变体（只用于角色库） ──
    def set_variants(self, tag: str, variants: list[dict]) -> None:
        """缓存本体的变体列表（保留用户删除过的标记），并把库里已有的这些变体挂到本体下。"""
        with _lock, _conn() as c:
            r = c.execute(f"SELECT id, variants, other_names FROM {self.table} WHERE tag = ?", (tag,)).fetchone()
            if not r:
                return
            removed = {v["tag"] for v in json.loads(r["variants"]) if v.get("removed")}
            lst = [{"tag": v["tag"], "post_count": v["post_count"], **({"removed": True} if v["tag"] in removed else {})}
                   for v in variants]
            c.execute(f"UPDATE {self.table} SET variants = ?, variants_at = ? WHERE id = ?",
                      (json.dumps(lst, ensure_ascii=False), time.time(), r["id"]))
            for v in lst:
                c.execute(f"UPDATE {self.table} SET parent_tag = ? WHERE tag = ? AND parent_tag = ''", (tag, v["tag"]))
            self._claim_names(c, r["id"], tag, [*json.loads(r["other_names"]), tag, tag.replace("_", " ")])

    def set_parent(self, tag: str, parent_tag: str) -> None:
        with _lock, _conn() as c:
            c.execute(f"UPDATE {self.table} SET parent_tag = ? WHERE tag = ? AND parent_tag = ''", (parent_tag, tag))
            p = c.execute(f"SELECT id, other_names FROM {self.table} WHERE tag = ?", (parent_tag,)).fetchone()
            if p:
                self._claim_names(c, p["id"], parent_tag,
                                  [*json.loads(p["other_names"]), parent_tag, parent_tag.replace("_", " ")])

    def add_names(self, rid: int, names: list[str]) -> None:
        with _lock, _conn() as c:
            self._add_names(c, rid, names)

    def _clean(self, k: str, v):
        if k == "appearance":
            out, seen = [], set()
            for x in v or []:
                if isinstance(x, str):
                    x = {"tag": x, "freq": None}
                t = (x.get("tag") or "").strip().replace(" ", "_")
                if t and t not in seen:
                    seen.add(t)
                    out.append({"tag": t, "freq": x.get("freq")})
            return json.dumps(out, ensure_ascii=False)
        if k in ("tag", "series_tag"):
            return (v or "").strip().lstrip("@").replace(" ", "_")
        if k in ("wiki", "notes"):
            return v or ""
        return (v or "").strip()

    def create_manual(self, data: dict) -> dict:
        tag = self._clean("tag", data.get("tag")) or norm(data.get("name") or "")
        if not tag:
            raise ValueError("请至少填写名称或标签")
        now = time.time()
        with _lock, _conn() as c:
            if c.execute(f"SELECT 1 FROM {self.table} WHERE tag = ?", (tag,)).fetchone():
                raise ValueError(f"库中已有标签 {tag}")
            cols = {"tag": tag, "name": self._clean("name", data.get("name")) or tag.replace("_", " "),
                    "source": "manual", "edited": json.dumps(list(self.editable)), "updated_at": now}
            cols.update({k: self._clean(k, data.get(k)) for k in self.editable if k not in ("tag", "name")})
            rid = c.execute(f"INSERT INTO {self.table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                            tuple(cols.values())).lastrowid
            self._add_names(c, rid, [*(data.get("names") or []), cols["name"], tag, tag.replace("_", " ")],
                            replace=True)
            return self._one(c, rid)

    def update(self, rid: int, data: dict) -> dict | None:
        """手动编辑。内容有变化的字段记为「已编辑」。names 给出时整体替换映射。"""
        with _lock, _conn() as c:
            r = c.execute(f"SELECT * FROM {self.table} WHERE id = ?", (rid,)).fetchone()
            if not r:
                return None
            edited = set(json.loads(r["edited"]))
            sets: dict = {}
            for k in self.editable:
                if k not in data:
                    continue
                v = self._clean(k, data[k])
                if k == "tag" and not v:
                    raise ValueError("标准标签不能为空")
                if v != r[k]:
                    sets[k] = v
                    edited.add(k)
            if "tag" in sets and c.execute(f"SELECT 1 FROM {self.table} WHERE tag = ? AND id != ?",
                                           (sets["tag"], rid)).fetchone():
                raise ValueError(f"库中已有标签 {sets['tag']}")
            sets["edited"] = json.dumps(sorted(edited))
            sets["updated_at"] = time.time()
            c.execute(f"UPDATE {self.table} SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?", (*sets.values(), rid))
            if "tag" in sets and self.has_variants:     # 本体改了标签，变体跟着挂过去
                c.execute(f"UPDATE {self.table} SET parent_tag = ? WHERE parent_tag = ?", (sets["tag"], r["tag"]))
            if "names" in data:
                c.execute(f"DELETE FROM {self.names_table} WHERE {self.fk} = ?", (rid,))
                self._add_names(c, rid, data["names"], replace=True)
            return self._one(c, rid)

    def apply_refresh(self, rid: int, prof: dict, keep_edited: bool) -> dict | None:
        """「从 Danbooru 重新获取」：keep_edited 时保留手动改过的字段，否则用新资料覆盖并清除已编辑标记。"""
        with _lock, _conn() as c:
            r = c.execute(f"SELECT * FROM {self.table} WHERE id = ?", (rid,)).fetchone()
            if not r:
                return None
            edited = set(json.loads(r["edited"])) if keep_edited else set()
            now = time.time()
            sets = {"post_count": prof["post_count"], "other_names": self._dump("other_names", prof["other_names"]),
                    "fetched_at": now, "updated_at": now, "source": "danbooru"}
            sets.update({k: self._dump(k, prof[k]) for k in self.auto if k not in edited})
            sets["edited"] = json.dumps(sorted(edited))
            c.execute(f"UPDATE {self.table} SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?", (*sets.values(), rid))
            self._add_names(c, rid, prof.get("other_names", []))
            return self._one(c, rid)

    def delete(self, rid: int) -> bool:
        """角色库：删除本体时连同它的变体；单独删除的变体记下来，后台缓存不再自动取回（「获取」等明确操作仍可加回）。"""
        with _lock, _conn() as c:
            if self.has_variants:
                r = c.execute(f"SELECT tag, parent_tag FROM {self.table} WHERE id = ?", (rid,)).fetchone()
                if r and r["parent_tag"]:
                    p = c.execute(f"SELECT id, variants FROM {self.table} WHERE tag = ?", (r["parent_tag"],)).fetchone()
                    if p:
                        lst = [{**v, "removed": True} if v["tag"] == r["tag"] else v for v in json.loads(p["variants"])]
                        c.execute(f"UPDATE {self.table} SET variants = ? WHERE id = ?",
                                  (json.dumps(lst, ensure_ascii=False), p["id"]))
                elif r:
                    c.execute(f"DELETE FROM {self.table} WHERE parent_tag = ?", (r["tag"],))
            return c.execute(f"DELETE FROM {self.table} WHERE id = ?", (rid,)).rowcount > 0


class OutfitLibrary(Library):
    """服装库：另有按作品缓存的服装目录（outfit_series），见 web/outfits.py。"""

    def of_series(self, series: str) -> list[dict]:
        with _lock, _conn() as c:
            return [self._row(c, r) for r in c.execute(
                f"SELECT * FROM {self.table} WHERE series_tag = ? ORDER BY post_count DESC", (series,))]

    def all_rows(self) -> list[dict]:
        with _lock, _conn() as c:
            return [self._row(c, r) for r in c.execute(f"SELECT * FROM {self.table}")]

    def catalog(self, series: str) -> dict | None:
        with _lock, _conn() as c:
            r = c.execute("SELECT items, fetched_at FROM outfit_series WHERE series_tag = ?", (series,)).fetchone()
            return {"items": json.loads(r["items"]), "fetched_at": r["fetched_at"]} if r else None

    def generic_tags(self) -> set[str]:
        """各作品目录里「去掉作品括号后撞了常见普通标签」的服装（china_dress_(love_live!)）。"""
        with _lock, _conn() as c:
            return {x["tag"] for (items,) in c.execute("SELECT items FROM outfit_series")
                    for x in json.loads(items) if x.get("generic")}

    _PROFILE_KEYS = ("appearance", "related_members", "profiled_at")     # 目录条目在后台补的常见标签与常穿的角色

    def set_catalog(self, series: str, items: list[dict]) -> None:
        """保存作品服装目录（重新取目录时，已补好的常见标签与常穿的角色沿用）。"""
        with _lock, _conn() as c:
            r = c.execute("SELECT items FROM outfit_series WHERE series_tag = ?", (series,)).fetchone()
            old = {x["tag"]: x for x in json.loads(r["items"])} if r else {}
            items = [{**x, **{k: old[x["tag"]][k] for k in self._PROFILE_KEYS if k in old.get(x["tag"], {})}}
                     for x in items]
            c.execute("INSERT OR REPLACE INTO outfit_series (series_tag, items, fetched_at) VALUES (?,?,?)",
                      (series, json.dumps(items, ensure_ascii=False), time.time()))

    def set_name(self, rid: int, name: str) -> None:
        """AI 起的中文名：设为显示名称并记进名称映射（不算手动编辑，原来的名字仍在名称映射里）。"""
        with _lock, _conn() as c:
            r = c.execute(f"SELECT name FROM {self.table} WHERE id = ?", (rid,)).fetchone()
            if r:
                self._add_names(c, rid, [r["name"], name])
                c.execute(f"UPDATE {self.table} SET name = ?, updated_at = ? WHERE id = ?", (name, time.time(), rid))

    def series_names(self) -> dict[str, list[str]]:
        with _lock, _conn() as c:
            return {r["series_tag"]: json.loads(r["names"]) for r in c.execute("SELECT series_tag, names FROM series_names")}

    def set_series_names(self, series: str, names: list[str]) -> None:
        with _lock, _conn() as c:
            c.execute("INSERT OR REPLACE INTO series_names (series_tag, names, updated_at) VALUES (?,?,?)",
                      (series, json.dumps(names, ensure_ascii=False), time.time()))

    def update_catalog_item(self, series: str, tag: str, fields: dict) -> None:
        with _lock, _conn() as c:
            r = c.execute("SELECT items FROM outfit_series WHERE series_tag = ?", (series,)).fetchone()
            if r:
                items = [{**x, **fields} if x["tag"] == tag else x for x in json.loads(r["items"])]
                c.execute("UPDATE outfit_series SET items = ? WHERE series_tag = ?",
                          (json.dumps(items, ensure_ascii=False), series))


CHARACTERS = Library("characters", "names", "character_id", auto=("series_tag", "wiki", "appearance"),
                     editable=("name", "tag", "series_tag", "wiki", "appearance", "notes"),
                     search_cols=("tag", "series_tag"), has_variants=True)
ARTISTS = Library("artists", "artist_names", "artist_id", auto=(),
                  editable=("name", "tag", "notes"), search_cols=("tag",))
# 服装的作品沿用目录 / 标签括号（与目录对得上），不随共现作品变
OUTFITS = OutfitLibrary("outfits", "outfit_names", "outfit_id", auto=("wiki", "appearance", "members"),
                        editable=("name", "tag", "series_tag", "wiki", "appearance", "notes"),
                        search_cols=("tag", "series_tag"), init=("series_tag",))
LIBS = {"characters": CHARACTERS, "artists": ARTISTS, "outfits": OUTFITS}
