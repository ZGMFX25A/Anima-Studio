"""联网检索调度：识别角色与画师（单独一次 LLM 调用）→ 角色库 / 画师库 / Danbooru；标签参考（DanbooruSearch）。"""
from __future__ import annotations

import json
import re

from .. import config
from ..llm import retry
from . import danbooru
from .library import ARTISTS, CHARACTERS
from .net import WebError


def _read(name: str) -> str:
    return (config.PROMPTS_DIR / name).read_text(encoding="utf-8").strip()


def _render(text: str, values: dict) -> str:
    return re.sub(r"\{\{(\w+)\}\}", lambda m: str(values.get(m.group(1), m.group(0))), text)


def extract(p: dict, m: dict, description: str, current: dict,
            request_id: str = "") -> tuple[list[dict], list[dict], dict]:
    """识别描述中的作品角色与画师。思考强度用该模型的最低档（能关就关）。出错自动重试（见 llm/retry.py）。

    current：「修改」时当前的 character / artist 字段（「生成」时为空）。
    """
    levels = (m.get("thinking") or {}).get("levels") or ["off"]
    user = _render(_read("search/extract_user.md"), {
        "description": description or "（空）", "current_character": current.get("character") or "（空）",
        "current_artist": current.get("artist") or "（空）"})
    schema = json.loads(_read("search/extract_schema.json"))
    log: dict = {"level": levels[0], "user": user}
    raw, _ = retry.chat(p, m, levels[0], _read("search/extract_system.md"), [{"type": "text", "text": user}], schema,
                        retry.json_check(("characters", "artists")), log, request_id)
    log.pop("request", None)    # 完整请求体只在正式填写的日志里记

    def items(key: str, fields: tuple) -> list[dict]:
        out = []
        for c in raw.get(key) or []:
            if isinstance(c, dict):
                item = {k: str(c.get(k) or "").strip().lstrip("@") for k in fields}
                if item["name"] or item["name_en"]:
                    out.append(item)
        return out[:3]

    return items("characters", ("name", "name_en", "series")), items("artists", ("name", "name_en")), log


def _summary(ch: dict) -> dict:
    return {"id": ch["id"], "tag": ch["tag"], "display": ch["name"], "series_tag": ch.get("series_tag", ""),
            "post_count": ch["post_count"], "source": ch["source"], "edited": bool(ch["edited"])}


def _items(results: list[dict]) -> list[dict]:
    return [{"name": r["query"]["name"] or r["query"]["name_en"], "status": r["status"], "error": r["error"],
             "alternatives": r["alternatives"], **(_summary(r["character"]) if r["character"] else {})}
            for r in results]


def _research(found: list[dict], lib, find, profile) -> list[dict]:
    """逐个解析：先查本地资料库，没有才联网查 Danbooru 并存入资料库。"""
    results = []
    for q in found:
        names = [n for n in (q["name"], q["name_en"]) if n]
        entry = {"query": q, "status": "not_found", "character": None, "alternatives": [], "error": None}
        hit = next((c for c in (lib.lookup(n) for n in names) if c), None)
        if hit:
            lib.add_names(hit["id"], names)
            entry.update(status="library", character=hit)
            results.append(entry)
            continue
        try:
            cands = find(q)
            if cands:
                best = cands[0]
                entry["alternatives"] = [c["tag"] for c in cands[1:]]
                existing = lib.get_by_tag(best["tag"])
                if existing:
                    lib.add_names(existing["id"], names)
                    entry.update(status="library", character=lib.get(existing["id"]))
                else:
                    prof = profile(best["tag"], best["post_count"])
                    display = q["name"] or best["tag"].replace("_", " ")
                    entry.update(status="danbooru", character=lib.save_from_danbooru(prof, display, names))
        except WebError as e:
            entry.update(status="error", error=str(e))
        results.append(entry)
    # 不同写法指向同一条目时只保留一个
    seen, out = set(), []
    for e in results:
        cid = (e["character"] or {}).get("id")
        if cid is None or cid not in seen:
            out.append(e)
            if cid is not None:
                seen.add(cid)
    return out


def research_characters(found: list[dict]) -> list[dict]:
    return _research(found, CHARACTERS, lambda q: danbooru.find_character(q["name"], q["name_en"], q["series"]),
                     danbooru.profile)


def research_artists(found: list[dict]) -> list[dict]:
    return _research(found, ARTISTS, lambda q: danbooru.find_artist(q["name"], q["name_en"]), danbooru.artist_profile)


def _drop_empty_lines(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not re.search(r"[：:]\s*$", line))


def artist_block(results: list[dict]) -> str:
    items = []
    for r in results:
        a = r["character"]
        if not a:
            continue
        others = "、".join(n for n in a["other_names"][:8] if n != a["tag"])
        items.append(_drop_empty_lines(_render(_read("fill/user/artist_item.md"), {
            "name": a["name"], "tag": "@" + a["tag"].replace("_", " "), "other_names": others,
            "post_count": a["post_count"] or "", "notes": " ".join(a["notes"].split())})))
    if not items:
        return ""
    return _render(_read("fill/user/artist_block.md"), {"artists": "\n\n".join(items)})


def character_block(results: list[dict], labels: dict) -> str:
    items = []
    for r in results:
        ch = r["character"]
        if not ch:
            continue
        app = ", ".join(f"{a['tag'].replace('_', ' ')}（{round(a['freq'] * 100)}%）" if a.get("freq") is not None
                        else a["tag"].replace("_", " ") for a in ch["appearance"])
        text = _render(_read("fill/user/character_item.md"), {
            "name": ch["name"], "tag": ch["tag"].replace("_", " "), "series": ch["series_tag"].replace("_", " "),
            "wiki": " ".join(ch["wiki"].split()), "appearance": app, "notes": " ".join(ch["notes"].split())})
        items.append(_drop_empty_lines(text))
    if not items:
        return ""
    return _render(_read("fill/user/character_block.md"), {"characters": "\n\n".join(items)})


def tag_block(tags: dict | None) -> str:
    if not tags or not tags.get("results"):
        return ""
    line = ", ".join(f"{t['tag'].replace('_', ' ')}（{t['cn']}）" if t.get("cn") else t["tag"].replace("_", " ")
                     for t in tags["results"])
    return _render(_read("fill/user/tag_block.md"), {"tags": line})


def summary(results: list[dict], artists: list[dict], tags: dict | None, tag_error: str | None,
            want_web: bool, personas: list[dict] | None = None) -> dict:
    return {
        "personas": personas or [],
        "characters": _items(results) if want_web else None,
        "artists": _items(artists) if want_web else None,
        "tags": None if tags is None and tag_error is None else {
            "count": len(tags["results"]) if tags else 0, "cached": bool(tags and tags.get("cached")),
            "remaining": tags.get("remaining") if tags else None, "pool": tags.get("pool") if tags else None,
            "items": [t["tag"] for t in tags["results"]][:60] if tags else [], "error": tag_error},
    }
