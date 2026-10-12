"""联网设置（联网方式、DanbooruSearch Key 与参数），与 LLM 服务商共用 data/settings.json。"""
from __future__ import annotations

import copy

import httpx

from ..llm import store

DEFAULTS = {
    "proxy_mode": "direct",          # direct / system / custom
    "proxy_url": "",
    "tagsearch": {"api_key": "", "client": "", "top_k": 5, "limit": 40, "popularity_weight": 0.15},
}


class WebError(Exception):
    pass


def get() -> dict:
    with store._lock:
        d = store._load()
    out = copy.deepcopy(DEFAULTS)
    w = d.get("web") or {}
    for k in ("proxy_mode", "proxy_url"):
        if k in w:
            out[k] = w[k]
    out["tagsearch"].update(w.get("tagsearch") or {})
    return out


def public() -> dict:
    s = get()
    key = s["tagsearch"].pop("api_key")
    s["tagsearch"]["has_key"] = bool(key)
    s["tagsearch"]["api_key_masked"] = store.mask(key)
    return s


def update(data: dict) -> dict:
    with store._lock:
        d = store._load()
        w = d.setdefault("web", {})
        if data.get("proxy_mode") in ("direct", "system", "custom"):
            w["proxy_mode"] = data["proxy_mode"]
        if "proxy_url" in data:
            w["proxy_url"] = str(data["proxy_url"] or "").strip()
        ts_in = data.get("tagsearch") or {}
        ts = w.setdefault("tagsearch", {})
        if ts_in.get("api_key"):                     # 留空表示不修改
            ts["api_key"] = ts_in["api_key"].strip()
        if "client" in ts_in:
            ts["client"] = str(ts_in["client"] or "").strip()
        for k, lo, hi, typ in (("top_k", 1, 200, int), ("limit", 1, 200, int), ("popularity_weight", 0.0, 1.0, float)):
            if ts_in.get(k) not in (None, ""):
                ts[k] = min(hi, max(lo, typ(ts_in[k])))
        store._save(d)
    return public()


def reset_tagsearch() -> dict:
    """恢复默认：清空 Key 与 Client（回到公共池），召回参数还原。"""
    with store._lock:
        d = store._load()
        d.setdefault("web", {})["tagsearch"] = copy.deepcopy(DEFAULTS["tagsearch"])
        store._save(d)
    return public()


def client(timeout: float = 20.0, override: dict | None = None) -> httpx.Client:
    """override：测试时临时使用的联网方式（尚未保存的输入），不写入设置。"""
    s = get()
    for k in ("proxy_mode", "proxy_url"):
        if override and override.get(k) is not None:
            s[k] = override[k]
    if s["proxy_mode"] == "custom" and s["proxy_url"]:
        return httpx.Client(proxy=s["proxy_url"], trust_env=False, timeout=timeout)
    return httpx.Client(trust_env=s["proxy_mode"] == "system", timeout=timeout)
