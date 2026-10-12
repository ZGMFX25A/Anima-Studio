"""DanbooruSearch（HF Space）语义标签检索：描述 → 标准 Danbooru 标签候选。结果按「描述 + 参数」永久缓存。

额度：不带 Key 使用公共池（所有人共享）；填入个人 Key + Client 后使用个人额度。search 每次 3 点。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time

import httpx

from .. import config
from . import net
from .net import WebError

HOST = "https://sakizuki-danboorusearch.hf.space"
DB_PATH = config.DATA_DIR / "tagsearch_cache.db"
_lock = threading.Lock()

with sqlite3.connect(DB_PATH) as _c:
    _c.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, query TEXT, result TEXT, created REAL)")


def _headers(ts: dict) -> dict:
    h = {"Content-Type": "application/json"}
    if ts.get("api_key"):                      # 无 Key 时完全不发送 Authorization（对方要求）
        h["Authorization"] = f"Bearer {ts['api_key']}"
        if ts.get("client"):
            h["X-DanbooruSearch-Client"] = ts["client"]
    return h


def _post(path: str, body: dict, ts: dict, timeout: float = 60, net_override: dict | None = None) -> dict:
    try:
        with net.client(timeout, net_override) as c:
            r = c.post(HOST + path, json=body, headers=_headers(ts))
    except httpx.HTTPError as e:
        raise WebError(f"无法连接 DanbooruSearch：{e}") from e
    try:
        d = r.json()
    except ValueError:
        d = {}
    if r.status_code != 200:
        msg = d.get("detail") or d.get("error") or d.get("api_key_notice") or r.text[:200]
        if isinstance(msg, dict):
            msg = msg.get("message") or json.dumps(msg, ensure_ascii=False)
        hint = {401: "（Key 无效；无效 Key 不会回退到公共池）", 403: "（Key 或 Client 不匹配）",
                429: "（今日额度已用完）"}.get(r.status_code, "")
        raise WebError(f"DanbooruSearch 返回 HTTP {r.status_code}{hint}：{msg}")
    return d


def remaining(notice: str) -> int | None:
    m = re.search(r"剩余\s*(\d+)\s*点", notice or "")
    return int(m.group(1)) if m else None


def search(query: str, nsfw: bool) -> dict:
    """返回 {results: [{tag, cn, count, wiki}], notice, remaining, cached, pool}。"""
    ts = net.get()["tagsearch"]
    body = {"query": query.strip(), "top_k": ts["top_k"], "limit": ts["limit"],
            "popularity_weight": ts["popularity_weight"], "show_nsfw": bool(nsfw), "use_segmentation": True,
            "target_categories": ["General"], "group_mode": "off"}
    key = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    pool = "personal" if ts.get("api_key") else "public"
    with _lock, sqlite3.connect(DB_PATH) as c:
        row = c.execute("SELECT result FROM cache WHERE key = ?", (key,)).fetchone()
    if row:
        return {**json.loads(row[0]), "cached": True, "pool": pool}
    d = _post("/api/search", body, ts)
    results = [{"tag": x.get("tag", ""), "cn": (x.get("cn_name") or "").split(",")[0], "count": x.get("count") or 0,
                "wiki": x.get("wiki") or ""} for x in d.get("results", []) if x.get("tag")]
    out = {"results": results, "notice": d.get("api_key_notice", ""), "remaining": remaining(d.get("api_key_notice"))}
    with _lock, sqlite3.connect(DB_PATH) as c:
        c.execute("INSERT OR REPLACE INTO cache (key, query, result, created) VALUES (?,?,?,?)",
                  (key, query, json.dumps(out, ensure_ascii=False), time.time()))
    return {**out, "cached": False, "pool": pool}


def test(override: dict | None = None) -> dict:
    """测试：发一次最小的搜索（消耗 3 点），返回连通性、额度池与剩余额度。

    override 为页面上尚未保存的输入（api_key / client / proxy_mode / proxy_url），只用于这次测试。
    """
    override = override or {}
    ts = net.get()["tagsearch"]
    if override.get("api_key"):
        ts["api_key"] = override["api_key"].strip()
    if override.get("client") is not None:
        ts["client"] = override["client"].strip()
    t0 = time.time()
    d = _post("/api/search", {"query": "smile", "top_k": 1, "limit": 1, "show_nsfw": False,
                              "use_segmentation": False}, ts, timeout=60, net_override=override)
    return {"ok": True, "elapsed": round(time.time() - t0, 2), "pool": "personal" if ts.get("api_key") else "public",
            "notice": d.get("api_key_notice", ""), "remaining": remaining(d.get("api_key_notice")),
            "sample": (d.get("results") or [{}])[0].get("tag")}


def clear_cache() -> int:
    with _lock, sqlite3.connect(DB_PATH) as c:
        return c.execute("DELETE FROM cache").rowcount
