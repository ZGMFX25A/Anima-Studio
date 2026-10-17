"""联网检索调度：识别角色与画师（单独一次 LLM 调用）→ 角色库 / 画师库 / Danbooru；标签参考（DanbooruSearch）。"""
from __future__ import annotations

import json
import re
import unicodedata

from .. import config
from ..llm import retry
from . import danbooru, outfits, variant_cache
from .library import ARTISTS, CHARACTERS, _covers, _fz
from .net import WebError


def _read(name: str) -> str:
    return (config.PROMPTS_DIR / name).read_text(encoding="utf-8").strip()


def _render(text: str, values: dict) -> str:
    return re.sub(r"\{\{(\w+)\}\}", lambda m: str(values.get(m.group(1), m.group(0))), text)


def extract(p: dict, m: dict, description: str, current: dict,
            request_id: str = "") -> tuple[list[dict], list[dict], list[dict], dict]:
    """识别描述中的作品角色、作品服装与画师 → (角色, 画师, 作品服装, 日志)。
    思考强度用该模型的最低档（能关就关）。出错自动重试（见 llm/retry.py）。

    current：「修改」时当前的 character / artist 字段（「生成」时为空）。
    """
    levels = (m.get("thinking") or {}).get("levels") or ["off"]
    user = _render(_read("search/extract_user.md"), {
        "description": description or "（空）", "current_character": current.get("character") or "（空）",
        "current_artist": current.get("artist") or "（空）"})
    schema = json.loads(_read("search/extract_schema.json"))
    log: dict = {"level": levels[0], "user": user}
    raw, _ = retry.chat(p, m, levels[0], _read("search/extract_system.md"), [{"type": "text", "text": user}], schema,
                        retry.json_check(("characters", "outfits", "artists")), log, request_id)
    log.pop("request", None)    # 完整请求体只在正式填写的日志里记

    def items(key: str, fields: tuple, n: int) -> list[dict]:
        out = []
        for c in raw.get(key) or []:
            if isinstance(c, dict):
                item = {k: str(c.get(k) or "").strip().lstrip("@") for k in fields}
                if item["name"] or item["name_en"]:
                    out.append(item)
        return out[:n]

    return (items("characters", ("name", "name_en", "series", "outfit"), 4), items("artists", ("name", "name_en"), 3),
            items("outfits", ("name", "name_en", "series", "wearer"), 3), log)


def _summary(ch: dict) -> dict:
    return {"id": ch["id"], "tag": ch["tag"], "display": ch["name"], "series_tag": ch.get("series_tag", ""),
            "post_count": ch["post_count"], "source": ch["source"], "edited": bool(ch["edited"])}


def _items(results: list[dict]) -> list[dict]:
    return [{"name": r["query"]["name"] or r["query"]["name_en"], "status": r["status"], "error": r["error"],
             "alternatives": r["alternatives"], "outfit": r["query"].get("outfit", ""),
             "variant_of": r["base"]["tag"] if r.get("base") else "", "options": len(r.get("options") or []),
             "outfit_tag": r["outfit"]["tag"] if r.get("outfit") else "", "outfit_id": r["outfit"]["id"] if r.get("outfit") else None,
             "outfit_options": len(r.get("outfit_options") or []),
             **(_summary(r["character"]) if r["character"] else {})}
            for r in results]


def _series_ok(row: dict, series: str) -> bool:
    """模糊命中时核对作品（识别结果里的作品是英文写法，如 blue archive）；没给作品时不核对。"""
    s = danbooru._skey(series)
    return not s or not s.isascii() or not row.get("series_tag") or danbooru._same_series(s, row["series_tag"])


def _research(found: list[dict], lib, find, profile, series_check: bool = False) -> list[dict]:
    """逐个解析：名称映射精确命中 → 本地模糊比对 → 联网查 Danbooru 并存入资料库。"""
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
        accept = (lambda row: _series_ok(row, q.get("series", ""))) if series_check else None
        fz = next((f[0] for f in (lib.fuzzy(n, accept) for n in names) if f), None)
        if fz:
            if fz["score"] > 1 and q["name"]:    # 「优香」→「早濑优香」这类字字都对上的，记下这个写法，下次直接命中
                lib.add_names(fz["row"]["id"], [q["name"]])
            entry.update(status="library", character=lib.get(fz["row"]["id"]), fuzzy=fz["matched"])
            results.append(entry)
            continue
        try:
            cands = find(q)
            if cands:
                # 同名的（如 3 个叫 k子 的画师）：资料库里已有的优先
                best = next((c for c in cands if lib.get_by_tag(c["tag"])), cands[0])
                entry["alternatives"] = [c["tag"] for c in cands if c is not best]
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
    return results


def _dedupe(results: list[dict]) -> list[dict]:
    """不同写法指向同一条目时只保留一个；同一角色的不同造型（变体，或没对上的服装）各留一条。"""
    seen, out = set(), []
    for e in results:
        ch = e["character"]
        key = (ch["id"], e["outfit"]["id"] if e.get("outfit") else e["query"].get("outfit", "")
               if e.get("options") or e.get("outfit_options") else "") if ch else None
        if key is None or key not in seen:
            out.append(e)
            if key is not None:
                seen.add(key)
    return out


# ───────────── 造型变体：按识别出的服装（或描述里这个角色所在的那一句）选变体 ─────────────

_CLAUSE = re.compile(r"[，,。.；;！!？?\n、]|和|与|跟|及|还有|以及|\band\b|\bwith\b")


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").casefold()


def _n(text: str) -> str:
    return re.sub(r"[\s_\-·・.@()（）\[\]【】「」『』]+", "", _fold(text))


def _cues(v: dict, base: dict) -> set[str]:
    """一个变体的「服装词」：标签里多出的括号（pajamas）、名称括号里的（睡衣）、名称去掉本体名剩下的（パジャマ）。"""
    base_names = sorted({_fold(n) for n in [base["name"], *base.get("names", [])] if len(_n(n)) >= 2}, key=len, reverse=True)
    series_words = {_n(p) for n in [base["name"], *base.get("names", [])] for p in re.findall(r"[(（]([^()（）]+)[)）]", _fold(n))}
    series_words |= {_n(base.get("series_tag", ""))}
    q = danbooru.qualifier(v["tag"], base["tag"])
    out = {q} | {w for w in re.split(r"[\s_]+", q) if len(w) >= 3 and not any(ch.isdigit() for ch in w)}
    for name in [v["name"], *v.get("names", [])]:
        s = _fold(name)
        out |= set(re.findall(r"[(（]([^()（）]+)[)）]", s))
        if not s.isascii():
            rest = s
            for b in base_names:
                rest = rest.replace(b, "")
            out.add(rest)
    cues = set()
    for c in out:
        k = _n(c)
        if k and k not in series_words and len(k) >= (3 if k.isascii() else 2) and len(k) <= 16:
            cues.add(k)
    return cues


def _clauses(description: str, names: list[str]) -> list[str]:
    """描述里提到这个角色的那几句（按标点和「和 / 与」分开）。"""
    keys = [_n(n) for n in names if len(_n(n)) >= 2]
    return [c for c in _CLAUSE.split(_fold(description)) if c and any(k in _n(c) for k in keys)]


def _base_of(ch: dict) -> dict:
    return (CHARACTERS.get_by_tag(ch["parent_tag"]) if ch.get("parent_tag") else None) or ch


def _outfit_texts(ch: dict, q: dict, description: str, scan: bool) -> list[str]:
    """拿来认服装的文字：识别出的服装原文；没给时是描述里这个角色所在的那几句。

    scan：同一角色出现了好几次时不看描述（那句里的服装多半是别的条目的）。
    """
    outfit = (q.get("outfit") or "").strip()
    if outfit:
        return [outfit]
    return _clauses(description, [q["name"], q["name_en"], ch["name"], *_base_of(ch).get("names", [])]) if scan else []


def _pick_variant(ch: dict, q: dict, description: str, scan: bool = True) -> tuple[dict, dict | None, list[dict]]:
    """返回 (用哪个条目, 它的本体（用的是变体时）, 没对上时附给 LLM 的可选变体)。

    scan：识别结果没给服装时，去描述里这个角色所在的那一句找；同一角色出现了好几次时不找（那句里的服装多半是别的条目的）。
    """
    base = _base_of(ch)
    variants = CHARACTERS.variants_of(base["tag"])
    if not variants:
        return ch, (base if base is not ch else None), []
    outfit = (q.get("outfit") or "").strip()
    texts = [_n(t) for t in _outfit_texts(ch, q, description, scan)]
    best, best_len = None, 0
    for v in variants:
        for cue in _cues(v, base):
            if any(cue in t or (len(t) >= 2 and t in cue) for t in texts if t) and len(cue) > best_len:
                best, best_len = v, len(cue)
    if best:
        return CHARACTERS.get(best["id"]), base, []
    if ch is not base:                       # 名字本身就是变体（如「米莉亚」）
        return ch, base, []
    return ch, None, variants if outfit else []


def research_characters(found: list[dict], description: str = "") -> list[dict]:
    """角色 → 角色库 / Danbooru；再按服装认造型变体，变体没对上时认作品服装（服装库，见 outfits.py）。

    结果条目：character（用哪个条目）、base（用的是变体时的本体）、options（没对上时附给 LLM 的可选变体）、
    outfit（名字对上的作品服装）、outfit_options（都没对上时附给 LLM 的可选作品服装）。
    """
    results = _research(found, CHARACTERS, lambda q: danbooru.find_character(q["name"], q["name_en"], q["series"]),
                        danbooru.profile, series_check=True)
    bases = [_base_of(r["character"])["tag"] if r["character"] else None for r in results]
    for r, b in zip(results, bases):
        r.update(outfit=None, outfit_options=[])
        if r["character"]:
            variant_cache.schedule(r["character"]["tag"])      # 后台补齐本体与变体、作品服装，不拖慢这次填写
            scan = bases.count(b) == 1
            ch, base, options = _pick_variant(r["character"], r["query"], description, scan)
            r.update(character=ch, base=base, options=options)
            if base is None:          # 用的是本体（没有对上变体）：服装可能是几个角色共用的作品服装
                row, opts = outfits.pick(ch, (r["query"].get("outfit") or "").strip(),
                                         _outfit_texts(ch, r["query"], description, scan))
                r.update(outfit=row, outfit_options=opts)
                if row:
                    r["options"] = []
    return _dedupe(results)


def chosen_outfits(results: list[dict], appearance: str, entries: list[dict]) -> list[dict]:
    """填写结果的 appearance 用了「可选的作品服装」里的某一套 → 存入服装库（只存用到的那套），返回新增的服装资料条目。"""
    used = {re.sub(r"\s+", "_", t.replace("\\", "").strip().lower()) for t in appearance.split(",")}
    have = {e["outfit"]["tag"] for e in entries}
    out: dict[str, dict] = {}
    for r in results:
        for o in r.get("outfit_options") or []:
            if o["tag"].lower() in used and o["tag"] not in have:
                if o["tag"] not in out:
                    out[o["tag"]] = {"outfit": outfits.save_used(o), "wearers": [], "chosen": True}
                who = r["query"]["name"] or r["character"]["name"]
                if who not in out[o["tag"]]["wearers"]:
                    out[o["tag"]]["wearers"].append(who)
    return list(out.values())


def research_outfits(results: list[dict], description: str, found: list[dict] = ()) -> tuple[list[dict], list[dict]]:
    """服装资料块的条目 → (条目, 没找到的)。

    条目来源：角色名字对上的作品服装；识别出的作品服装（不管谁穿：人设、原创角色也算；服装库 → Danbooru）；
    描述里直接写了名字的服装库服装（只查本地）。识别出的服装正好是某个角色的服装（原文相同或写明是他穿）时，
    挂到那个角色上（角色已对上造型变体的不动）。
    """
    out: dict[int, dict] = {}

    def add(row: dict, who: str) -> None:
        e = out.setdefault(row["id"], {"outfit": row, "wearers": []})
        if who and who not in e["wearers"]:
            e["wearers"].append(who)

    for r in results:
        if r.get("outfit"):
            add(r["outfit"], r["query"]["name"] or r["character"]["name"])
    misses = []
    for q in found:
        row, err = outfits.resolve(q)
        if not row:
            misses.append({"query": q, "error": err})
            continue
        add(row, q.get("wearer", ""))
        for r in results:
            rq = r["query"]
            text = outfits._key(rq.get("outfit", ""))
            if r["character"] and not r.get("outfit") and not r.get("base") and (
                    (text and text == outfits._key(q["name"]))
                    or (not text and q.get("wearer") and q["wearer"] in (rq["name"], rq["name_en"]))):
                r.update(outfit=row, outfit_options=[], options=[])
                add(row, rq["name"] or r["character"]["name"])
    for row in outfits.scan(description, set(out)):
        out[row["id"]] = {"outfit": row, "wearers": []}
    return list(out.values()), misses


def research_artists(found: list[dict]) -> list[dict]:
    return _dedupe(_research(found, ARTISTS, lambda q: danbooru.find_artist(q["name"], q["name_en"]),
                             danbooru.artist_profile))


def fuzzy_search(name: str, series: str = "") -> dict:
    """角色库 / 人设导入的「模糊搜索」：先列角色库里模糊比对上的（「睡衣优香」→ 早濑优香(睡衣)），再接 Danbooru 的结果。

    Danbooru 连不上时只给角色库的结果（offline 为出错信息）；两边都没有时抛出 WebError。
    """
    offline = None
    try:
        r = danbooru.search_characters(name, series)
    except WebError as e:
        s = danbooru._skey(series)
        r, offline = {"items": [], "series_tags": [s] if s and s.isascii() else []}, str(e)
    keys = r["series_tags"]
    local = []
    for x in CHARACTERS.fuzzy(name, limit=10):
        row = x["row"]
        sm = (danbooru._series_hit(row["tag"], keys) or any(danbooru._same_series(_n(row["series_tag"]), _n(k))
                                                             for k in keys if row["series_tag"])) if keys else None
        local.append({"tag": row["tag"], "post_count": row["post_count"], "matched": x["matched"],
                      "series_match": sm, "exact": x["score"] > 1, "local": True})
    if offline and not local:
        raise WebError(offline)
    have = {x["tag"] for x in local}
    items = local + [c for c in r["items"] if c["tag"] not in have]
    items.sort(key=lambda c: (c["series_match"] is False, not c.get("local")))   # 稳定排序：属于该作品的在前，同组里角色库的在前
    return {"items": items[:20], "series_tags": keys, "offline": offline}


def fuzzy_persona(q: dict, plist: list[dict]) -> dict | None:
    """识别出的角色名与人设模糊比对（字字都出现在人设的名称 / 别名 / 角色库名称里，并核对作品）：「优香」→ 人设「早濑优香」。"""
    best, best_s = None, 0.0
    for name in (q["name"], q["name_en"]):
        k = _fz(name)
        if len(k) < 2:
            continue
        for p in plist:
            if q.get("series") and p.get("series") and not danbooru._same_series(
                    danbooru._skey(q["series"]), danbooru._skey(p["series"])):
                continue
            for n in [p["name"], *p["aliases"], *persona_names(p)]:
                t = _fz(n)
                if t and ((k in t) if k.isascii() else _covers(k, t)) and len(k) / len(t) > best_s:
                    best, best_s = p, len(k) / len(t)
    return best


def persona_names(p: dict) -> list[str]:
    """已有角色的人设，另外认角色库里这个角色（本体与变体）的全部名称。"""
    if p.get("kind") != "existing":
        return []
    out = []
    for t in {p.get("character_tag"), p.get("source_tag"), *(o.get("character_tag") for o in p.get("outfits") or [])}:
        row = CHARACTERS.get_by_tag((t or "").strip().replace(" ", "_")) if t else None
        if not row:
            continue
        base = CHARACTERS.get_by_tag(row["parent_tag"]) if row.get("parent_tag") else row
        for r in [base or row, *CHARACTERS.variants_of((base or row)["tag"])]:
            out += [r["name"], *r["names"]]
    return list(dict.fromkeys(out))


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


def _option(v: dict, base: dict) -> str:
    """没对上服装时附给 LLM 的一个可选变体：标签（名称）：这个变体特有的服装标签。"""
    own = {a["tag"] for a in base["appearance"]}
    extra = [a["tag"].replace("_", " ") for a in v["appearance"] if a["tag"] not in own and (a.get("freq") or 0) >= 0.2][:6]
    return f"{v['tag'].replace('_', ' ')}（{v['name']}）" + (f"：{', '.join(extra)}" if extra else "")


def _outfit_option(o: dict, ch: dict) -> str:
    """没对上服装时附给 LLM 的一套作品服装：标签（别名，这个角色穿过）：简介开头 / 角色本身没有的常见标签。"""
    own = {a["tag"] for a in ch["appearance"]}
    extra = [a["tag"].replace("_", " ") for a in o["appearance"] if a["tag"] not in own and (a.get("freq") or 0) >= 0.2][:8]
    alias = next((n for n in [o["name"], *o["other_names"]] if n and not n.isascii()), "")
    note = "，".join(x for x in (alias, "这个角色穿过" if outfits.wear(o, ch["tag"]) else "") if x)
    desc = "／".join(x for x in (outfits.brief(o["wiki"], 160), ", ".join(extra)) if x)
    return f"{o['tag'].replace('_', ' ')}" + (f"（{note}）" if note else "") + (f"：{desc}" if desc else "")


def outfit_block(entries: list[dict]) -> str:
    """服装资料块：名字对上的作品服装（谁穿、简介、常见标签、补充说明）。"""
    items = []
    for e in entries:
        o = e["outfit"]
        app = ", ".join(f"{a['tag'].replace('_', ' ')}（{round(a['freq'] * 100)}%）" if a.get("freq") is not None
                        else a["tag"].replace("_", " ") for a in o["appearance"])
        items.append(_drop_empty_lines(_render(_read("fill/user/outfit_item.md"), {
            "name": o["name"], "tag": o["tag"].replace("_", " "), "series": o["series_tag"].replace("_", " "),
            "wearers": "、".join(e["wearers"]), "other_names": "、".join(o["other_names"][:6]),
            "wiki": " ".join(o["wiki"].split()), "appearance": app, "notes": " ".join(o["notes"].split())})))
    if not items:
        return ""
    return _render(_read("fill/user/outfit_block.md"), {"outfits": "\n\n".join(items)})


def character_block(results: list[dict], labels: dict) -> str:
    items = []
    for r in results:
        ch = r["character"]
        if not ch:
            continue
        base = r.get("base")
        app = ", ".join(f"{a['tag'].replace('_', ' ')}（{round(a['freq'] * 100)}%）" if a.get("freq") is not None
                        else a["tag"].replace("_", " ") for a in ch["appearance"])
        notes = " ".join(" ".join(x.split()) for x in (ch["notes"], base["notes"] if base else "") if x.strip())
        text = _render(_read("fill/user/character_item.md"), {
            "name": ch["name"], "tag": ch["tag"].replace("_", " "), "base_tag": base["tag"].replace("_", " ") if base else "",
            "series": ch["series_tag"].replace("_", " "), "outfit": r["query"].get("outfit", ""),
            "wiki": " ".join(ch["wiki"].split()), "appearance": app, "notes": notes,
            "variants": "；".join(_option(v, ch) for v in r.get("options") or []),
            "outfit_tag": r["outfit"]["tag"].replace("_", " ") if r.get("outfit") else "",
            "outfit_options": "；".join(_outfit_option(o, ch) for o in r.get("outfit_options") or [])})
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
            want_web: bool, personas: list[dict] | None = None, outfit_entries: list[dict] | None = None,
            outfit_misses: list[dict] | None = None) -> dict:
    return {
        "personas": personas or [],
        "characters": _items(results) if want_web else None,
        "outfits": [{"id": e["outfit"]["id"], "tag": e["outfit"]["tag"], "name": e["outfit"]["name"],
                     "series_tag": e["outfit"]["series_tag"], "post_count": e["outfit"]["post_count"],
                     "wearers": e["wearers"], "edited": bool(e["outfit"]["edited"]), "chosen": bool(e.get("chosen"))}
                    for e in outfit_entries or []] if want_web else None,
        "outfit_misses": [{"name": m["query"]["name"] or m["query"]["name_en"], "series": m["query"]["series"],
                           "wearer": m["query"]["wearer"], "error": m["error"]} for m in outfit_misses or []],
        "artists": _items(artists) if want_web else None,
        "tags": None if tags is None and tag_error is None else {
            "count": len(tags["results"]) if tags else 0, "cached": bool(tags and tags.get("cached")),
            "remaining": tags.get("remaining") if tags else None, "pool": tags.get("pool") if tags else None,
            "items": [t["tag"] for t in tags["results"]][:60] if tags else [], "error": tag_error},
    }
