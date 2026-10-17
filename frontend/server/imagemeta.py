"""图片里的生成参数：写入格式、读取（本程序 / A1111 / ComfyUI / NovelAI）、规则拆分、旧图迁移。

本程序生成的 PNG 带两个文本块（DESIGN.md §14）：
- anima_painter：JSON v2 = {format, version, job_id, params, prompt, form}，form 是各字段原值、画幅、参考设置（不含参考图）；
- parameters：A1111 / WebUI 格式，WebUI「PNG 图片信息」、Civitai 等都认。
新图由绘画模块写入（painter/painter/images.py）；旧图由 migrate() 按图库记录补写，只换文本块，图像数据原样拷贝。
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import struct
import threading
import zlib
from pathlib import Path

from PIL import Image

from . import config, gallery

log = logging.getLogger("frontend.imagemeta")

KEY = "anima_painter"
A1111_KEY = "parameters"
FORMAT = "anima-studio/image"
VERSION = 2
SEED_MAX = 2 ** 53 - 1      # 绘画模块接受的最大种子（JS 能精确表示的整数上限）
FIELD_NAMES = ("quality_meta_year_safe", "count", "character", "series", "artist",
               "appearance", "action", "nltags", "neg")
_MARKER = config.DATA_DIR / "image_meta_v2.done"
_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_TEXT_TYPES = (b"tEXt", b"zTXt", b"iTXt")


# ───────────────────────────── 写入格式 ─────────────────────────────

def form_subset(form: dict) -> dict:
    """写进图片的表单：字段原值、画幅、采样器选择、参考设置。不含参考图（哈希只在本机有意义）、AI 描述与人设。"""
    f = form if isinstance(form, dict) else {}
    fields = f.get("fields") or {}
    out = {"fields": {k: str(fields.get(k) or "") for k in FIELD_NAMES},
           "aspect_ratio": f.get("aspect_ratio") or "", "width": f.get("width"), "height": f.get("height"),
           "sampler": f.get("sampler") or "", "scheduler": f.get("scheduler") or ""}
    cr, pr = f.get("character_ref") or {}, f.get("pose_ref") or {}
    out["character_ref"] = {"enabled": True, "images": len([x for x in cr.get("images") or [] if x]),
                            **{k: cr.get(k) for k in ("strength", "start_percent", "end_percent")}} \
        if cr.get("enabled") else {"enabled": False}
    out["pose_ref"] = {"enabled": True, **{k: pr.get(k) for k in (
        "input_type", "strength", "fit", "margin", "persons", "hands", "face", "feet")}} \
        if pr.get("enabled") else {"enabled": False}
    return out


def a1111_text(positive: str, negative: str, params: dict) -> str:
    """与绘画模块 painter/painter/images.py 的 a1111_text 写法一致，改动要两边同步。"""
    lines = [positive]
    if negative:
        lines.append(f"Negative prompt: {negative}")
    kv = [("Steps", params.get("steps")), ("Sampler", params.get("sampler_name")),
          ("Schedule type", params.get("scheduler")), ("CFG scale", params.get("cfg")),
          ("Seed", params.get("seed")),
          ("Size", f"{params['width']}x{params['height']}" if params.get("width") and params.get("height") else None),
          ("Model", params.get("model")), ("Version", "Anima Studio")]
    lines.append(", ".join(f"{k}: {v}" for k, v in kv if v not in (None, "")))
    return "\n".join(lines)


# ───────────────────────────── PNG 文本块 ─────────────────────────────

def _chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    if not data.startswith(_PNG_SIG):
        raise ValueError("不是 PNG")
    out, pos = [], len(_PNG_SIG)
    while pos + 8 <= len(data):
        n, typ = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        if len(body) < n:
            raise ValueError("PNG 文件不完整")
        out.append((typ, body))
        pos += 12 + n
        if typ == b"IEND":
            break
    if not out or out[0][0] != b"IHDR" or out[-1][0] != b"IEND":
        raise ValueError("PNG 结构不完整")
    return out


def _text_of(typ: bytes, body: bytes) -> tuple[str, str] | None:
    try:
        key, _, rest = body.partition(b"\0")
        k = key.decode("latin-1")
        if typ == b"tEXt":
            return k, rest.decode("latin-1")
        if typ == b"zTXt":
            return k, zlib.decompress(rest[1:]).decode("latin-1")
        flag, rest = rest[0], rest[2:]
        _lang, _, rest = rest.partition(b"\0")
        _trans, _, rest = rest.partition(b"\0")
        return k, (zlib.decompress(rest) if flag else rest).decode("utf-8")
    except (IndexError, ValueError, zlib.error, UnicodeDecodeError):
        return None


def png_texts(data: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    for typ, body in _chunks(data):
        if typ in _TEXT_TYPES and (kv := _text_of(typ, body)):
            out.setdefault(kv[0], kv[1])
    return out


def _chunk(typ: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + typ + body + struct.pack(">I", zlib.crc32(typ + body) & 0xFFFFFFFF)


def _text_chunk(key: str, text: str) -> bytes:
    """能用 latin-1 表示就写 tEXt（兼容性最好），否则写不压缩的 iTXt（UTF-8），与 PIL 的做法一致。"""
    try:
        return _chunk(b"tEXt", key.encode("latin-1") + b"\0" + text.encode("latin-1"))
    except UnicodeEncodeError:
        return _chunk(b"iTXt", key.encode("latin-1") + b"\0\0\0\0\0" + text.encode("utf-8"))


def rewrite_png_texts(path: Path, texts: dict[str, str]) -> None:
    """替换 PNG 中这些关键字的文本块（放在 IHDR 之后），其它块（含图像数据）原样保留。
    先写临时文件再替换，保留原文件的修改时间。"""
    data = path.read_bytes()
    chunks = _chunks(data)
    keep = [(t, b) for t, b in chunks if not (t in _TEXT_TYPES and (_text_of(t, b) or ("", ""))[0] in texts)]
    new = bytearray(_PNG_SIG)
    new += _chunk(*keep[0])
    for k, v in texts.items():
        new += _text_chunk(k, v)
    for t, b in keep[1:]:
        new += _chunk(t, b)
    # 校验：除这些文本块外，其它块与原文件逐字节相同
    if [c for c in _chunks(bytes(new)) if not (c[0] in _TEXT_TYPES and (_text_of(*c) or ("", ""))[0] in texts)] != keep:
        raise ValueError("重写后的结构校验失败")
    st = path.stat()
    tmp = path.with_name(path.name + ".meta-tmp")
    try:
        tmp.write_bytes(bytes(new))
        os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


# ───────────────────────────── 读取 ─────────────────────────────

def _exif_user_comment(img: Image.Image) -> str:
    """JPEG / WebP：A1111 把 parameters 写在 EXIF UserComment。"""
    try:
        raw = img.getexif().get_ifd(0x8769).get(0x9286)
    except Exception:  # noqa: BLE001
        return ""
    if isinstance(raw, str):
        return raw.strip("\0 ")
    if not isinstance(raw, bytes) or len(raw) < 8:
        return ""
    head, body = raw[:8], raw[8:]
    if head.startswith(b"UNICODE"):
        cands = []
        for enc in ("utf-16-be", "utf-16-le"):
            try:
                cands.append(body.decode(enc))
            except UnicodeDecodeError:
                pass
        # 字节序不确定：选可打印字符更多的那种
        return max(cands, key=lambda s: sum(c.isprintable() or c in "\n\r\t" for c in s), default="").strip("\0 ")
    return body.decode("utf-8", "replace").strip("\0 ")


def _parse_a1111(text: str) -> dict | None:
    lines = text.strip().splitlines()
    if not lines:
        return None
    params_line = ""
    if re.match(r"^\s*Steps:\s*\d", lines[-1]):
        params_line = lines.pop()
    pos, neg, in_neg = [], [], False
    for ln in lines:
        if ln.startswith("Negative prompt:"):
            in_neg = True
            ln = ln[len("Negative prompt:"):].strip()
        (neg if in_neg else pos).append(ln)
    kv = dict(re.findall(r'\s*([\w ]+?):\s*("(?:[^"\\]|\\.)*"|[^,]*)(?:,|$)', params_line))
    seed = kv.get("Seed", "").strip()
    return {"positive": "\n".join(pos).strip(), "negative": "\n".join(neg).strip(),
            "seed": int(seed) if seed.isdigit() else None,
            "info": {k.strip(): v.strip() for k, v in kv.items() if k.strip()}}


def _parse_comfy(text: str) -> dict | None:
    """ComfyUI 的 prompt（API 格式工作流）：从采样节点的 positive / negative 沿连线找到文本。"""
    try:
        wf = json.loads(text)
    except ValueError:
        return None
    if not isinstance(wf, dict):
        return None
    nodes = {str(k): v for k, v in wf.items() if isinstance(v, dict)}

    def text_of(ref, depth=0) -> str:
        if isinstance(ref, str):
            return ref
        if depth > 6 or not isinstance(ref, list) or not ref:
            return ""
        node = nodes.get(str(ref[0])) or {}
        inp = node.get("inputs") or {}
        for k in ("text", "text_g", "string", "value", "prompt", "conditioning", "positive"):
            if k in inp:
                t = text_of(inp[k], depth + 1)
                if t:
                    return t
        return ""

    def int_of(ref, depth=0) -> int | None:
        """种子：直接写的数，或连到别的节点（Primitive / Seed 节点）上的数。"""
        if isinstance(ref, int) and not isinstance(ref, bool):
            return ref
        if depth > 4 or not isinstance(ref, list) or not ref:
            return None
        inp = (nodes.get(str(ref[0])) or {}).get("inputs") or {}
        for k in ("seed", "noise_seed", "value", "int", "number"):
            if k in inp and (v := int_of(inp[k], depth + 1)) is not None:
                return v
        return None

    for node in nodes.values():
        inp = node.get("inputs") or {}
        if "positive" in inp:
            pos = text_of(inp["positive"])
            if pos:
                seed = int_of(inp.get("seed"))
                return {"positive": pos.strip(), "negative": text_of(inp.get("negative")).strip(),
                        "seed": seed if seed is not None else int_of(inp.get("noise_seed")), "info": {}}
    texts = [n["inputs"]["text"] for n in nodes.values()
             if "CLIPTextEncode" in str(n.get("class_type")) and isinstance((n.get("inputs") or {}).get("text"), str)]
    if texts:
        return {"positive": texts[0].strip(), "negative": (texts[1] if len(texts) > 1 else "").strip(),
                "seed": None, "info": {}}
    return None


def _parse_novelai(texts: dict) -> dict | None:
    try:
        c = json.loads(texts.get("Comment") or "")
    except ValueError:
        c = None
    if isinstance(c, dict) and (c.get("prompt") or texts.get("Description")):
        return {"positive": str(c.get("prompt") or texts.get("Description") or "").strip(),
                "negative": str(c.get("uc") or "").strip(),
                "seed": c.get("seed") if isinstance(c.get("seed"), int) else None, "info": {}}
    return None


SOURCE_LABELS = {"anima": "Anima Studio", "a1111": "A1111 / WebUI 格式", "comfyui": "ComfyUI 工作流",
                 "novelai": "NovelAI"}


def read(raw: bytes) -> dict:
    """识别图片里的生成参数。优先 anima_painter，其次 A1111 parameters、ComfyUI prompt、NovelAI Comment。"""
    try:
        img = Image.open(io.BytesIO(raw))
    except Exception as e:  # noqa: BLE001
        raise ValueError("无法识别的图片") from e
    texts: dict[str, str] = {}
    if img.format == "PNG":
        try:
            texts = png_texts(raw)
        except ValueError:
            texts = {k: v for k, v in img.info.items() if isinstance(v, str)}
    else:
        texts = {k: v for k, v in img.info.items() if isinstance(v, str)}
        if uc := _exif_user_comment(img):
            texts.setdefault(A1111_KEY, uc)

    if KEY in texts:
        try:
            meta = json.loads(texts[KEY])
        except ValueError:
            meta = None
        if isinstance(meta, dict) and isinstance(meta.get("prompt"), dict):
            params = meta.get("params") or {}
            out = {"source": "anima", "source_label": SOURCE_LABELS["anima"],
                   "version": meta.get("version") or 1, "job_id": meta.get("job_id") or "",
                   "positive": str(meta["prompt"].get("positive") or ""),
                   "negative": str(meta["prompt"].get("negative") or ""),
                   "seed": params.get("seed"), "params": params,
                   "form": meta.get("form") if isinstance(meta.get("form"), dict) else None, "record": None}
            if out["job_id"]:
                rid = gallery.find_by_job(out["job_id"])
                out["record"] = gallery.get(rid) if rid else None
            return out
    parsed, source = None, ""
    if texts.get(A1111_KEY):
        parsed, source = _parse_a1111(texts[A1111_KEY]), "a1111"
    if not parsed and texts.get("prompt"):
        parsed, source = _parse_comfy(texts["prompt"]), "comfyui"
    if not parsed:
        parsed, source = _parse_novelai(texts), "novelai"
    if not parsed or not parsed["positive"]:
        raise ValueError("这张图片里没有找到生成参数。聊天软件或网站压缩过的图片会丢掉参数，请用原图")
    # 种子超出绘画模块的范围（≤ 2^53−1，浏览器也只能精确表示到这里）时不能沿用：ComfyUI 的种子常常更大
    seed = parsed.get("seed")
    if isinstance(seed, int) and not 0 <= seed <= SEED_MAX:
        parsed = {**parsed, "seed": None, "seed_out_of_range": str(seed)}
    return {"source": source, "source_label": SOURCE_LABELS[source], **parsed}


# ───────────────────────────── 规则拆分 ─────────────────────────────

_QUALITY = {
    "masterpiece", "best quality", "high quality", "good quality", "normal quality", "low quality", "worst quality",
    "amazing quality", "great quality", "very aesthetic", "aesthetic", "displeasing", "very displeasing",
    "highres", "absurdres", "lowres", "newest", "recent", "mid", "early", "old", "oldest",
    "safe", "sensitive", "nsfw", "explicit", "questionable", "general",
}
_QUALITY_RE = re.compile(r"^(year \d{4}|score_\d+(_up)?|rating[:_ ].+)$")
_COUNT_RE = re.compile(r"^(\d+\+?(girl|boy|other)s?|multiple (girls|boys|others)|no humans|solo|solo focus)$")
_CHARACTER_RE = re.compile(r"^[^():]+ \([^():]+\)$")    # name (series)，排除 (tag:1.2) 权重写法


def norm_tag(tok: str) -> str:
    """比较用的写法：小写、下划线换空格、去掉权重括号与 :1.2。「名字 (作品)」里的括号保留。"""
    t = tok.strip().lower().replace("_", " ")
    while True:
        u = t
        if t[:1] in "([{" and t[-1:] in ")]}":
            t = t[1:-1].strip()
        elif t[:1] in "([{" and not any(c in t for c in ")]}"):       # (a, b:1.2) 拆开后的前半
            t = t[1:].strip()
        elif t[-1:] in ")]}" and sum(t.count(c) for c in ")]}") > sum(t.count(c) for c in "([{"):   # 后半
            t = t[:-1].strip()
        t = re.sub(r":[\d.]+$", "", t).strip()
        if t == u:
            return t


def split_tokens(text: str) -> list[str]:
    text = re.sub(r"\bBREAK\b", ",", text.replace("\r", " ").replace("\n", ","))
    return [t.strip() for t in text.split(",") if t.strip()]


def rule_split(positive: str, negative: str) -> dict:
    """不用 AI 的粗分：质量 / 年份 / 安全词 → quality_meta_year_safe，人数 → count，@画师 → artist，
    「名字 (作品)」→ character，长句 → nltags，其余 → appearance；负向原样进 neg。"""
    buckets = {k: [] for k in FIELD_NAMES}
    sentence: list[str] = []
    for tok in split_tokens(positive):
        n = norm_tag(tok)
        words = len(n.split())
        if sentence or (words >= 5 and not _CHARACTER_RE.match(n)):
            sentence.append(tok)
            if tok.rstrip().endswith((".", "!", "?")):
                buckets["nltags"].append(", ".join(sentence))
                sentence = []
            continue
        if n in _QUALITY or _QUALITY_RE.match(n):
            buckets["quality_meta_year_safe"].append(tok)
        elif _COUNT_RE.match(n):
            buckets["count"].append(tok)
        elif tok.startswith("@") or n.startswith("artist:"):
            buckets["artist"].append(tok if tok.startswith("@") else "@" + tok.split(":", 1)[1].strip())
        elif _CHARACTER_RE.match(n) and not buckets["character"]:
            buckets["character"].append(tok)
        else:
            buckets["appearance"].append(tok)
    if sentence:
        buckets["nltags"].append(", ".join(sentence))
    out = {k: ", ".join(v) for k, v in buckets.items()}
    out["nltags"] = " ".join(buckets["nltags"])
    out["neg"] = ", ".join(split_tokens(negative))
    return out


# ───────────────────────────── 旧图迁移 ─────────────────────────────

_migrate_lock = threading.Lock()


def _meta_for(rec: dict, old: dict | None) -> dict:
    params = {"model": rec["model"], "width": rec["width"], "height": rec["height"], "seed": rec["seed"],
              "steps": rec["steps"], "cfg": rec["cfg"], "sampler_name": rec["sampler"], "scheduler": rec["scheduler"]}
    if old and isinstance(old.get("params"), dict):        # 旧 v1 块里的参数更全（model_file、stages 等）
        params = {**params, **{k: v for k, v in old["params"].items() if v is not None}}
    return {"format": FORMAT, "version": VERSION, "job_id": rec["job_id"], "params": params,
            "prompt": {"positive": rec["positive"] or "", "negative": rec["negative"] or ""},
            "form": form_subset(rec["form"])}


def migrate(force: bool = False) -> dict:
    """给图库里的旧图补写 v2 anima_painter 与 A1111 parameters。已是 v2 的跳过（force 时也重写）。"""
    stats = {"total": 0, "updated": 0, "skipped": 0, "missing": 0, "unsupported": 0, "failed": 0}
    with _migrate_lock:
        for gid in gallery.all_ids():
            rec = gallery.get(gid)
            if not rec:
                continue
            stats["total"] += 1
            path = Path(rec["path"])
            if not path.exists():
                stats["missing"] += 1
                continue
            if path.suffix.lower() != ".png":
                stats["unsupported"] += 1
                continue
            try:
                texts = png_texts(path.read_bytes())
                old = None
                try:
                    old = json.loads(texts[KEY]) if KEY in texts else None
                except ValueError:
                    pass
                if not force and isinstance(old, dict) and old.get("version", 1) >= VERSION and A1111_KEY in texts:
                    stats["skipped"] += 1
                    continue
                meta = _meta_for(rec, old if isinstance(old, dict) else None)
                rewrite_png_texts(path, {
                    KEY: json.dumps(meta, ensure_ascii=False),
                    A1111_KEY: a1111_text(meta["prompt"]["positive"], meta["prompt"]["negative"], meta["params"])})
                stats["updated"] += 1
            except (OSError, ValueError) as e:
                stats["failed"] += 1
                log.warning("写入生成参数失败：%s（%s）", path, e)
    return stats


def migrate_once() -> dict | None:
    """启动时调用：只自动跑一次（完成后留标记）；之后可在 菜单 → 存储 手动再跑。"""
    if _MARKER.exists():
        return None
    stats = migrate()
    if not stats["failed"]:
        _MARKER.write_text(json.dumps(stats), encoding="utf-8")
    return stats
