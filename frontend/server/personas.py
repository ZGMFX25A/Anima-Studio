"""人设（data/personas.db，永久保存）：原创人设与导入的已有角色，基础外观 + 服装差分（造型）。

预览图是普通生成记录（gallery.db 的 persona_id 列），只在人设页管理；封面复制到 data/personas/<id>/，
删除预览图后封面也不会丢。
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import unicodedata
from pathlib import Path
from typing import IO

from PIL import Image

from . import config, gallery

DB_PATH = config.DATA_DIR / "personas.db"
FILES_DIR = config.DATA_DIR / "personas"
FILES_DIR.mkdir(parents=True, exist_ok=True)
KINDS = ("original", "existing")
THUMB_SIDE = 480
_lock = threading.Lock()
log = logging.getLogger("frontend.personas")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS personas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,                 -- 名称：全库唯一，AI 填写时描述里出现即匹配
    aliases TEXT NOT NULL DEFAULT '[]',        -- 别名：同样参与匹配
    kind TEXT NOT NULL DEFAULT 'original',     -- original 原创 / existing 已有角色
    character_tag TEXT NOT NULL DEFAULT '',    -- 写进 character（标签写法，空格分隔）
    series TEXT NOT NULL DEFAULT '',           -- 写进 series
    count TEXT NOT NULL DEFAULT '1girl',       -- 主体，写进 count
    artist TEXT NOT NULL DEFAULT '',           -- 画师偏好（@ 开头），未指定画师时使用
    groups TEXT NOT NULL DEFAULT '[]',
    base_tags TEXT NOT NULL DEFAULT '',        -- 基础外观标签：只写本体特征
    appearance_desc TEXT NOT NULL DEFAULT '',  -- 外观描述（中文自然语言），发给 LLM
    outfits TEXT NOT NULL DEFAULT '[]',        -- [{id, name, tags, desc, character_tag, default}]
    source_tag TEXT NOT NULL DEFAULT '',       -- 导入来源的 Danbooru 角色标签
    cover TEXT NOT NULL DEFAULT '',            -- 封面版本号（空 = 没有封面）
    cover_gid INTEGER,                         -- 封面来自哪张生成记录（可能已删除）
    created_at REAL, updated_at REAL
);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


with _conn() as _c:
    _c.executescript(_SCHEMA)


class PersonaError(ValueError):
    pass


# ───────────────────────────── 规范化 ─────────────────────────────

def fold(text: str) -> str:
    """比较用：全角 / 半角统一（Ｋ子 = K子）、不分大小写。"""
    return unicodedata.normalize("NFKC", text or "").casefold()


def norm(name: str) -> str:
    return re.sub(r"[\s_\-·・.@]+", "", fold(name))


def split_list(v) -> list[str]:
    if isinstance(v, str):
        v = re.split(r"[,，、\n]", v)
    out, seen = [], set()
    for x in v or []:
        x = str(x).strip()
        if x and x.casefold() not in seen:
            seen.add(x.casefold())
            out.append(x)
    return out


_EMOTICONS = {"o_o", "0_0", "x_x", "u_u", "t_t", "q_q", "d_d"}


def tag_text(tag: str) -> str:
    """Danbooru 标签 → 提示词写法：词间下划线写成空格（^_^、o_o 这类表情标签保持原样）。"""
    tag = (tag or "").strip()
    if tag.casefold() in _EMOTICONS:
        return tag
    return re.sub(r"(?<=[0-9a-z)])_(?=[0-9a-z(])", " ", tag, flags=re.I)


def clean_tags(text, exclude: set[str] | None = None) -> str:
    """逗号分隔的标签：去空白、去重（可排除 exclude 中的标签）。"""
    exclude = exclude or set()
    out, seen = [], set(exclude)
    for t in split_list(text if isinstance(text, (str, list)) else ""):
        k = t.casefold()
        if k not in seen:
            seen.add(k)
            out.append(t)
    return ", ".join(out)


def tag_set(text: str) -> set[str]:
    return {t.casefold() for t in split_list(text)}


def _artist(text: str) -> str:
    return ", ".join(t if t.startswith("@") else "@" + t for t in split_list(text))


def clean_outfits(raw) -> list[dict]:
    out: list[dict] = []
    ids = set()
    for i, o in enumerate(raw or []):
        if not isinstance(o, dict):
            continue
        oid = str(o.get("id") or "").strip() or secrets.token_hex(4)
        while oid in ids:
            oid = secrets.token_hex(4)
        ids.add(oid)
        out.append({"id": oid, "name": str(o.get("name") or "").strip() or f"造型 {i + 1}",
                    "tags": clean_tags(o.get("tags") or ""), "desc": str(o.get("desc") or "").strip(),
                    "character_tag": tag_text(str(o.get("character_tag") or "")), "default": bool(o.get("default"))})
    if out:   # 恰好一个默认造型
        first = next((o for o in out if o["default"]), out[0])
        for o in out:
            o["default"] = o is first
    return out


def _clean(data: dict) -> dict:
    name = str(data.get("name") or "").strip()
    if not name:
        raise PersonaError("请填写名称")
    kind = data.get("kind") if data.get("kind") in KINDS else "original"
    aliases = [a for a in split_list(data.get("aliases")) if norm(a) and norm(a) != norm(name)]
    return {
        "name": name, "aliases": aliases, "kind": kind,
        "character_tag": tag_text(str(data.get("character_tag") or "")),
        "series": tag_text(str(data.get("series") or "")),
        "count": clean_tags(data.get("count") or ""),
        "artist": _artist(data.get("artist") or ""),
        "groups": split_list(data.get("groups")),
        "base_tags": clean_tags(data.get("base_tags") or ""),
        "appearance_desc": str(data.get("appearance_desc") or "").strip(),
        "outfits": clean_outfits(data.get("outfits")),
        "source_tag": str(data.get("source_tag") or "").strip(),
    }


_JSON = ("aliases", "groups", "outfits")


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    for k in _JSON:
        d[k] = json.loads(d[k])
    return d


def _check_names(c: sqlite3.Connection, d: dict, pid: int | None) -> None:
    """名称与别名在全库唯一（按忽略大小写 / 空格 / 符号比较）。"""
    mine = {norm(d["name"]): d["name"], **{norm(a): a for a in d["aliases"]}}
    for r in c.execute("SELECT id, name, aliases FROM personas WHERE id != ?", (pid or -1,)):
        for other in [r["name"], *json.loads(r["aliases"])]:
            k = norm(other)
            if k in mine:
                raise PersonaError(f"「{mine[k]}」已被人设「{r['name']}」用作名称或别名，名称和别名需要全库唯一")


def _taken(exclude: int | None = None) -> dict[str, tuple[int, str, bool]]:
    """全库已用的名称 / 别名：norm → (人设 id, 人设名称, 是否为名称)。"""
    out: dict[str, tuple[int, str, bool]] = {}
    with _lock, _conn() as c:
        for r in c.execute("SELECT id, name, aliases FROM personas WHERE id != ?", (exclude or -1,)):
            for n in json.loads(r["aliases"]):
                out.setdefault(norm(n), (r["id"], r["name"], False))
            out[norm(r["name"])] = (r["id"], r["name"], True)
    return out


def find_conflict(name: str) -> dict | None:
    """名称与哪个人设的名称或别名重复：{id, name, by: name|alias}。"""
    hit = _taken().get(norm(name))
    return {"id": hit[0], "name": hit[1], "by": "name" if hit[2] else "alias"} if hit else None


def unique_name(name: str) -> str:
    """不与任何人设名称 / 别名重复的名称：名称 (2)、名称 (3)……"""
    taken = _taken()
    base = re.sub(r"\s*\(\d+\)$", "", name.strip()) or name.strip()
    i = 2
    while norm(f"{base} ({i})") in taken:
        i += 1
    return f"{base} ({i})"


def drop_taken_aliases(d: dict, exclude: int | None = None) -> list[str]:
    """去掉已被其它人设用作名称 / 别名的别名（exclude = 将被覆盖的人设），返回去掉的别名。"""
    taken = _taken(exclude)
    aliases = split_list(d.get("aliases"))
    d["aliases"] = [a for a in aliases if norm(a) not in taken]
    return [a for a in aliases if norm(a) in taken]


# ───────────────────────────── 增删改查 ─────────────────────────────

def summary(p: dict) -> dict:
    return {k: p[k] for k in ("id", "name", "aliases", "kind", "character_tag", "series", "count", "artist", "groups",
                              "cover", "updated_at")} | {
        "outfits": [{"id": o["id"], "name": o["name"], "default": o["default"]} for o in p["outfits"]]}


def list_all(q: str = "", kind: str = "", group: str = "") -> dict:
    with _lock, _conn() as c:
        rows = [_row(r) for r in c.execute("SELECT * FROM personas ORDER BY updated_at DESC")]
    groups: dict[str, int] = {}
    for p in rows:
        for g in p["groups"]:
            groups[g] = groups.get(g, 0) + 1
    qn = norm(q)
    items = []
    for p in rows:
        if kind in KINDS and p["kind"] != kind:
            continue
        if group and group not in p["groups"]:
            continue
        if qn:
            hay = [p["name"], *p["aliases"], p["character_tag"], p["series"], *p["groups"]]
            if not any(qn in norm(h) for h in hay):
                continue
        items.append(summary(p))
    return {"total": len(rows), "items": items,
            "groups": [{"name": k, "count": v} for k, v in sorted(groups.items(), key=lambda x: (-x[1], x[0]))]}


def all_full() -> list[dict]:
    with _lock, _conn() as c:
        return [_row(r) for r in c.execute("SELECT * FROM personas ORDER BY id")]


def get(pid: int) -> dict | None:
    with _lock, _conn() as c:
        r = c.execute("SELECT * FROM personas WHERE id = ?", (pid,)).fetchone()
    if not r:
        return None
    d = _row(r)
    d["image_count"] = len(gallery.persona_image_ids(pid))
    return d


def create(data: dict) -> dict:
    d = _clean(data)
    now = time.time()
    with _lock, _conn() as c:
        _check_names(c, d, None)
        cols = {**{k: json.dumps(v, ensure_ascii=False) if k in _JSON else v for k, v in d.items()},
                "created_at": now, "updated_at": now}
        pid = c.execute(f"INSERT INTO personas ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                        tuple(cols.values())).lastrowid
    return get(pid)


def update(pid: int, data: dict) -> dict | None:
    d = _clean(data)
    with _lock, _conn() as c:
        if not c.execute("SELECT 1 FROM personas WHERE id = ?", (pid,)).fetchone():
            return None
        _check_names(c, d, pid)
        sets = {**{k: json.dumps(v, ensure_ascii=False) if k in _JSON else v for k, v in d.items()},
                "updated_at": time.time()}
        c.execute(f"UPDATE personas SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?", (*sets.values(), pid))
    return get(pid)


def delete(pid: int) -> int | None:
    """删除人设、封面与全部预览图（预览图不在历史页，留着就再也看不到了）。返回删除的预览图数量。"""
    with _lock, _conn() as c:
        if c.execute("DELETE FROM personas WHERE id = ?", (pid,)).rowcount == 0:
            return None
    ids = gallery.persona_image_ids(pid)
    for gid in ids:
        gallery.delete(gid, set())
    shutil.rmtree(FILES_DIR / str(pid), ignore_errors=True)
    return len(ids)


# ───────────────────────────── 封面 ─────────────────────────────

def cover_path(pid: int, thumb: bool = False) -> Path:
    """封面副本 cover.png（原图无损、不缩小）；旧版存的是 cover.webp（长边 1280、有损），仍可读取。"""
    d = FILES_DIR / str(pid)
    if thumb:
        return d / "cover_thumb.webp"
    png = d / "cover.png"
    return png if png.exists() or not (d / "cover.webp").exists() else d / "cover.webp"


def _write_cover_png(d: Path, src: Path | IO[bytes]) -> Image.Image:
    """原图无损存成 cover.png（先写临时文件再替换，读取方不会拿到写了一半的文件），去掉旧版的 cover.webp。
    只存像素，不带原图的文本块（生成参数、人设卡数据）。"""
    img = Image.open(src)
    img.load()
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "A" in img.getbands() or "transparency" in img.info else "RGB")
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "cover.png.tmp"
    img.save(tmp, "PNG")
    os.replace(tmp, d / "cover.png")
    (d / "cover.webp").unlink(missing_ok=True)
    return img


def _save_cover(pid: int, src: Path | IO[bytes], gid: int | None) -> dict | None:
    with _lock, _conn() as c:
        if not c.execute("SELECT 1 FROM personas WHERE id = ?", (pid,)).fetchone():
            return None
    img = _write_cover_png(FILES_DIR / str(pid), src)
    img.thumbnail((THUMB_SIDE, THUMB_SIDE))
    img.save(cover_path(pid, True), "WEBP", quality=86)
    with _lock, _conn() as c:
        c.execute("UPDATE personas SET cover = ?, cover_gid = ?, updated_at = ? WHERE id = ?",
                  (str(int(time.time() * 1000)), gid, time.time(), pid))
    return get(pid)


def set_cover_from_gallery(pid: int, gid: int) -> dict | None:
    src = gallery.image_path(gid)
    if not src or not src.exists():
        raise PersonaError("这张图片的文件已不存在")
    return _save_cover(pid, src, gid)


def set_cover_from_ref(pid: int, h: str) -> dict | None:
    src = gallery.ref_path(h)
    if not src:
        raise PersonaError("上传的图片已丢失，请重新上传")
    return _save_cover(pid, src, None)


def set_cover_from_bytes(pid: int, raw: bytes) -> dict | None:
    """人设卡导入：卡片图片本身就是封面。"""
    return _save_cover(pid, io.BytesIO(raw), None)


def migrate_legacy_covers() -> int:
    """旧版封面副本 cover.webp 是缩小过的有损图：来源生成记录的原图还在时，用原图重建无损 cover.png。
    上传的封面、原图已删除的保持 webp。已迁移的不再处理，返回本次迁移的数量。"""
    with _lock, _conn() as c:
        rows = c.execute("SELECT id, cover_gid FROM personas WHERE cover != '' AND cover_gid IS NOT NULL").fetchall()
    n = 0
    for r in rows:
        d = FILES_DIR / str(r["id"])
        if not (d / "cover.webp").exists() or (d / "cover.png").exists():
            continue
        src = gallery.image_path(r["cover_gid"])
        if not src or not src.exists():
            continue
        try:
            _write_cover_png(d, src)
        except OSError as e:
            log.warning("人设 %s 的封面无法从原图重建，保留 webp：%s", r["id"], e)
            continue
        with _lock, _conn() as c:   # 换封面版本号，页面不再用缓存里的旧图（不改 updated_at，列表顺序不变）
            c.execute("UPDATE personas SET cover = ? WHERE id = ?", (str(int(time.time() * 1000)), r["id"]))
        n += 1
    return n


def clear_cover(pid: int) -> dict | None:
    for name in ("cover.png", "cover.png.tmp", "cover.webp", "cover_thumb.webp"):
        (FILES_DIR / str(pid) / name).unlink(missing_ok=True)
    with _lock, _conn() as c:
        c.execute("UPDATE personas SET cover = '', cover_gid = NULL, updated_at = ? WHERE id = ?", (time.time(), pid))
    return get(pid)


# ───────────────────────────── AI 填写：匹配与资料块 ─────────────────────────────

def _hit(name: str, text: str, text_sp: str, text_ns: str) -> int:
    """name 在描述中的位置（找不到为 -1）。英文名按整词匹配，避免 aris 命中 paris。"""
    k = fold(name).strip()
    if len(norm(k)) < 2:
        return -1
    if k.isascii():
        k = re.sub(r"[\s_]+", " ", k)
        m = re.search(rf"(?<![0-9a-z]){re.escape(k)}(?![0-9a-z])", text_sp)
        return m.start() if m else -1
    i = text.find(k)
    return i if i >= 0 else text_ns.find(re.sub(r"\s+", "", k))


def match(text: str, limit: int = 4, extra_names=None) -> list[dict]:
    """描述中出现名称或别名的人设，按出现位置排序：[{persona, matched}]。

    extra_names(p)：另外认的名字（已有角色：角色库里这个角色及其变体的全部名称，如 优香 / ユウカ / hayase yuuka）。
    """
    t = fold(text)
    if not t.strip():
        return []
    t_sp, t_ns = re.sub(r"[\s_]+", " ", t), re.sub(r"\s+", "", t)
    hits = []
    for p in all_full():
        best = None
        for n in [p["name"], *p["aliases"], *(extra_names(p) if extra_names else [])]:
            i = _hit(n, t, t_sp, t_ns)
            if i >= 0 and (best is None or i < best[0]):
                best = (i, n)
        if best:
            hits.append((best[0], {"persona": p, "matched": best[1]}))
    return [h for _, h in sorted(hits, key=lambda x: x[0])][:limit]


def names_norm(p: dict) -> set[str]:
    return {norm(n) for n in [p["name"], *p["aliases"]] if norm(n)}


def strip_original_names(character: str, entries: list[dict]) -> tuple[str, list[str]]:
    """原创人物的名称 / 别名不是 Danbooru 角色标签：从 character 中去掉（忽略大小写、空格、括号后缀）。

    人设里手填的角色标签（含造型专属标签）保留。返回 (新的 character, 被去掉的写法)。
    """
    names: set[str] = set()
    keep: set[str] = set()
    for e in entries:
        p = e["persona"]
        if p["kind"] != "original":
            continue
        names |= names_norm(p)
        keep |= {norm(t) for t in (p["character_tag"], *(o["character_tag"] for o in p["outfits"])) if t}
    if not names:
        return character, []
    out, removed = [], []
    for t in split_list(character):
        k, base = norm(t), norm(re.sub(r"\s*\([^()]*\)\s*$", "", t))   # 如「天童爱丽丝 (original)」也算
        if k not in keep and (k in names or base in names):
            removed.append(t)
        else:
            out.append(t)
    return ", ".join(out), removed


def tags_underscore(p: dict) -> set[str]:
    """人设对应的 Danbooru 角色标签（含造型专属标签），下划线写法，用于跳过重复的联网检索。"""
    out = {t for t in (p["character_tag"], p["source_tag"], *(o["character_tag"] for o in p["outfits"])) if t}
    return {t.strip().replace(" ", "_") for t in out}


def _read(name: str) -> str:
    return (config.PROMPTS_DIR / name).read_text(encoding="utf-8").strip()


def _render(text: str, values: dict) -> str:
    return re.sub(r"\{\{(\w+)\}\}", lambda m: str(values.get(m.group(1), m.group(0))), text)


def _drop_empty_lines(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not re.search(r"[：:]\s*$", line))


def block(entries: list[dict]) -> str:
    """entries：[{persona, matched, outfit_id, linked}] → 附进 AI 填写请求的人设资料块。"""
    labels = json.loads(_read("fill/user/persona_labels.json"))
    items = []
    for e in entries:
        p = e["persona"]
        outfits = []
        outfit_tpl = (config.PROMPTS_DIR / "fill/user/persona_outfit_item.md").read_text(encoding="utf-8").rstrip()  # 保留行首缩进
        for o in p["outfits"]:
            outfits.append(_drop_empty_lines(_render(outfit_tpl, {
                "name": o["name"], "default": labels.get("persona_default_outfit", "") if o["default"] else "",
                "tags": o["tags"], "desc": " ".join(o["desc"].split()), "character_tag": o["character_tag"]})))
        sel = next((o for o in p["outfits"] if o["id"] == e.get("outfit_id")), None)
        how = (_render(labels.get("persona_matched", ""), {"name": e["matched"]}) if e.get("matched")
               else labels.get("persona_linked", ""))
        items.append(_drop_empty_lines(_render(_read("fill/user/persona_item.md"), {
            "name": p["name"], "how": how, "kind": labels.get(f"persona_kind_{p['kind']}", p["kind"]),
            "tag": p["character_tag"], "series": p["series"], "count": p["count"], "artist": p["artist"],
            "base_tags": p["base_tags"], "appearance_desc": " ".join(p["appearance_desc"].split()),
            "selected": sel["name"] if sel else "",
            "outfits": "\n".join(outfits) if outfits else labels.get("persona_no_outfit", "")})))
    if not items:
        return ""
    return _render(_read("fill/user/persona_block.md"), {"personas": "\n\n".join(items)})
