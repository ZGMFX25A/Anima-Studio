"""服装库「用 AI 起中文名」：给还没有中文名的服装起中文显示名（同时记进名称映射），顺带给作品起中文名与常用简称。

起名只为绘图页的服装检索栏能用中文搜到。手动改过名称的服装不动。
提示词在 prompts/outfit/，每次请求重新读取。完整请求写入 logs/llm_last_request.json。
"""
from __future__ import annotations

import json
import re
import time

from .. import config
from ..web import outfits
from ..web.library import OUTFITS
from . import client, retry
from .fill import _write_log
from .persona import _model

BATCH = 40         # 一次请求最多起几套


def _read(name: str) -> str:
    return (config.PROMPTS_DIR / "outfit" / name).read_text(encoding="utf-8").strip()


def has_cn(name: str) -> bool:
    """名字里有汉字、没有假名，算已有中文名（初星学園这类日文汉字名也算，够用来搜）。"""
    return bool(re.search(r"[一-鿿]", name or "")) and not re.search(r"[぀-ヿ]", name or "")


def pending() -> dict:
    """还要起名的：没有中文名、也没手动改过名称的服装；还没有中文名的作品。"""
    rows = OUTFITS.all_rows()
    known = OUTFITS.series_names()
    return {"outfits": [r for r in rows if "name" not in r["edited"] and not has_cn(r["name"])],
            "series": sorted({r["series_tag"] for r in rows if r["series_tag"] and r["series_tag"] not in known})}


def _line(r: dict) -> str:
    alias = "、".join(n for n in r["other_names"][:4] if n)
    return "；".join(x for x in (r["tag"].replace("_", " "), r["series_tag"].replace("_", " "), alias,
                                outfits.brief(r["wiki"], 160)) if x)


def run(req: dict) -> dict:
    """返回 {named, series, total, series_total}：起好名的服装 / 作品数，以及这次要起的总数。"""
    p, m, level = _model(req)
    rid = str(req.get("request_id") or "")
    todo = pending()
    rows, series = todo["outfits"], todo["series"]
    named = n_series = 0
    batches = [rows[i:i + BATCH] for i in range(0, len(rows), BATCH)] or ([[]] if series else [])
    schema = json.loads(_read("name_schema.json"))
    for i, batch in enumerate(batches):
        ser = series if i == 0 else []
        user = re.sub(r"\{\{(\w+)\}\}", lambda x: {"outfits": "\n".join(f"- {_line(r)}" for r in batch) or "（无）",
                                                   "series": "\n".join(f"- {s.replace('_', ' ')}" for s in ser) or "（无）"}
                      .get(x.group(1), x.group(0)), _read("name_user.md"))
        log: dict = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "mode": "outfit_names", "provider": p["name"],
                     "format": p["format"], "model": m["id"], "level": level, "system": _read("name_system.md"),
                     "user_text": user}
        try:
            raw, _ = retry.chat(p, m, level, log["system"], [{"type": "text", "text": user}], schema,
                                retry.json_check(("outfits", "series")), log, rid)
        except client.LLMError:
            _write_log(log)
            if named or n_series:      # 前面的批次已经起好的保留，告诉页面这次只做了一部分
                return {"named": named, "series": n_series, "total": len(rows), "series_total": len(series),
                        "partial": True}
            raise
        log["result"] = raw
        _write_log(log)
        by_tag = {r["tag"]: r for r in batch}
        for x in raw.get("outfits") or []:
            if not isinstance(x, dict):
                continue
            r = by_tag.get(str(x.get("tag") or "").strip().replace(" ", "_"))
            name = str(x.get("name") or "").strip()
            if r and name and len(name) <= 30:
                OUTFITS.set_name(r["id"], name)
                named += 1
        for x in raw.get("series") or []:
            if not isinstance(x, dict):
                continue
            s = str(x.get("tag") or "").strip().replace(" ", "_")
            names = list(dict.fromkeys(str(n).strip() for n in x.get("names") or [] if str(n).strip() and len(str(n)) <= 20))
            if s in ser and names:
                OUTFITS.set_series_names(s, names[:5])
                n_series += 1
    return {"named": named, "series": n_series, "total": len(rows), "series_total": len(series), "partial": False}
