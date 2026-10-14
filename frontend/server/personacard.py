"""人设卡：导出为一张 PNG（卡面就是封面原图），人设的完整资料以 JSON 写在 PNG 的 iTXt 文本块里；导入时读回。

也能读 SillyTavern 角色卡（tEXt 块 chara / ccv3，base64 JSON）：只取名称与描述，建一个原创人设。
聊天软件压缩图片会丢掉文本块，所以分享时要用原图 / 文件方式发送。
"""
from __future__ import annotations

import base64
import binascii
import io
import json
import os
import re
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from PIL.PngImagePlugin import PngInfo

from . import gallery, personas

CHUNK = "anima_persona"            # iTXt 关键字
FORMAT = "anima-studio/persona"
VERSION = 1
EXPORT_KEYS = ("name", "aliases", "kind", "character_tag", "series", "count", "artist", "groups", "base_tags",
               "appearance_desc", "outfits", "source_tag")
MAX_BYTES = 50 * 1024 * 1024
TEXT_LIMIT, SHORT_LIMIT, LIST_LIMIT, OUTFIT_LIMIT = 20000, 300, 50, 50
NO_DATA = "这张图片里没有人设数据。聊天软件发送时会压缩图片、去掉里面的数据，请让对方用「原图」或文件方式发送"


class CardError(ValueError):
    pass


class CardConflict(Exception):
    """名称与已有人设重复，需要用户选择覆盖 / 另存为新 / 跳过。"""

    def __init__(self, info: dict):
        super().__init__(info["message"])
        self.info = info


# ───────────────────────────── 导出 ─────────────────────────────

def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    for name in (["msyhbd.ttc"] if bold else []) + ["msyh.ttc", "simhei.ttf", "Deng.ttf"]:
        try:
            return ImageFont.truetype(str(fonts / name), size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def _fit(draw: ImageDraw.ImageDraw, text: str, width: int, size: int, low: int, bold: bool = False):
    """缩小字号直到放得下；最小字号仍放不下时截断加省略号。"""
    while size > low and draw.textlength(text, font=_font(size, bold)) > width:
        size -= 4
    font = _font(size, bold)
    if draw.textlength(text, font=font) > width:
        while text and draw.textlength(text + "…", font=font) > width:
            text = text[:-1]
        text += "…"
    return text, font


def _placeholder(p: dict) -> Image.Image:
    """没有封面时的卡面：渐变底 + 首字 + 名称。"""
    w, h = 832, 1248
    grad = Image.linear_gradient("L").resize((w, h))
    img = Image.composite(Image.new("RGB", (w, h), (14, 16, 20)), Image.new("RGB", (w, h), (36, 40, 62)), grad)
    glow = Image.new("L", (w, h), 0)
    ImageDraw.Draw(glow).ellipse((w / 2 - 300, h * 0.38 - 300, w / 2 + 300, h * 0.38 + 300), fill=70)
    img.paste((139, 147, 255), mask=glow.filter(ImageFilter.GaussianBlur(120)))
    d = ImageDraw.Draw(img)
    d.text((w / 2, h * 0.38), (p["name"] or "?")[0], font=_font(380, True), fill=(184, 190, 255), anchor="mm")
    name, font = _fit(d, p["name"], w - 120, 72, 32, True)
    d.text((w / 2, h * 0.68), name, font=font, fill=(236, 238, 245), anchor="mm")
    sub = p["series"] if p["kind"] == "existing" and p["series"] else ("原创人物" if p["kind"] == "original" else "")
    if sub:
        sub, font = _fit(d, sub, w - 160, 34, 22)
        d.text((w / 2, h * 0.68 + 74), sub, font=font, fill=(150, 155, 178), anchor="mm")
    d.text((w / 2, h - 64), "Anima Studio · 人设卡", font=_font(26), fill=(105, 110, 132), anchor="mm")
    return img


def _cover_image(p: dict) -> Image.Image | None:
    """封面原图（不缩小）：封面副本 cover.png 就是原图；旧版的 cover.webp 是缩小过的有损副本，
    这时若来源生成记录的文件还在，改用那张原图。"""
    if not p["cover"]:
        return None
    src = personas.cover_path(p["id"])
    if src.suffix == ".webp" and p.get("cover_gid") is not None:
        orig = gallery.image_path(p["cover_gid"])
        if orig and orig.exists():
            src = orig
    if not src.exists():
        return None
    img = Image.open(src)
    img.load()
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "A" in img.getbands() or "transparency" in img.info else "RGB")
    return img


def export_card(pid: int) -> tuple[bytes, str] | None:
    """→ (PNG 内容, 人设名称)；人设不存在时为 None。"""
    p = personas.get(pid)
    if not p:
        return None
    img = _cover_image(p)
    card = {"format": FORMAT, "version": VERSION, "app": "Anima Studio",
            "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "has_cover": img is not None, "persona": {k: p[k] for k in EXPORT_KEYS}}
    if img is None:
        img = _placeholder(p)
    info = PngInfo()
    info.add_itxt(CHUNK, json.dumps(card, ensure_ascii=False, separators=(",", ":")), zip=True)
    info.add_itxt("Title", p["name"])
    info.add_text("Software", "Anima Studio")
    buf = io.BytesIO()
    img.save(buf, "PNG", pnginfo=info)
    return buf.getvalue(), p["name"]


_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_filename(name: str) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(". ")[:80]
    if s.split(".")[0].upper() in _RESERVED:
        s = "_" + s
    return s or "persona"


def disposition(filename: str) -> str:
    ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


def export_zip(ids: list[int]) -> Path:
    """多个人设卡打包成 zip（临时文件，调用方发送后删除）。"""
    fd, tmp = tempfile.mkstemp(prefix="anima_persona_", suffix=".zip")
    os.close(fd)
    used: set[str] = set()
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:   # PNG 本身已压缩
        for pid in ids:
            r = export_card(pid)
            if not r:
                continue
            base = safe_filename(r[1])
            fn, i = base, 2
            while fn.casefold() in used:
                fn, i = f"{base} ({i})", i + 1
            used.add(fn.casefold())
            z.writestr(f"{fn}.png", r[0])
    return Path(tmp)


# ───────────────────────────── 读取 ─────────────────────────────

def _s(v, limit: int = TEXT_LIMIT) -> str:
    return v[:limit] if isinstance(v, str) else ""


def _strs(v) -> list[str] | str:
    if isinstance(v, str):
        return v[:TEXT_LIMIT]
    return [x[:SHORT_LIMIT] for x in v if isinstance(x, str)][:LIST_LIMIT] if isinstance(v, list) else []


def _coerce(p: dict) -> dict:
    """卡里的数据来自别人，按类型逐项取值（之后还会经过 personas._clean 规范化）。"""
    outfits = [o for o in p.get("outfits") if isinstance(o, dict)] if isinstance(p.get("outfits"), list) else []
    return {
        "name": _s(p.get("name"), 100).strip(), "aliases": _strs(p.get("aliases")),
        "kind": p.get("kind") if p.get("kind") in personas.KINDS else "original",
        "character_tag": _s(p.get("character_tag"), SHORT_LIMIT), "series": _s(p.get("series"), SHORT_LIMIT),
        "count": _s(p.get("count"), SHORT_LIMIT), "artist": _s(p.get("artist"), 2000), "groups": _strs(p.get("groups")),
        "base_tags": _s(p.get("base_tags")), "appearance_desc": _s(p.get("appearance_desc")),
        "source_tag": _s(p.get("source_tag"), SHORT_LIMIT),
        "outfits": [{"id": re.sub(r"[^0-9A-Za-z_-]", "", _s(o.get("id"), 32)), "name": _s(o.get("name"), 100),
                     "tags": _s(o.get("tags")), "desc": _s(o.get("desc")),
                     "character_tag": _s(o.get("character_tag"), SHORT_LIMIT), "default": o.get("default") is True}
                    for o in outfits[:OUTFIT_LIMIT]],
    }


def _anima(raw: str) -> dict:
    try:
        j = json.loads(raw)
    except ValueError as e:
        raise CardError("人设卡里的数据已损坏，无法读取") from e
    if not isinstance(j, dict) or j.get("format") != FORMAT or not isinstance(j.get("persona"), dict):
        raise CardError("人设卡里的数据格式不对，无法读取")
    notes = []
    if isinstance(j.get("version"), int) and j["version"] > VERSION:
        notes.append("这张卡来自更新版本的程序，可能有部分内容没有导入")
    return {"source": "anima", "persona": _coerce(j["persona"]), "has_cover": j.get("has_cover") is not False,
            "notes": notes}


def _sillytavern(text: dict) -> dict | None:
    """SillyTavern 角色卡（V1 / V2 / V3）：名称 + 描述（放进外观描述），建原创人设。"""
    for key in ("ccv3", "chara"):
        if key not in text:
            continue
        try:
            j = json.loads(base64.b64decode(text[key]).decode("utf-8"))
        except (ValueError, binascii.Error):
            continue
        d = j.get("data") if isinstance(j, dict) and isinstance(j.get("data"), dict) else j
        if not isinstance(d, dict):
            continue
        name = _s(d.get("name"), 100).strip()
        if not name:
            continue
        desc = re.sub(r"\{\{char\}\}|<BOT>", name, _s(d.get("description")), flags=re.I).strip()
        return {"source": "sillytavern", "has_cover": True,
                "persona": _coerce({"name": name, "kind": "original", "appearance_desc": desc,
                                    "outfits": [{"name": "默认", "default": True}]}),
                "notes": ["SillyTavern 角色卡：只导入了名称与描述（放在外观描述里），可在详情页用「AI 整理」生成标签"]}
    return None


def read(raw: bytes) -> dict:
    """→ {source: anima|sillytavern, persona, has_cover, notes}"""
    if len(raw) > MAX_BYTES:
        raise CardError("文件太大，不是人设卡")
    try:
        img = Image.open(io.BytesIO(raw))
        fmt = img.format
        text = dict(img.text) if fmt == "PNG" else {}
    except Exception as e:  # noqa: BLE001 —— 任何无法解析的文件都按"不是图片"处理
        raise CardError("无法识别的图片") from e
    if fmt != "PNG":
        raise CardError(f"人设卡是 PNG 图片，这张是 {fmt}，里面没有人设数据（可能被转换或压缩过）")
    if CHUNK in text:
        return _anima(text[CHUNK])
    card = _sillytavern(text)
    if card:
        return card
    raise CardError(NO_DATA)


# ───────────────────────────── 导入 ─────────────────────────────

def import_card(raw: bytes, on_conflict: str = "") -> dict:
    """导入一张人设卡并保存。on_conflict：overwrite 覆盖 / rename 另存为新 / skip 跳过；
    名称重复且没有给出处理方式时抛 CardConflict。"""
    card = read(raw)
    d = card["persona"]
    if not d["name"]:
        raise CardError("人设卡里没有名称")
    target, renamed_from = None, ""
    hit = personas.find_conflict(d["name"])
    if hit:
        if on_conflict == "skip":
            return {"status": "skipped", "name": d["name"], "conflict": hit, "source": card["source"]}
        if on_conflict == "overwrite":
            target = hit["id"]
        elif on_conflict == "rename":
            renamed_from, d["name"] = d["name"], personas.unique_name(d["name"])
        else:
            by = "名称" if hit["by"] == "name" else "别名"
            raise CardConflict({"message": f"「{d['name']}」与已有人设「{hit['name']}」的{by}重复",
                                "conflict": hit, "name": d["name"], "source": card["source"],
                                "suggested_name": personas.unique_name(d["name"])})
    dropped = personas.drop_taken_aliases(d, target)
    p = personas.update(target, d) if target else None
    if p is None:     # 新建，或要覆盖的人设刚被删掉
        target = None
        p = personas.create(d)
    notes = list(card["notes"])
    if card["has_cover"]:
        try:
            p = personas.set_cover_from_bytes(p["id"], raw) or p
        except OSError:
            notes.append("封面图片无法读取，没有设置封面")
    return {"status": "overwritten" if target else "created", "persona": p, "source": card["source"],
            "renamed_from": renamed_from, "dropped_aliases": dropped, "notes": notes}
