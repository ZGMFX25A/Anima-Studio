"""图库：生成记录（SQLite）、参考图（按内容哈希存储）、缩略图。"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import sqlite3
import threading
import time
from pathlib import Path

from PIL import Image, ImageOps

from . import config

_lock = threading.Lock()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    path TEXT NOT NULL,
    width INTEGER, height INTEGER, seed INTEGER,
    model TEXT, steps INTEGER, cfg REAL, sampler TEXT, scheduler TEXT,
    positive TEXT, negative TEXT,
    form TEXT NOT NULL,          -- 前端表单原值（JSON），复用时据此恢复
    refs TEXT NOT NULL,          -- 引用到的参考图哈希列表（JSON）
    favorite INTEGER NOT NULL DEFAULT 0,
    favorited_at REAL,
    persona_id INTEGER           -- 人设预览图：只在人设页管理，不出现在历史 / 收藏
);
CREATE INDEX IF NOT EXISTS idx_images_created ON images(created_at);
CREATE INDEX IF NOT EXISTS idx_images_fav ON images(favorite, favorited_at);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


with _conn() as _c:
    _c.executescript(_SCHEMA)
    if "persona_id" not in {r[1] for r in _c.execute("PRAGMA table_info(images)")}:   # 旧库补列
        _c.execute("ALTER TABLE images ADD COLUMN persona_id INTEGER")
    _c.execute("CREATE INDEX IF NOT EXISTS idx_images_persona ON images(persona_id)")


# ───────────────────────────── 参考图 ─────────────────────────────

_EXT = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}


def decode_data_url(data: str) -> bytes:
    if data.startswith("data:"):
        data = data.split(",", 1)[1]
    return base64.b64decode(data)


def save_ref(data: str) -> str:
    """保存一张参考图，返回内容哈希。PNG/JPEG/WebP 原样保存，其它格式转 PNG。
    手机竖拍的照片是"横放像素 + EXIF 方向标记"，而缩略图、LLM、绘画模块都只按像素读图，所以先转正再保存。"""
    raw = decode_data_url(data)
    try:
        img = Image.open(io.BytesIO(raw))
        fmt = img.format
        img.load()
    except Exception as e:  # noqa: BLE001
        raise ValueError("无法识别的图片") from e
    rotated = img.getexif().get(0x0112, 1) not in (0, 1)   # 0x0112 = Orientation
    if rotated:
        img = ImageOps.exif_transpose(img)
    if rotated or fmt not in _EXT:
        fmt = fmt if fmt in _EXT else "PNG"
        buf = io.BytesIO()
        img.save(buf, fmt, quality=95)
        raw = buf.getvalue()
    h = hashlib.sha256(raw).hexdigest()[:32]
    path = config.REFS_DIR / f"{h}.{_EXT[fmt]}"
    if not path.exists():
        path.write_bytes(raw)
    return h


def ref_path(h: str) -> Path | None:
    if not h.isalnum():
        return None
    for p in config.REFS_DIR.glob(f"{h}.*"):
        return p
    return None


def ref_b64(h: str) -> str:
    p = ref_path(h)
    if not p:
        raise FileNotFoundError(h)
    return base64.b64encode(p.read_bytes()).decode()


def ref_bytes(h: str) -> bytes:
    p = ref_path(h)
    if not p:
        raise FileNotFoundError(h)
    return p.read_bytes()


def _referenced_hashes(c: sqlite3.Connection) -> set[str]:
    out: set[str] = set()
    for (refs,) in c.execute("SELECT refs FROM images"):
        out.update(json.loads(refs))
    return out


def cleanup_refs(keep: set[str] | None = None, min_age: float = 0.0) -> int:
    """删除没有任何记录引用的参考图（keep 中的与修改时间不足 min_age 秒的保留）。"""
    keep = keep or set()
    with _lock, _conn() as c:
        used = _referenced_hashes(c)
    n = 0
    now = time.time()
    for p in config.REFS_DIR.iterdir():
        h = p.stem
        if h in used or h in keep:
            continue
        try:
            if now - p.stat().st_mtime < min_age:
                continue
            p.unlink()
            n += 1
        except OSError:
            pass
    return n


# ───────────────────────────── 记录 ─────────────────────────────

def _row(r: sqlite3.Row, full: bool = False) -> dict:
    d = {k: r[k] for k in ("id", "job_id", "created_at", "width", "height", "seed", "model", "steps", "cfg",
                           "sampler", "scheduler", "favorite", "persona_id")}
    d["favorite"] = bool(d["favorite"])
    d["exists"] = Path(r["path"]).exists()
    d["filename"] = Path(r["path"]).name
    if full:
        d.update(path=r["path"], positive=r["positive"], negative=r["negative"], form=json.loads(r["form"]),
                 refs=json.loads(r["refs"]))
    return d


def add(*, job_id: str, path: str, width: int, height: int, seed: int | None, params: dict, positive: str,
        negative: str, form: dict, refs: list[str], persona_id: int | None = None) -> int:
    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT INTO images (job_id, created_at, path, width, height, seed, model, steps, cfg, sampler, scheduler,"
            " positive, negative, form, refs, persona_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, time.time(), path, width, height, seed, params.get("model"), params.get("steps"),
             params.get("cfg"), params.get("sampler_name"), params.get("scheduler"), positive, negative,
             json.dumps(form, ensure_ascii=False), json.dumps(sorted(set(refs))), persona_id))
        return cur.lastrowid


def list_items(favorite: bool, offset: int, limit: int) -> dict:
    """历史 / 收藏：不含人设预览图。"""
    with _lock, _conn() as c:
        if favorite:
            where, order = "WHERE persona_id IS NULL AND favorite = 1", "favorited_at DESC, id DESC"
        else:
            where, order = "WHERE persona_id IS NULL", "id DESC"
        total = c.execute(f"SELECT COUNT(*) FROM images {where}").fetchone()[0]
        rows = c.execute(f"SELECT * FROM images {where} ORDER BY {order} LIMIT ? OFFSET ?", (limit, offset)).fetchall()
    return {"total": total, "items": [_row(r, full=True) for r in rows]}


def list_persona(persona_id: int) -> list[dict]:
    with _lock, _conn() as c:
        rows = c.execute("SELECT * FROM images WHERE persona_id = ? ORDER BY id DESC", (persona_id,)).fetchall()
    return [_row(r, full=True) for r in rows]


def persona_image_ids(persona_id: int) -> list[int]:
    with _lock, _conn() as c:
        return [r[0] for r in c.execute("SELECT id FROM images WHERE persona_id = ?", (persona_id,))]


def get(item_id: int) -> dict | None:
    with _lock, _conn() as c:
        r = c.execute("SELECT * FROM images WHERE id = ?", (item_id,)).fetchone()
    return _row(r, full=True) if r else None


def find_by_job(job_id: str) -> int | None:
    with _lock, _conn() as c:
        r = c.execute("SELECT id FROM images WHERE job_id = ? ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
    return r[0] if r else None


def all_ids() -> list[int]:
    with _lock, _conn() as c:
        return [r[0] for r in c.execute("SELECT id FROM images ORDER BY id")]


def image_path(item_id: int) -> Path | None:
    with _lock, _conn() as c:
        r = c.execute("SELECT path FROM images WHERE id = ?", (item_id,)).fetchone()
    return Path(r["path"]) if r else None


def toggle_favorite(item_id: int) -> bool | None:
    with _lock, _conn() as c:
        r = c.execute("SELECT favorite FROM images WHERE id = ?", (item_id,)).fetchone()
        if not r:
            return None
        fav = not r["favorite"]
        c.execute("UPDATE images SET favorite = ?, favorited_at = ? WHERE id = ?",
                  (int(fav), time.time() if fav else None, item_id))
    return fav


def delete(item_id: int, keep: set[str]) -> bool:
    """永久删除原图文件、缩略图与记录；没有其它记录引用（且不在 keep 中）的参考图一并删除。"""
    with _lock, _conn() as c:
        r = c.execute("SELECT path, refs FROM images WHERE id = ?", (item_id,)).fetchone()
        if not r:
            return False
        c.execute("DELETE FROM images WHERE id = ?", (item_id,))
        refs = set(json.loads(r["refs"]))
        used = _referenced_hashes(c)
    try:
        Path(r["path"]).unlink(missing_ok=True)
    except OSError:
        pass
    thumb_path(item_id).unlink(missing_ok=True)
    for h in refs - used - keep:
        p = ref_path(h)
        if p:
            p.unlink(missing_ok=True)
    return True


# ───────────────────────────── 缩略图 ─────────────────────────────

def thumb_path(item_id: int) -> Path:
    return config.THUMBS_DIR / f"{item_id}.webp"


def thumbnail(item_id: int, size: int = 512) -> Path | None:
    tp = thumb_path(item_id)
    if tp.exists():
        return tp
    src = image_path(item_id)
    if not src or not src.exists():
        return None
    img = Image.open(src)
    img.thumbnail((size, size))
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    img.save(tp, "WEBP", quality=88)
    return tp


def ref_thumbnail(h: str, size: int = 256) -> bytes | None:
    p = ref_path(h)
    if not p:
        return None
    img = Image.open(p)
    img.thumbnail((size, size))
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA")
    buf = io.BytesIO()
    img.save(buf, "WEBP", quality=85)
    return buf.getvalue()
