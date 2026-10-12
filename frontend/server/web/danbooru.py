"""Danbooru 查询：角色名解析（补全接口支持中日文别名 / 旧名 / 单词匹配）、wiki 简介、共现外貌标签。"""
from __future__ import annotations

import json
import re
import threading
import time

import httpx

from .. import config
from .net import WebError, client

BASE = "https://danbooru.donmai.us"
UA = "AnimaStudio/1.0 (local prompt helper)"
CAT_ARTIST, CAT_COPYRIGHT, CAT_CHARACTER = 1, 3, 4
MIN_INTERVAL = 0.35              # 请求间隔（Danbooru 建议持续访问约 1 次/秒以内，短时可更快）
WIKI_CHARS = 600
APPEARANCE_TOP = 25

_rate_lock = threading.Lock()
_last = [0.0]


def _get(path: str, params: dict | None = None, retries: int = 2, net_override: dict | None = None):
    """GET JSON；404 返回 None；5xx / 429（Danbooru 偶发超时）自动重试。"""
    for attempt in range(retries + 1):
        with _rate_lock:
            wait = _last[0] + MIN_INTERVAL - time.time()
            if wait > 0:
                time.sleep(wait)
            _last[0] = time.time()
        try:
            with client(20, net_override) as c:
                r = c.get(BASE + path, params=params, headers={"User-Agent": UA})
        except httpx.HTTPError as e:
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise WebError(f"无法连接 Danbooru：{e}") from e
        if r.status_code == 404:
            return None
        if r.status_code == 200:
            return r.json()
        if (r.status_code >= 500 or r.status_code == 429) and attempt < retries:
            time.sleep(1.5 * (attempt + 1))
            continue
        raise WebError(f"Danbooru 返回 HTTP {r.status_code}（{path}）")


def autocomplete(query: str, net_override: dict | None = None, category: int = CAT_CHARACTER) -> list[dict]:
    """某一类别的补全结果：[{tag, post_count, matched}]，tag 已是正式标签（别名、各语言别名已解析）。"""
    query = query.strip()
    if not query:
        return []
    d = _get("/autocomplete.json", {"search[query]": query, "search[type]": "tag_query", "limit": 10},
             net_override=net_override) or []
    out = []
    for x in d:
        if x.get("category") == category and x.get("value") and x["value"] not in (o["tag"] for o in out):
            out.append({"tag": x["value"], "post_count": x.get("post_count") or 0,
                        "matched": x.get("antecedent") or x.get("label") or ""})
    return out


def find_character(name: str, name_en: str, series: str) -> list[dict]:
    """按 原文写法 → 英文名+作品 → 英文名 依次查，返回候选（按图片数降序，最多 4 个）。"""
    tries = [name]
    if name_en:
        en = name_en.replace("_", " ").strip()
        if series:
            tries.append(f"{en} {series.replace('_', ' ')}")
        tries.append(en)
    for q in tries:
        if not q or not q.strip():
            continue
        cands = autocomplete(q)
        if cands:
            if series:   # 有作品名时，带作品名的候选优先
                s = re.sub(r"[\s_]+", "_", series.lower().strip())
                cands.sort(key=lambda c: (s not in c["tag"], -c["post_count"]))
            else:
                cands.sort(key=lambda c: -c["post_count"])
            return cands[:4]
    return []


_DTEXT = [
    (re.compile(r"\[\[([^\]|]+)\|([^\]]*)\]\]"), lambda m: m.group(2) or m.group(1).split("(")[0].strip()),
    (re.compile(r"\[\[([^\]]+)\]\]"), lambda m: m.group(1)),
    (re.compile(r'"([^"]+)":\S+'), lambda m: m.group(1)),
    (re.compile(r"\[/?(?:b|i|u|s|tn|spoiler|expand[^\]]*|quote|code|nodtext)\]", re.I), lambda m: ""),
    (re.compile(r"^h\d\.\s*", re.M), lambda m: ""),
    (re.compile(r"\{\{([^}]+)\}\}"), lambda m: m.group(1)),
]


def clean_dtext(body: str, limit: int = WIKI_CHARS) -> str:
    # 去掉图片 / 资源嵌入行（如 "* !post #4447168: Default"）
    text = "\n".join(line for line in (body or "").splitlines() if not re.search(r"!(?:post|asset)\s*#\d+", line))
    for pat, rep in _DTEXT:
        text = pat.sub(rep, text)
    # 「See also / External links」之后多是链接列表，对外貌没有帮助
    text = re.split(r"^\s*(?:See also|External links?|Related tags?|Tag group)\s*:?\s*$", text, flags=re.M | re.I)[0]
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    text = re.sub(r"\n\s*Appearance\s*$", "", text, flags=re.I).strip()
    if len(text) > limit:
        cut = text[:limit]
        dot = max(cut.rfind(". "), cut.rfind("。"), cut.rfind("\n"))
        text = (cut[:dot + 1] if dot > limit * 0.5 else cut).rstrip() + " …"
    return text


def _stoplist() -> set[str]:
    try:
        return set(json.loads((config.CONFIG_DIR / "danbooru_stoplist.json").read_text(encoding="utf-8"))["tags"])
    except (OSError, ValueError, KeyError):
        return set()


def related(tag: str, category: str, limit: int) -> list[tuple[str, float]]:
    d = _get("/related_tag.json", {"query": tag, "category": category, "limit": limit}) or {}
    return [(t["tag"]["name"], round(float(t.get("frequency") or 0), 2)) for t in d.get("related_tags", [])
            if t.get("tag", {}).get("name")]


def profile(tag: str, post_count: int | None = None, general_limit: int = 60) -> dict:
    """取角色资料：wiki 简介、别名、作品标签、外貌标签（共现比例）。

    general：排除停用词后的全部共现通用标签 [(tag, freq)]，人设导入拆分时使用（不存进角色库）。
    """
    wiki = _get(f"/wiki_pages/{tag}.json") or {}
    general = general_tags(tag, general_limit)
    appearance = [{"tag": t, "freq": f} for t, f in general if f >= 0.08][:APPEARANCE_TOP]
    copy = [t for t, _ in related(tag, "copyright", 3)]
    if post_count is None:
        info = _get("/tags.json", {"search[name]": tag, "only": "post_count"}) or [{}]
        post_count = info[0].get("post_count") if info else None
    return {
        "tag": tag, "series_tag": copy[0] if copy else "", "post_count": post_count or 0,
        "other_names": [n for n in (wiki.get("other_names") or []) if n][:20],
        "wiki": clean_dtext(wiki.get("body") or ""), "appearance": appearance, "general": general,
    }


def general_tags(tag: str, limit: int = 60) -> list[tuple[str, float]]:
    """排除停用词后的共现通用标签 [(tag, freq)]。"""
    stop = _stoplist()
    return [(t, f) for t, f in related(tag, "general", limit) if t not in stop]


def variants(tag: str, limit: int = 15) -> list[dict]:
    """造型变体标签：Danbooru 给部分角色的服装 / 形态单独建的角色标签。

    aris_(blue_archive) → 查 aris_(*)_(blue_archive)（如 aris_(maid)_(blue_archive)）；
    hatsune_miku → 查 hatsune_miku_(*)（如 hatsune_miku_(append)）。没有图片的标签与 cosplay 标签不列出。
    """
    m = re.match(r"^(.+?)_(\([^()]+\))$", tag)
    pattern = f"{m.group(1)}_(*)_{m.group(2)}" if m else f"{tag}_(*)"
    d = _get("/tags.json", {"search[name_matches]": pattern, "search[category]": CAT_CHARACTER,
                            "search[order]": "count", "limit": limit + 5, "only": "name,post_count"}) or []
    out = [{"tag": x["name"], "post_count": x.get("post_count") or 0} for x in d
           if x.get("name") and x["name"] != tag and (x.get("post_count") or 0) > 0
           and not x["name"].endswith("_(cosplay)")]
    return out[:limit]


def find_artist(name: str, name_en: str) -> list[dict]:
    """画师：按 原文写法 → 英文名 查补全（日文名 / 中文名 / 旧名都能命中），按作品数降序，最多 4 个。"""
    for q in (name.lstrip("@"), name_en.lstrip("@").replace("_", " ")):
        if q and q.strip():
            cands = autocomplete(q, category=CAT_ARTIST)
            if cands:
                return sorted(cands, key=lambda c: -c["post_count"])[:4]
    return []


def artist_profile(tag: str, post_count: int | None = None) -> dict:
    """画师资料：各语言别名（来自 /artists）、作品数。"""
    arts = _get("/artists.json", {"search[name]": tag, "only": "name,other_names,group_name", "limit": 1}) or []
    other = [n for n in (arts[0].get("other_names") or []) if n and "@" not in n][:20] if arts else []
    if post_count is None:
        info = _get("/tags.json", {"search[name]": tag, "only": "post_count"}) or [{}]
        post_count = info[0].get("post_count") if info else None
    return {"tag": tag, "post_count": post_count or 0, "other_names": other}


def ping(net_override: dict | None = None) -> dict:
    t0 = time.time()
    d = autocomplete("hatsune miku", net_override)
    return {"ok": bool(d), "elapsed": round(time.time() - t0, 2), "sample": d[0]["tag"] if d else None}
