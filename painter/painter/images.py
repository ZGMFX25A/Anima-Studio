"""输入图片解码与预处理、输出图片编码。"""
from __future__ import annotations

import base64
import binascii
import io
import json

from PIL import Image, PngImagePlugin


class BadImage(ValueError):
    pass


def decode(b64: str, field: str) -> Image.Image:
    if b64.startswith("data:"):
        b64 = b64.split(",", 1)[-1]
    try:
        img = Image.open(io.BytesIO(base64.b64decode(b64, validate=False)))
        img.load()
    except (binascii.Error, OSError, ValueError) as e:
        raise BadImage(f"{field}: 无法解码图片 ({e})") from e
    return img


def _limit(img: Image.Image, max_side: int) -> Image.Image:
    if max(img.size) > max_side:
        img = img.copy()
        img.thumbnail((max_side, max_side), Image.LANCZOS)
    return img


def prepare_character_ref(image_b64: str, mask_b64: str | None, field: str, max_side: int) -> bytes:
    """主体合成到白底（in-context LoRA 的训练分布），返回 RGB PNG。"""
    img = decode(image_b64, field)
    if mask_b64:
        mask = decode(mask_b64, field + ".mask").convert("L").resize(img.size, Image.BILINEAR)
    elif img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        mask = img.convert("RGBA").getchannel("A")
    else:
        mask = None
    rgb = img.convert("RGB")
    if mask is not None:
        white = Image.new("RGB", rgb.size, (255, 255, 255))
        rgb = Image.composite(rgb, white, mask)
    return to_png(_limit(rgb, max_side))


def prepare_pose_ref(image_b64: str, input_type: str, field: str, max_side: int) -> bytes:
    img = decode(image_b64, field)
    if img.mode in ("RGBA", "LA", "P"):
        bg = (0, 0, 0) if input_type == "skeleton" else (255, 255, 255)
        rgba = img.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, bg)
        canvas.paste(rgba, mask=rgba.getchannel("A"))
        img = canvas
    return to_png(_limit(img.convert("RGB"), max_side))


def to_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def a1111_text(positive: str, negative: str, params: dict) -> str:
    """A1111 / WebUI 的 parameters 文本（WebUI「PNG 图片信息」、Civitai 等都认）。
    前端迁移旧图时用同样的写法（frontend/server/imagemeta.py），改动要两边一致。"""
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


def encode_output(png_bytes: bytes, fmt: str, quality: int, metadata: dict | None) -> tuple[bytes, int, int]:
    """metadata：写进 PNG 的 anima_painter 文本块（JSON），同时按它写一份 A1111 格式的 parameters。"""
    img = Image.open(io.BytesIO(png_bytes))
    img.load()
    buf = io.BytesIO()
    if fmt == "png":
        info = None
        if metadata is not None:
            info = PngImagePlugin.PngInfo()
            info.add_text("anima_painter", json.dumps(metadata, ensure_ascii=False))
            prompt = metadata.get("prompt") or {}
            info.add_text("parameters", a1111_text(prompt.get("positive", ""), prompt.get("negative", ""),
                                                   metadata.get("params") or {}))
        img.save(buf, "PNG", pnginfo=info, compress_level=4)
    elif fmt == "webp":
        img.save(buf, "WEBP", quality=quality, method=4)
    else:
        img.convert("RGB").save(buf, "JPEG", quality=quality)
    return buf.getvalue(), img.width, img.height
