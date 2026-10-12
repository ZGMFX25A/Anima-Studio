"""识图：AI 填写前先让识图模型逐张详细描述上传的图片（灵感参考图 / 人物参考图 / 动作参考图），
整理成「识图结果」文字块交给正式填写——正式填写不再直接收图片。提示词在 prompts/vision/。

每张图单独请求一次（并行），模型可以专心描述一张图的细节。
同一张图 + 同一用途 + 同一模型与思考档 + 同一提示词的结果缓存在内存里（重启清空），反复点「生成 / 修改」不重复识图。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from .. import config, gallery
from . import client, retry

MAX_WORKERS = 3
CACHE_SIZE = 64
_cache: OrderedDict[str, str] = OrderedDict()
_lock = threading.Lock()


def _read(name: str) -> str:
    return (config.PROMPTS_DIR / name).read_text(encoding="utf-8").strip()


def _render(text: str, values: dict) -> str:
    return re.sub(r"\{\{(\w+)\}\}", lambda m: str(values.get(m.group(1), m.group(0))), text)


def _check(text: str) -> tuple[dict | None, str, str]:
    """纯文字回复：不能为空；很短又带拒绝说法的视为拒绝（正常的详细描述远比这长）。"""
    t = re.sub(r"(?s)<think>.*?</think>", "", text).strip()
    if not t:
        return None, "识图模型没有返回内容", "empty"
    if len(t) < 200 and any(h in t.lower() for h in retry.REFUSAL_HINTS):
        return None, f"识图模型拒绝了这张图：「{t[:120]}」", "refusal"
    return {"text": t}, "", ""


def _prompt_file(item: dict) -> str:
    if item["kind"] == "pose":
        return "vision/pose_skeleton.md" if item.get("input_type") == "skeleton" else "vision/pose_photo.md"
    return f"vision/{item['kind']}.md"


def _one(p: dict, m: dict, level: str, item: dict, rid: str) -> dict:
    out = {**item, "text": "", "error": None, "cached": False, "elapsed": 0.0}
    system = _read("vision/system.md") + "\n\n" + _read(_prompt_file(item))
    user = _read("vision/user.md")
    key = hashlib.sha1(json.dumps([item["hash"], item["kind"], p["id"], m["id"], level, system, user],
                                  ensure_ascii=False).encode("utf-8")).hexdigest()
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return {**out, "text": _cache[key], "cached": True}
    try:
        part = client.image_part(gallery.ref_bytes(item["hash"]))
    except FileNotFoundError:
        return {**out, "error": "文件已丢失"}
    except (OSError, ValueError) as e:
        return {**out, "error": f"图片无法读取：{e}"}
    out["size"] = part.pop("size")
    log: dict = {}
    t0 = time.time()
    try:
        raw, res = retry.chat(p, m, level, system, [part, {"type": "text", "text": user}], None, _check, log, rid)
    except client.LLMError as e:
        return {**out, "error": str(e), "retries": log.get("retries"), "elapsed": round(time.time() - t0, 2)}
    with _lock:
        _cache[key] = raw["text"]
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
    return {**out, "text": raw["text"], "elapsed": round(time.time() - t0, 2), "retries": log.get("retries"),
            "level": res["level"], "level_note": log.get("level_note")}


def describe(p: dict, m: dict, level: str, items: list[dict], request_id: str = "") -> list[dict]:
    """items: [{"kind": inspiration|character|pose, "label", "hash", "input_type"(动作参考)}]。

    返回同顺序的结果，每项加上 text（失败为 ""）、error、cached、elapsed、size、retries，
    以及实际使用的思考档 level、档位被自动改掉时的提示 level_note。
    单张失败不影响其它；取消时由调用方检查 retry.is_cancelled。
    """
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(items))) as ex:
        return list(ex.map(lambda it: _one(p, m, level, it, request_id), items))
