"""读图：把其它工具生成的图片里的提示词交给 LLM，原样拆分到各字段。

提示词在 prompts/import/，每次请求重新读取。完整请求写入 logs/llm_last_request.json。
"""
from __future__ import annotations

import json
import re
import time

from .. import config
from ..imagemeta import FIELD_NAMES, norm_tag, split_tokens
from . import client, retry, store
from .fill import _write_log


def _read(name: str) -> str:
    return (config.PROMPTS_DIR / "import" / name).read_text(encoding="utf-8").strip()


def _render(text: str, values: dict) -> str:
    return re.sub(r"\{\{(\w+)\}\}", lambda m: str(values.get(m.group(1), m.group(0))), text)


def split(req: dict) -> dict:
    rid = str(req.get("request_id") or "")
    try:
        return _split(req, rid)
    finally:
        retry.clear(rid)


def _split(req: dict, rid: str) -> dict:
    t0 = time.time()
    p, m = store.find_model(req.get("provider_id", ""), req.get("model_id", ""))
    if not p or not m:
        raise client.LLMError("所选 LLM 模型不存在，请在 菜单 → LLM 中勾选模型")
    levels = (m.get("thinking") or {}).get("levels") or ["off"]
    level = req.get("level") or m.get("level") or levels[0]
    level = level if level in levels else levels[0]
    positive = str(req.get("positive") or "").strip()
    negative = str(req.get("negative") or "").strip()
    labels = json.loads(_read("labels.json"))
    system = _read("system.md")
    user = _render(_read("user.md"), {"positive": positive, "negative": negative or labels["empty"]})
    log: dict = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "mode": "image_import", "provider": p["name"],
                 "format": p["format"], "model": m["id"], "level": level, "system": system, "user_text": user}
    try:
        raw, _ = retry.chat(p, m, level, system, [{"type": "text", "text": user}], json.loads(_read("schema.json")),
                            retry.json_check(FIELD_NAMES), log, rid)
    except client.LLMError:
        _write_log(log)
        raise
    _write_log(log)
    warnings = retry.notes(log)

    out = {}
    for k in FIELD_NAMES:
        v = raw.get(k)
        out[k] = (", ".join(str(x) for x in v) if isinstance(v, list) else str(v or "")).strip()
    toks = [t.strip() for t in out["artist"].split(",") if t.strip()]
    out["artist"] = ", ".join(t if t.startswith("@") else "@" + t for t in toks)

    # 校验：原文的标签都要出现在某个字段里，LLM 漏掉的放进 appearance / neg
    def key(t: str) -> str:
        return re.sub(r"^(@|artist:\s*|by\s+)", "", norm_tag(t))

    def missing(src: str, keys: tuple[str, ...]) -> list[str]:
        have = " , ".join(key(x) for k in keys for x in split_tokens(out[k]))
        have += " " + " ".join(norm_tag(out[k]) for k in keys)
        return [t for t in split_tokens(src) if key(t) and key(t) not in have]

    lost_pos = missing(positive, tuple(k for k in FIELD_NAMES if k != "neg"))
    lost_neg = missing(negative, ("neg",))
    if lost_pos:
        out["appearance"] = ", ".join(x for x in (out["appearance"], ", ".join(lost_pos)) if x)
    if lost_neg:
        out["neg"] = ", ".join(x for x in (out["neg"], ", ".join(lost_neg)) if x)
    if lost_pos or lost_neg:
        warnings.append(f"AI 漏掉了 {len(lost_pos) + len(lost_neg)} 个标签，已补进 appearance / neg："
                        f"{', '.join((lost_pos + lost_neg)[:8])}{' …' if len(lost_pos) + len(lost_neg) > 8 else ''}")
    return {"fields": out, "warnings": warnings, "elapsed": round(time.time() - t0, 2)}
